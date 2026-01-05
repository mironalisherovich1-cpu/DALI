import os
import re
import json
import time
import math
import random
import sqlite3
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.exceptions import TelegramBadRequest


# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
LTC_ADDRESS = os.getenv("LTC_ADDRESS", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

DEFAULT_USD_PER_LTC = float(os.getenv("DEFAULT_USD_PER_LTC", "100") or "100")
DB_PATH = os.getenv("DB_PATH", "shop.sqlite3")

# Payment checking
CHECK_INTERVAL_SEC = 45
MIN_CONFIRMATIONS = 1
# amount matching tolerance in LTC (very small)
AMOUNT_TOL_LTC = 0.00000001  # 1 litoshi

# SoChain endpoints (Litecoin)
SOCHAIN_RECEIVED = "https://sochain.com/api/v2/get_tx_received/LTC/{address}"
SOCHAIN_TX = "https://sochain.com/api/v2/get_tx/LTC/{txid}"

# Price rate endpoint (CoinGecko)
COINGECKO_RATE = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd"


# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY,
            balance_usd REAL NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            price_usd REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            amount_usd REAL NOT NULL,
            usd_per_ltc REAL NOT NULL,
            amount_ltc REAL NOT NULL,
            status TEXT NOT NULL, -- pending|paid|delivered|cancelled|expired
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            txid TEXT,
            UNIQUE(tg_id, id)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_tx(
            txid TEXT PRIMARY KEY,
            seen_at INTEGER NOT NULL
        )
        """)
        conn.commit()

def ensure_user(tg_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO users(tg_id, balance_usd, created_at) VALUES(?,?,?)",
                (tg_id, 0.0, int(time.time()))
            )
            conn.commit()

def get_balance(tg_id: int) -> float:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance_usd FROM users WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        return float(row["balance_usd"]) if row else 0.0

def add_balance(tg_id: int, usd: float):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance_usd = balance_usd + ? WHERE tg_id=?", (usd, tg_id))
        conn.commit()

def deduct_balance(tg_id: int, usd: float) -> bool:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance_usd FROM users WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        if not row:
            return False
        bal = float(row["balance_usd"])
        if bal + 1e-9 < usd:
            return False
        cur.execute("UPDATE users SET balance_usd = balance_usd - ? WHERE tg_id=?", (usd, tg_id))
        conn.commit()
        return True

def list_products(active_only=True) -> List[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        if active_only:
            cur.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id DESC")
        else:
            cur.execute("SELECT * FROM products ORDER BY id DESC")
        return cur.fetchall()

def get_product(pid: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE id=?", (pid,))
        return cur.fetchone()

def add_product(name: str, description: str, price_usd: float, stock: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO products(name, description, price_usd, stock, is_active) VALUES(?,?,?,?,1)",
            (name, description, price_usd, stock)
        )
        conn.commit()

def delete_product(pid: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
        conn.commit()

def reduce_stock(pid: int, qty: int) -> bool:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT stock FROM products WHERE id=? AND is_active=1", (pid,))
        row = cur.fetchone()
        if not row:
            return False
        stock = int(row["stock"])
        if stock < qty:
            return False
        cur.execute("UPDATE products SET stock = stock - ? WHERE id=?", (qty, pid))
        conn.commit()
        return True

def create_order(tg_id: int, pid: int, qty: int, amount_usd: float, usd_per_ltc: float, amount_ltc: float) -> int:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders(tg_id, product_id, qty, amount_usd, usd_per_ltc, amount_ltc, status, created_at)
            VALUES(?,?,?,?,?,?, 'pending', ?)
        """, (tg_id, pid, qty, amount_usd, usd_per_ltc, amount_ltc, int(time.time())))
        conn.commit()
        return int(cur.lastrowid)

def list_user_orders(tg_id: int, limit: int = 10) -> List[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT o.*, p.name as product_name
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.tg_id=?
            ORDER BY o.id DESC
            LIMIT ?
        """, (tg_id, limit))
        return cur.fetchall()

def get_order(order_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT o.*, p.name as product_name, p.description as product_desc
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.id=?
        """, (order_id,))
        return cur.fetchone()

def mark_order_paid(order_id: int, txid: str):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE orders
            SET status='paid', paid_at=?, txid=?
            WHERE id=? AND status='pending'
        """, (int(time.time()), txid, order_id))
        conn.commit()

def mark_order_delivered(order_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE orders SET status='delivered' WHERE id=? AND status='paid'", (order_id,))
        conn.commit()

def get_pending_orders(limit: int = 50) -> List[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT o.*
            FROM orders o
            WHERE o.status='pending'
            ORDER BY o.created_at ASC
            LIMIT ?
        """, (limit,))
        return cur.fetchall()

