import os
import time
import random
import sqlite3
import asyncio
from typing import Optional, List, Dict, Tuple

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
LTC_ADDRESS = os.getenv("LTC_ADDRESS", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

DEFAULT_USD_PER_LTC = float(os.getenv("DEFAULT_USD_PER_LTC", "100") or "100")
DB_PATH = os.getenv("DB_PATH", "shop.sqlite3")

CHECK_INTERVAL_SEC = 45
MIN_CONFIRMATIONS = 1
AMOUNT_TOL_LTC = 0.00000001  # 1 litoshi

SOCHAIN_RECEIVED = "https://sochain.com/api/v2/get_tx_received/LTC/{address}"
SOCHAIN_TX = "https://sochain.com/api/v2/get_tx/LTC/{txid}"
COINGECKO_RATE = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd"

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.MARKDOWN)
dp = Dispatcher(bot)

# =========================
# DB
# =========================
def db():
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
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            txid TEXT
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
            cur.execute("INSERT INTO users(tg_id, balance_usd, created_at) VALUES(?,?,?)",
                        (tg_id, 0.0, int(time.time())))
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

def add_product(name: str, description: str, price_usd: float, stock: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO products(name, description, price_usd, stock, is_active) VALUES(?,?,?,?,1)",
                    (name, description, price_usd, stock))
        conn.commit()

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

def disable_product(pid: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
        conn.commit()

def create_order(tg_id: int, pid: int, qty: int, amount_usd: float, usd_per_ltc: float, amount_ltc: float) -> int:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders(tg_id, product_id, qty, amount_usd, usd_per_ltc, amount_ltc, status, created_at)
            VALUES(?,?,?,?,?,?, 'pending', ?)
        """, (tg_id, pid, qty, amount_usd, usd_per_ltc, amount_ltc, int(time.time())))
        conn.commit()
        return int(cur.lastrowid)

def get_order(order_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT o.*, p.name as product_name, p.description as product_desc
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.id=?
        """, (order_id,))
        return cur.fetchone()

def pending_orders(limit=50) -> List[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE status='pending' ORDER BY created_at ASC LIMIT ?", (limit,))
        return cur.fetchall()

def mark_order_paid(order_id: int, txid: str):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE orders SET status='paid', paid_at=?, txid=? WHERE id=? AND status='pending'",
                    (int(time.time()), txid, order_id))
        conn.commit()

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
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🛒 Shop", "💰 Balance")
    kb.add("📦 My Orders", "ℹ️ Info")
    return kb

def products_kb():
    rows = []
    for p in list_products(True)[:20]:
        rows.append([types.InlineKeyboardButton(
            text=f"#{p['id']} • {p['name']} • ${float(p['price_usd']):.2f} • stock:{int(p['stock'])}",
            callback_data=f"p:{p['id']}"
        )])
    rows.append([types.InlineKeyboardButton("🔄 Refresh", callback_data="shop:refresh")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

def product_kb(pid: int):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("✅ Buy 1", callback_data=f"buy:{pid}:1")],
        [types.InlineKeyboardButton("⬅️ Back", callback_data="shop:back")]
    ])

def order_kb(order_id: int):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("🔄 Check payment", callback_data=f"order:check:{order_id}")],
        [types.InlineKeyboardButton("⬅️ Back to Shop", callback_data="shop:back")]
    ])

def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS

# =========================
# PAYMENT
# =========================
async def http_get_json(url: str, timeout=15) -> Dict:
    def _get():
        return requests.get(url, timeout=timeout).json()
    return await asyncio.to_thread(_get)

async def usd_per_ltc() -> float:
    try:
        j = await http_get_json(COINGECKO_RATE, timeout=10)
        v = float(j["litecoin"]["usd"])
        return v if v > 0 else DEFAULT_USD_PER_LTC
    except Exception:
        return DEFAULT_USD_PER_LTC

def make_unique_amount(base: float) -> float:
    litoshis = int(round(base * 1e8))
    offset = random.randint(1, 50)
    return (litoshis + offset) / 1e8