def tx_seen(txid: str) -> bool:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT txid FROM seen_tx WHERE txid=?", (txid,))
        return cur.fetchone() is not None

def mark_tx_seen(txid: str):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO seen_tx(txid, seen_at) VALUES(?,?)", (txid, int(time.time())))
        conn.commit()


# =========================
# UI
# =========================
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Shop"), KeyboardButton(text="💰 Balance")],
            [KeyboardButton(text="📦 My Orders"), KeyboardButton(text="ℹ️ Info")]
        ],
        resize_keyboard=True
    )

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add product", callback_data="admin:add")],
        [InlineKeyboardButton(text="📦 List products", callback_data="admin:list")],
        [InlineKeyboardButton(text="❌ Disable product", callback_data="admin:del")],
        [InlineKeyboardButton(text="➕ Add balance (manual)", callback_data="admin:bal")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats")],
    ])

def products_kb() -> InlineKeyboardMarkup:
    rows = []
    for p in list_products(active_only=True)[:20]:
        rows.append([InlineKeyboardButton(
            text=f"#{p['id']} • {p['name']} • ${p['price_usd']:.2f} • stock:{p['stock']}",
            callback_data=f"p:{p['id']}"
        )])
    rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="shop:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def product_card_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Buy 1", callback_data=f"buy:{pid}:1")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="shop:back")]
    ])