def tx_pays_exact_amount(tx_data: Dict, address: str, expected_ltc: float) -> bool:
    outs = tx_data.get("outputs", [])
    for o in outs:
        if o.get("address") == address:
            try:
                v = float(o.get("value"))
            except Exception:
                continue
            if abs(v - expected_ltc) <= AMOUNT_TOL_LTC:
                return True
    return False

def confirmations(tx_data: Dict) -> int:
    try:
        return int(tx_data.get("confirmations", 0))
    except Exception:
        return 0

async def try_match_order(o: sqlite3.Row) -> Tuple[bool, Optional[str], int]:
    expected = float(o["amount_ltc"])
    try:
        received = await http_get_json(SOCHAIN_RECEIVED.format(address=LTC_ADDRESS), timeout=15)
        if received.get("status") != "success":
            return (False, None, 0)
        txs = received["data"].get("txs", [])
    except Exception:
        return (False, None, 0)

    for t in txs[:60]:
        txid = t.get("txid")
        if not txid or tx_seen(txid):
            continue

        try:
            txj = await http_get_json(SOCHAIN_TX.format(txid=txid), timeout=15)
            if txj.get("status") != "success":
                continue
            tx_data = txj.get("data") or {}
        except Exception:
            continue

        conf = confirmations(tx_data)
        if conf < MIN_CONFIRMATIONS:
            continue

        if tx_pays_exact_amount(tx_data, LTC_ADDRESS, expected):
            return (True, txid, conf)

    return (False, None, 0)

async def payment_watcher():
    await asyncio.sleep(3)
    while True:
        try:
            if LTC_ADDRESS:
                for o in pending_orders(50):
                    ok, txid, conf = await try_match_order(o)
                    if ok and txid:
                        mark_tx_seen(txid)
                        mark_order_paid(int(o["id"]), txid)
                        add_balance(int(o["tg_id"]), float(o["amount_usd"]))
                        try:
                            await bot.send_message(
                                int(o["tg_id"]),
                                f"✅ To‘lov tasdiqlandi.\nOrder #{int(o['id'])}\nTX: `{txid}`\n💰 Balansga +${float(o['amount_usd']):.2f}"
                            )
                        except Exception:
                            pass
        except Exception:
            pass

        await asyncio.sleep(CHECK_INTERVAL_SEC)

# =========================
# HANDLERS
# =========================
@dp.message_handler(commands=["start"])
async def start(m: types.Message):
    ensure_user(m.from_user.id)
    await m.answer("✅ Bot ishga tushdi.", reply_markup=main_menu())

@dp.message_handler(commands=["admin"])
async def admin(m: types.Message):
    if not is_admin(m.from_user.id):
        return await m.answer("❌ Admin emas.")
    await m.answer("🧑‍💼 Admin buyruqlar:\n"
                   "`/add_product name | price | stock | desc`\n"
                   "`/disable_product ID`\n"
                   "Misol:\n"
                   "`/add_product GSH 0.5 | 25 | 999 | Gadget`")

@dp.message_handler(commands=["add_product"])
async def addprod(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    try:
        raw = m.get_args()
        name, price, stock, desc = [x.strip() for x in raw.split("|", 3)]
        add_product(name, desc, float(price), int(stock))
        await m.answer("✅ Product qo‘shildi.")
    except Exception:
        await m.answer("❌ Format xato.\nMisol:\n`/add_product GSH 0.5 | 25 | 999 | Gadget`")

@dp.message_handler(commands=["disable_product"])
async def delprod(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    try:
        pid = int(m.get_args().strip())
        disable_product(pid)
        await m.answer(f"✅ Product #{pid} disabled.")
    except Exception:
        await m.answer("❌ Misol: `/disable_product 12`")

@dp.message_handler(lambda m: m.text == "🛒 Shop")
async def shop(m: types.Message):
    if not LTC_ADDRESS:
        return await m.answer("❌ LTC_ADDRESS sozlanmagan (Railway Variables).")
    items = list_products(True)
    if not items:
        return await m.answer("Hozircha tovar yo‘q.")
    await m.answer("🛒 Vitrina:", reply_markup=products_kb())

@dp.message_handler(lambda m: m.text == "💰 Balance")
async def bal(m: types.Message):
    ensure_user(m.from_user.id)
    await m.answer(f"💰 Balans: ${get_balance(m.from_user.id):.2f}")

@dp.message_handler(lambda m: m.text == "ℹ️ Info")
async def info(m: types.Message):
    await m.answer("ℹ️ To‘lov: LTC.\nBot order uchun aniq LTC miqdorini beradi.\nTo‘lov kelgach auto tasdiqlanadi.")

@dp.callback_query_handler(lambda c: c.data == "shop:refresh")
async def cb_refresh(c: types.CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=products_kb())
    await c.answer("OK")

@dp.callback_query_handler(lambda c: c.data == "shop:back")
async def cb_back(c: types.CallbackQuery):
    await c.message.edit_text("🛒 Vitrina:", reply_markup=products_kb())
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("p:"))
async def cb_product(c: types.CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        return await c.answer("Topilmadi", show_alert=True)
    txt = (f"🧾 *{p['name']}*\n"
           f"💵 ${float(p['price_usd']):.2f}\n"
           f"📦 Stock: {int(p['stock'])}\n\n"
           f"{p['description']}")
    await c.message.edit_text(txt, reply_markup=product_kb(pid))
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("buy:"))
async def cb_buy(c: types.CallbackQuery):
    ensure_user(c.from_user.id)
    _, pid_s, qty_s = c.data.split(":")
    pid = int(pid_s)
    qty = int(qty_s)

    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        return await c.answer("Topilmadi", show_alert=True)
    if int(p["stock"]) < qty:
        return await c.answer("Stock yetarli emas", show_alert=True)

    amount_usd = float(p["price_usd"]) * qty
    rate = await usd_per_ltc()
    base_ltc = amount_usd / rate
    amount_ltc = make_unique_amount(base_ltc)

    order_id = create_order(c.from_user.id, pid, qty, amount_usd, rate, amount_ltc)

    msg = (f"🧾 *Order #{order_id}*\n"
           f"📦 {p['name']} x{qty}\n"
           f"💵 ${amount_usd:.2f}\n\n"
           f"➡️ Address: `{LTC_ADDRESS}`\n"
           f"➡️ Amount: *{amount_ltc:.8f} LTC*\n\n"
           f"⚠️ Aynan shu miqdorni yuboring.\n"
           f"Confirmations: {MIN_CONFIRMATIONS}+ bo‘lsa auto tasdiq.")
    await c.message.edit_text(msg, reply_markup=order_kb(order_id))
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("order:check:"))
async def cb_check(c: types.CallbackQuery):
    order_id = int(c.data.split(":")[2])
    o = get_order(order_id)
    if not o or int(o["tg_id"]) != c.from_user.id:
        return await c.answer("Order topilmadi", show_alert=True)
    if o["status"] != "pending":
        return await c.answer(f"Status: {o['status']}", show_alert=True)

    ok, txid, conf = await try_match_order(o)
    if ok and txid:
        mark_tx_seen(txid)
        mark_order_paid(order_id, txid)
        add_balance(c.from_user.id, float(o["amount_usd"]))
        await c.message.edit_text(
            f"✅ *PAID*\nOrder #{order_id}\nTX: `{txid}`\nConf: {conf}\n💰 +${float(o['amount_usd']):.2f}",
            reply_markup=order_kb(order_id)
        )
        await c.answer("Paid ✅", show_alert=True)
    else:
        await c.answer("Hali to‘lov topilmadi", show_alert=True)

# =========================
# STARTUP
# =========================
async def on_startup(_):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    init_db()

    # Seed products if empty
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) c FROM products")
        if cur.fetchone()["c"] == 0:
            add_product("GSH MAROCCO 0.5", "Gadget code delivery after payment.", 25.0, 999)
            add_product("GSH MAROCCO 1", "Gadget code delivery after payment.", 45.0, 999)

    asyncio.create_task(payment_watcher())

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