def order_kb(order_id: int, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔄 Check payment", callback_data=f"order:check:{order_id}")],
        [InlineKeyboardButton(text="⬅️ Back to Shop", callback_data="shop:back")]
    ]
    if is_admin:
        rows.insert(0, [InlineKeyboardButton(text="📦 Mark delivered", callback_data=f"admin:deliver:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================
# PAYMENT LOGIC
# =========================
async def get_usd_per_ltc(session: aiohttp.ClientSession) -> float:
    try:
        async with session.get(COINGECKO_RATE, timeout=10) as r:
            data = await r.json()
            usd = float(data["litecoin"]["usd"])
            if usd > 0:
                return usd
    except Exception:
        pass
    return DEFAULT_USD_PER_LTC

def make_unique_ltc_amount(base_ltc: float) -> float:
    """
    To match payments on a single merchant address, we add a tiny random litoshi offset.
    """
    litoshis = int(round(base_ltc * 1e8))
    offset = random.randint(1, 50)  # 1..50 litoshi
    return (litoshis + offset) / 1e8

async def fetch_received_txs(session: aiohttp.ClientSession) -> List[Dict]:
    """
    Returns list of received txs at LTC_ADDRESS from SoChain.
    """
    url = SOCHAIN_RECEIVED.format(address=LTC_ADDRESS)
    async with session.get(url, timeout=15) as r:
        j = await r.json()
    if j.get("status") != "success":
        return []
    return j["data"].get("txs", [])

async def fetch_tx_details(session: aiohttp.ClientSession, txid: str) -> Optional[Dict]:
    url = SOCHAIN_TX.format(txid=txid)
    async with session.get(url, timeout=15) as r:
        j = await r.json()
    if j.get("status") != "success":
        return None
    return j.get("data")

def tx_pays_exact_amount_to_address(tx_data: Dict, address: str, expected_ltc: float) -> bool:
    """
    Validate tx has output to 'address' with amount ~ expected_ltc.
    SoChain tx data includes outputs with 'address' and 'value' (as string).
    """
    outputs = tx_data.get("outputs", [])
    for out in outputs:
        if out.get("address") == address:
            try:
                v = float(out.get("value"))
            except Exception:
                continue
            if abs(v - expected_ltc) <= AMOUNT_TOL_LTC:
                return True
    return False

def get_confirmations(tx_data: Dict) -> int:
    try:
        return int(tx_data.get("confirmations", 0))
    except Exception:
        return 0


# =========================
# ADMIN STATE (simple, in-memory)
# =========================
@dataclass
class AdminDraft:
    step: str
    payload: dict

ADMIN_DRAFTS: Dict[int, AdminDraft] = {}  # tg_id -> draft


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


# =========================
# BOT
# =========================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user.id)

    txt = (
        "✅ Bot ishga tushdi.\n\n"
        "🛒 Shop — tovarlar\n"
        "💰 Balance — balans\n"
        "📦 My Orders — buyurtmalar\n"
    )
    await message.answer(txt, reply_markup=main_menu_kb())


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Admin emas.")
    await message.answer("🧑‍💼 Admin panel:", reply_markup=None)
    await message.answer("Tanlang:", reply_markup=admin_menu_kb())


@dp.message(F.text == "🛒 Shop")
async def shop(message: Message):
    if not LTC_ADDRESS:
        return await message.answer("❌ LTC_ADDRESS sozlanmagan (Railway Variables).")
    items = list_products(active_only=True)
    if not items:
        return await message.answer("Hozircha tovar yo‘q.")
    await message.answer("🛒 Vitrina:", reply_markup=None)
    await message.answer("Tovar tanlang:", reply_markup=products_kb())


@dp.message(F.text == "💰 Balance")
async def balance(message: Message):
    ensure_user(message.from_user.id)
    bal = get_balance(message.from_user.id)
    await message.answer(f"💰 Balans: ${bal:.2f}", reply_markup=main_menu_kb())


@dp.message(F.text == "📦 My Orders")
async def my_orders(message: Message):
    ensure_user(message.from_user.id)
    orders = list_user_orders(message.from_user.id, limit=10)
    if not orders:
        return await message.answer("📦 Buyurtmalar yo‘q.")
    lines = ["📦 Oxirgi buyurtmalar:"]
    for o in orders:
        lines.append(f"#{o['id']} • {o['product_name']} x{o['qty']} • ${o['amount_usd']:.2f} • {o['status']}")
    await message.answer("\n".join(lines))


@dp.message(F.text == "ℹ️ Info")
async def info(message: Message):
    await message.answer(
        "ℹ️ To‘lov: LTC\n"
        "Bot buyurtma uchun aniq LTC miqdorni beradi.\n"
        "To‘lov kelgach avtomatik tasdiqlanadi."
    )


# ---------- Callbacks (Shop) ----------
@dp.callback_query(F.data == "shop:refresh")
async def cb_refresh(call: CallbackQuery):
    await call.message.edit_reply_markup(reply_markup=products_kb())
    await call.answer("Refreshed")

@dp.callback_query(F.data == "shop:back")
async def cb_back(call: CallbackQuery):
    try:
        await call.message.edit_text("🛒 Vitrina:", reply_markup=products_kb())
    except TelegramBadRequest:
        await call.message.answer("🛒 Vitrina:", reply_markup=products_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("p:"))
async def cb_product(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        return await call.answer("Topilmadi", show_alert=True)
    txt = (
        f"🧾 Tovar: {p['name']}\n"
        f"💵 Narx: ${float(p['price_usd']):.2f}\n"
        f"📦 Stock: {int(p['stock'])}\n\n"
        f"{p['description']}"
    )
    await call.message.edit_text(txt, reply_markup=product_card_kb(pid))
    await call.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery):
    if not LTC_ADDRESS:
        return await call.answer("LTC_ADDRESS sozlanmagan", show_alert=True)

    ensure_user(call.from_user.id)

    _, pid_s, qty_s = call.data.split(":")
    pid = int(pid_s)
    qty = int(qty_s)

    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        return await call.answer("Tovar topilmadi", show_alert=True)
    if int(p["stock"]) < qty:
        return await call.answer("Stock yetarli emas", show_alert=True)

    amount_usd = float(p["price_usd"]) * qty

    async with aiohttp.ClientSession() as session:
        usd_per_ltc = await get_usd_per_ltc(session)

    base_ltc = amount_usd / usd_per_ltc
    amount_ltc = make_unique_ltc_amount(base_ltc)

    # Create order
    order_id = create_order(
        tg_id=call.from_user.id,
        pid=pid,
        qty=qty,
        amount_usd=amount_usd,
        usd_per_ltc=usd_per_ltc,
        amount_ltc=amount_ltc
    )

    txt = (
        f"🧾 Order #{order_id}\n"
        f"📦 {p['name']} x{qty}\n"
        f"💵 ${amount_usd:.2f}\n\n"
        f"✅ To‘lov uchun:\n"
        f"➡️ Address: `{LTC_ADDRESS}`\n"
        f"➡️ Amount: *{amount_ltc:.8f} LTC*\n\n"
        f"⚠️ Aynan shu miqdorni yuboring (unikal).\n"
        f"Confirmations: {MIN_CONFIRMATIONS}+ bo‘lsa avtomatik tasdiqlanadi."
    )

    await call.message.edit_text(txt, reply_markup=order_kb(order_id, is_admin=is_admin(call.from_user.id)), parse_mode="Markdown")
    await call.answer()


# ---------- Order check ----------
@dp.callback_query(F.data.startswith("order:check:"))
async def cb_order_check(call: CallbackQuery):
    order_id = int(call.data.split(":")[2])
    o = get_order(order_id)
    if not o or int(o["tg_id"]) != call.from_user.id:
        return await call.answer("Order topilmadi", show_alert=True)

    if o["status"] != "pending":
        return await call.answer(f"Status: {o['status']}", show_alert=True)

    # Force one check pass (quick)
    async with aiohttp.ClientSession() as session:
        ok, txid, conf = await try_match_order_payment(session, o)

    if ok and txid:
        mark_order_paid(order_id, txid)
        # credit internal balance
        add_balance(call.from_user.id, float(o["amount_usd"]))

        await call.message.edit_text(
            f"✅ Order #{order_id} PAID\n"
            f"TX: `{txid}`\n"
            f"Confirmations: {conf}\n\n"
            f"💰 Balansga qo‘shildi: ${float(o['amount_usd']):.2f}",
            parse_mode="Markdown",
            reply_markup=order_kb(order_id, is_admin=is_admin(call.from_user.id))
        )
        await call.answer("Paid ✅", show_alert=True)
    else:
        await call.answer("Hali to‘lov topilmadi", show_alert=True)


# =========================
# ADMIN CALLBACKS / FLOWS
# =========================
@dp.callback_query(F.data == "admin:add")
async def admin_add(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("No", show_alert=True)
    ADMIN_DRAFTS[call.from_user.id] = AdminDraft(step="name", payload={})
    await call.message.answer("➕ Product name yubor:")
    await call.answer()

@dp.callback_query(F.data == "admin:list")
async def admin_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("No", show_alert=True)
    items = list_products(active_only=False)[:50]
    if not items:
        await call.message.answer("Tovar yo‘q.")
    else:
        lines = ["📦 Products:"]
        for p in items:
            lines.append(f"#{p['id']} • {p['name']} • ${float(p['price_usd']):.2f} • stock:{int(p['stock'])} • active:{int(p['is_active'])}")
        await call.message.answer("\n".join(lines))
    await call.answer()

@dp.callback_query(F.data == "admin:del")
async def admin_del(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("No", show_alert=True)
    ADMIN_DRAFTS[call.from_user.id] = AdminDraft(step="del_id", payload={})
    await call.message.answer("❌ O‘chirish uchun product ID yubor (masalan: 12):")
    await call.answer()

@dp.callback_query(F.data == "admin:bal")
async def admin_bal(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("No", show_alert=True)
    ADMIN_DRAFTS[call.from_user.id] = AdminDraft(step="bal_tg", payload={})
    await call.message.answer("➕ Balance: user tg_id yubor:")
    await call.answer()

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("No", show_alert=True)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) c FROM users")
        users = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM orders")
        orders = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM orders WHERE status='pending'")
        pending = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM orders WHERE status='paid'")
        paid = cur.fetchone()["c"]
    await call.message.answer(
        f"📊 Stats\n"
        f"Users: {users}\n"
        f"Orders: {orders}\n"
        f"Pending: {pending}\n"
        f"Paid: {paid}\n"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("admin:deliver:"))
async def admin_deliver(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("No", show_alert=True)
    order_id = int(call.data.split(":")[2])
    o = get_order(order_id)
    if not o:
        return await call.answer("Order topilmadi", show_alert=True)
    if o["status"] != "paid":
        return await call.answer(f"Status: {o['status']}", show_alert=True)
    mark_order_delivered(order_id)
    await call.message.answer(f"📦 Order #{order_id} delivered ✅")
    await call.answer("OK")


@dp.message()
async def admin_text_flow(message: Message):
    tg = message.from_user.id
    if tg not in ADMIN_DRAFTS:
        return

    if not is_admin(tg):
        ADMIN_DRAFTS.pop(tg, None)
        return

    draft = ADMIN_DRAFTS[tg]

    if draft.step == "name":
        draft.payload["name"] = message.text.strip()
        draft.step = "desc"
        return await message.answer("Description yubor:")

    if draft.step == "desc":
        draft.payload["desc"] = message.text.strip()
        draft.step = "price"
        return await message.answer("Price USD yubor (masalan: 25.5):")

    if draft.step == "price":
        try:
            price = float(message.text.replace(",", ".").strip())
            if price <= 0:
                raise ValueError
        except Exception:
            return await message.answer("❌ Price noto‘g‘ri. Masalan: 25.5")
        draft.payload["price"] = price
        draft.step = "stock"
        return await message.answer("Stock yubor (masalan: 10):")

    if draft.step == "stock":
        try:
            stock = int(message.text.strip())
            if stock < 0:
                raise ValueError
        except Exception:
            return await message.answer("❌ Stock noto‘g‘ri. Masalan: 10")
        add_product(
            name=draft.payload["name"],
            description=draft.payload["desc"],
            price_usd=float(draft.payload["price"]),
            stock=stock
        )
        ADMIN_DRAFTS.pop(tg, None)
        return await message.answer("✅ Product qo‘shildi.")

    if draft.step == "del_id":
        try:
            pid = int(message.text.strip())
        except Exception:
            return await message.answer("❌ ID noto‘g‘ri.")
        delete_product(pid)
        ADMIN_DRAFTS.pop(tg, None)
        return await message.answer(f"✅ Product #{pid} disabled.")

    if draft.step == "bal_tg":
        try:
            target = int(message.text.strip())
        except Exception:
            return await message.answer("❌ tg_id noto‘g‘ri.")
        ensure_user(target)
        draft.payload["target"] = target
        draft.step = "bal_amount"
        return await message.answer("USD miqdor yubor (masalan: 10):")

    if draft.step == "bal_amount":
        try:
            usd = float(message.text.replace(",", ".").strip())
        except Exception:
            return await message.answer("❌ USD noto‘g‘ri.")
        target = int(draft.payload["target"])
        add_balance(target, usd)
        ADMIN_DRAFTS.pop(tg, None)
        return await message.answer(f"✅ User {target} balansiga +${usd:.2f} qo‘shildi.")


# =========================
# BACKGROUND PAYMENT CHECKER
# =========================
async def try_match_order_payment(session: aiohttp.ClientSession, order_row: sqlite3.Row) -> Tuple[bool, Optional[str], int]:
    """
    Returns (matched, txid, confirmations).
    Checks latest received txs to merchant address and validates:
    - tx outputs include LTC_ADDRESS with exact amount_ltc
    - confirmations >= MIN_CONFIRMATIONS
    - tx not used already
    """
    expected = float(order_row["amount_ltc"])

    txs = []
    try:
        txs = await fetch_received_txs(session)
    except Exception:
        return (False, None, 0)

    # Received list includes txid and value. We'll verify via tx details.
    for t in txs[:50]:
        txid = t.get("txid")
        if not txid:
            continue
        if tx_seen(txid):
            continue

        # Fetch details
        try:
            tx_data = await fetch_tx_details(session, txid)
        except Exception:
            continue
        if not tx_data:
            continue

        conf = get_confirmations(tx_data)
        if conf < MIN_CONFIRMATIONS:
            continue

        if tx_pays_exact_amount_to_address(tx_data, LTC_ADDRESS, expected):
            return (True, txid, conf)

        # mark as seen if it's confirmed but doesn't match any order amount
        # (optional). We'll not mark here to allow other order matches.
    return (False, None, 0)

async def payment_watcher():
    await asyncio.sleep(3)
    while True:
        try:
            pending = get_pending_orders(limit=50)
            if pending and LTC_ADDRESS:
                async with aiohttp.ClientSession() as session:
                    # Iterate pending orders and try to match
                    for o in pending:
                        ok, txid, conf = await try_match_order_payment(session, o)
                        if ok and txid:
                            # lock tx
                            mark_tx_seen(txid)
                            mark_order_paid(int(o["id"]), txid)
                            add_balance(int(o["tg_id"]), float(o["amount_usd"]))

                            # notify user
                            try:
                                await bot.send_message(
                                    int(o["tg_id"]),
                                    f"✅ To‘lov tasdiqlandi.\n"
                                    f"Order #{int(o['id'])}\n"
                                    f"TX: {txid}\n"
                                    f"💰 Balansga +${float(o['amount_usd']):.2f}"
                                )
                            except Exception:
                                pass
        except Exception:
            # keep loop alive
            pass

        await asyncio.sleep(CHECK_INTERVAL_SEC)


# =========================
# MAIN
# =========================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    init_db()

    # Seed example products (only if empty)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) c FROM products")
        if cur.fetchone()["c"] == 0:
            add_product("GSH MAROCCO 0.5", "Gadget code delivery after payment.", 25.0, 999)
            add_product("GSH MAROCCO 1", "Gadget code delivery after payment.", 45.0, 999)

    asyncio.create_task(payment_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
