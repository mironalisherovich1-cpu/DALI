import os
import time
import math
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, Tuple, List

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from bip_utils import Bip84, Bip84Coins, Bip44Changes

# =========================
# CONFIG
# =========================
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")
LTC_XPUB = (os.getenv("LTC_XPUB") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")
if not LTC_XPUB:
    raise RuntimeError("LTC_XPUB env missing (zpub/xpub)")

DB_PATH = os.getenv("DB_PATH", "shop.sqlite3")

# Contacts (RU)
OBMENNIKI_USERNAME = "ltc_exp"
SUPPORT_USERNAME = "qwerty7777jass"
OPERATOR_USERNAME = "qwerty7777jass"

CITIES = ["Buxoro", "Navoiy", "Samarqand", "Toshkent"]

# Deposit scanner
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60") or "60")
MIN_CONFIRMATIONS = int(os.getenv("MIN_CONFIRMATIONS", "1") or "1")

# BlockCypher LTC address endpoint
BC_ADDR = "https://api.blockcypher.com/v1/ltc/main/addrs/{address}"

# =========================
# BOT
# =========================
bot = Bot(BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot, storage=MemoryStorage())

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
            city TEXT NOT NULL,
            addr_index INTEGER NOT NULL,
            ltc_address TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS balances(
            tg_id INTEGER PRIMARY KEY,
            ltc REAL NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price_ltc REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            delivery_photo_url TEXT,
            delivery_text TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            amount_ltc REAL NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            paid_at INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS credited_utx(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            value_sat INTEGER NOT NULL,
            credited_at INTEGER NOT NULL,
            UNIQUE(address, tx_hash, value_sat)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            rating_product INTEGER NOT NULL,
            rating_service INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """)

        conn.commit()

        # Seed products if empty (NEUTRAL default)
        cur.execute("SELECT COUNT(*) AS c FROM products")
        if int(cur.fetchone()["c"]) == 0:
            cur.executemany("""
                INSERT INTO products(name, price_ltc, is_active, delivery_photo_url, delivery_text)
                VALUES(?,?,?,?,?)
            """, [
                ("Product A (0.5)", 0.0035, 1, "", "Инструкция: ..."),
                ("Product A (1)",   0.0063, 1, "", "Инструкция: ..."),
                ("Product B",       0.0056, 1, "", "Инструкция: ..."),
                ("Product C (5 шт)",0.0084, 1, "", "Инструкция: ..."),
            ])
            conn.commit()


# =========================
# HD ADDRESS DERIVATION
# =========================
def derive_ltc_address_from_xpub(index: int) -> str:
    """
    Derive BIP84 (native segwit) address from xpub/zpub for Litecoin.
    Works with zpub/xpub provided by Electrum-LTC.
    """
    ctx = Bip84.FromExtendedKey(LTC_XPUB, Bip84Coins.LITECOIN)
    addr = ctx.Change(Bip44Changes.CHAIN_EXT).AddressIndex(index).PublicKey().ToAddress()
    return addr


def next_address_index() -> int:
    """Get next incremental index for new user."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(addr_index) AS mx FROM users")
        row = cur.fetchone()
        mx = row["mx"]
        return int(mx) + 1 if mx is not None else 0


# =========================
# USER / BALANCE
# =========================
def is_admin(tg_id: int) -> bool:
    return ADMIN_ID > 0 and tg_id == ADMIN_ID


def ensure_user(tg_id: int):
    now = int(time.time())
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
        if cur.fetchone() is None:
            idx = next_address_index()
            addr = derive_ltc_address_from_xpub(idx)
            cur.execute("""
                INSERT INTO users(tg_id, city, addr_index, ltc_address, created_at)
                VALUES(?,?,?,?,?)
            """, (tg_id, CITIES[0], idx, addr, now))
            cur.execute("""
                INSERT INTO balances(tg_id, ltc, updated_at) VALUES(?,?,?)
            """, (tg_id, 0.0, now))
            conn.commit()


def get_user(tg_id: int) -> sqlite3.Row:
    ensure_user(tg_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return cur.fetchone()


def set_city(tg_id: int, city: str):
    with db() as conn:
        conn.execute("UPDATE users SET city=? WHERE tg_id=?", (city, tg_id))
        conn.commit()


def get_balance_ltc(tg_id: int) -> float:
    ensure_user(tg_id)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ltc FROM balances WHERE tg_id=?", (tg_id,))
        return float(cur.fetchone()["ltc"])


def add_balance_ltc(tg_id: int, amount: float):
    now = int(time.time())
    with db() as conn:
        conn.execute("UPDATE balances SET ltc=ltc+?, updated_at=? WHERE tg_id=?", (amount, now, tg_id))
        conn.commit()


def sub_balance_ltc(tg_id: int, amount: float):
    now = int(time.time())
    with db() as conn:
        conn.execute("UPDATE balances SET ltc=ltc-?, updated_at=? WHERE tg_id=?", (amount, now, tg_id))
        conn.commit()


# =========================
# PRODUCTS / ORDERS
# =========================
def list_products(active_only=True) -> List[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        if active_only:
            cur.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id ASC")
        else:
            cur.execute("SELECT * FROM products ORDER BY id ASC")
        return cur.fetchall()


def get_product(pid: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE id=?", (pid,))
        return cur.fetchone()


def toggle_product(pid: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT is_active FROM products WHERE id=?", (pid,))
        row = cur.fetchone()
        if not row:
            return
        new_val = 0 if int(row["is_active"]) == 1 else 1
        cur.execute("UPDATE products SET is_active=? WHERE id=?", (new_val, pid))
        conn.commit()


def set_price(pid: int, price_ltc: float):
    with db() as conn:
        conn.execute("UPDATE products SET price_ltc=? WHERE id=?", (price_ltc, pid))
        conn.commit()


def set_delivery(pid: int, photo_url: str, text: str):
    with db() as conn:
        conn.execute("UPDATE products SET delivery_photo_url=?, delivery_text=? WHERE id=?", (photo_url, text, pid))
        conn.commit()


def add_product(name: str, price_ltc: float):
    with db() as conn:
        conn.execute("INSERT INTO products(name, price_ltc, is_active, delivery_photo_url, delivery_text) VALUES(?,?,?,?,?)",
                     (name, price_ltc, 1, "", "Инструкция: ..."))
        conn.commit()


def create_order_paid(tg_id: int, pid: int, amount_ltc: float) -> int:
    now = int(time.time())
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders(tg_id, product_id, amount_ltc, status, created_at, paid_at)
            VALUES(?,?,?, 'PAID', ?, ?)
        """, (tg_id, pid, amount_ltc, now, now))
        conn.commit()
        return int(cur.lastrowid)


def user_orders(tg_id: int, limit: int = 15) -> List[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT o.id, o.amount_ltc, o.status, o.created_at, p.name as product_name
            FROM orders o
            JOIN products p ON p.id=o.product_id
            WHERE o.tg_id=?
            ORDER BY o.id DESC
            LIMIT ?
        """, (tg_id, limit))
        return cur.fetchall()


def has_purchase(tg_id: int, pid: int) -> bool:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM orders WHERE tg_id=? AND product_id=? AND status='PAID' LIMIT 1", (tg_id, pid))
        return cur.fetchone() is not None


# =========================
# REVIEWS
# =========================
def review_count() -> int:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM reviews")
        return int(cur.fetchone()["c"])


def get_review_page(page: int, per_page: int = 1) -> Tuple[Optional[sqlite3.Row], int, int]:
    total = review_count()
    if total == 0:
        return None, 0, 0
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT r.*, p.name as product_name
            FROM reviews r
            JOIN products p ON p.id=r.product_id
            ORDER BY r.id DESC
            LIMIT ? OFFSET ?
        """, (per_page, offset))
        row = cur.fetchone()
    return row, page, pages


def mask_user(tg_id: int) -> str:
    s = str(tg_id)
    if len(s) <= 6:
        return s
    return s[:3] + "****" + s[-2:]


def add_review(tg_id: int, pid: int, rp: int, rs: int, text: str):
    now = int(time.time())
    with db() as conn:
        conn.execute("""
            INSERT INTO reviews(tg_id, product_id, rating_product, rating_service, text, created_at)
            VALUES(?,?,?,?,?,?)
        """, (tg_id, pid, rp, rs, text.strip(), now))
        conn.commit()


# =========================
# BLOCKCHAIN CREDITING
# =========================
def sat_to_ltc(sat: int) -> float:
    return sat / 100_000_000.0


def fetch_incoming_outputs(address: str) -> List[dict]:
    """
    BlockCypher txrefs:
    incoming outputs: tx_input_n == -1
    fields: tx_hash, value, confirmations
    """
    url = BC_ADDR.format(address=address)
    r = requests.get(url, params={"limit": 50}, timeout=20)
    r.raise_for_status()
    data = r.json()
    txrefs = data.get("txrefs", []) or []
    res = []
    for t in txrefs:
        if int(t.get("tx_input_n", 0)) != -1:
            continue
        conf = int(t.get("confirmations", 0))
        if conf < MIN_CONFIRMATIONS:
            continue
        res.append({
            "tx_hash": t.get("tx_hash"),
            "value": int(t.get("value", 0)),
            "confirmations": conf
        })
    return res


def credit_new_incoming_for_user(tg_id: int) -> int:
    """
    Credits all new (not yet credited) incoming outputs to user's personal address.
    Returns number of credited outputs.
    """
    user = get_user(tg_id)
    addr = user["ltc_address"]
    try:
        outs = fetch_incoming_outputs(addr)
    except Exception:
        return 0

    credited = 0
    now = int(time.time())
    with db() as conn:
        cur = conn.cursor()
        for o in outs:
            tx = o["tx_hash"]
            val_sat = o["value"]
            if val_sat <= 0:
                continue
            try:
                cur.execute("""
                    INSERT INTO credited_utx(tg_id, address, tx_hash, value_sat, credited_at)
                    VALUES(?,?,?,?,?)
                """, (tg_id, addr, tx, val_sat, now))
                # if inserted => credit balance
                add_balance_ltc(tg_id, sat_to_ltc(val_sat))
                credited += 1
            except sqlite3.IntegrityError:
                # already credited
                continue
        conn.commit()
    return credited


async def deposit_watcher_loop():
    await asyncio.sleep(3)
    while True:
        try:
            # scan last active users (simple approach)
            with db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT tg_id FROM users ORDER BY created_at DESC LIMIT 200")
                tg_ids = [int(r["tg_id"]) for r in cur.fetchall()]

            for uid in tg_ids:
                n = await asyncio.to_thread(credit_new_incoming_for_user, uid)
                if n > 0:
                    # notify user balance updated
                    bal = get_balance_ltc(uid)
                    try:
                        await bot.send_message(
                            uid,
                            f"✅ <b>Пополнение зачислено</b>\n"
                            f"Новых транзакций: <b>{n}</b>\n"
                            f"Текущий баланс: <b>{bal:.8f} LTC</b>"
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        await asyncio.sleep(SCAN_INTERVAL_SEC)


# =========================
# UI
# =========================
def main_menu_kb(admin: bool = False) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("👤 Мой профиль")
    kb.row("🛍 Витрина", "💰 Баланс")
    kb.row("⭐ Отзывы", "💱 Обменники")
    kb.row("🆘 Помощь")
    if admin:
        kb.row("🛠 Админ-панель")
    return kb


def profile_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("🛍 История покупок", callback_data="profile:orders"),
        types.InlineKeyboardButton("🔄 Изменить город", callback_data="city:change"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
    )
    return ikb


def city_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=2)
    for c in CITIES:
        ikb.insert(types.InlineKeyboardButton(c, callback_data=f"city:set:{c}"))
    ikb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="city:back_profile"))
    return ikb


def shop_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    for p in list_products(True):
        ikb.add(types.InlineKeyboardButton(f"{p['name']} — {float(p['price_ltc']):.8f} LTC", callback_data=f"p:{p['id']}"))
    ikb.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"))
    return ikb


def product_kb(pid: int) -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("✅ Купить (с баланса)", callback_data=f"buy:{pid}"),
        types.InlineKeyboardButton("⬅️ Назад к витрине", callback_data="shop:back")
    )
    return ikb


def balance_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("🔄 Проверить пополнение", callback_data="bal:check"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu")
    )
    return ikb


def reviews_nav_kb(page: int, pages: int) -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=3)
    ikb.row(
        types.InlineKeyboardButton("◀️", callback_data=f"rev:prev:{page}"),
        types.InlineKeyboardButton(f"{page}/{pages}", callback_data="rev:noop"),
        types.InlineKeyboardButton("▶️", callback_data=f"rev:next:{page}"),
    )
    ikb.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"))
    return ikb


def admin_menu_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("📦 Товары (управление)", callback_data="adm:products"),
        types.InlineKeyboardButton("➕ Добавить товар", callback_data="adm:add"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
    )
    return ikb


def admin_products_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    for p in list_products(False):
        status = "✅" if int(p["is_active"]) == 1 else "⛔️"
        ikb.add(types.InlineKeyboardButton(
            f"{status} #{p['id']} {p['name']} ({float(p['price_ltc']):.8f} LTC)",
            callback_data=f"adm:p:{p['id']}"
        ))
    ikb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="adm:back"))
    return ikb


def admin_product_actions_kb(pid: int) -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("💲 Изменить цену", callback_data=f"adm:price:{pid}"),
        types.InlineKeyboardButton("🖼/📝 Delivery (фото+текст)", callback_data=f"adm:delivery:{pid}"),
        types.InlineKeyboardButton("🔁 Toggle ON/OFF", callback_data=f"adm:toggle:{pid}"),
        types.InlineKeyboardButton("⬅️ К списку товаров", callback_data="adm:products"),
    )
    return ikb


def after_purchase_kb(pid: int) -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("✍️ Оставить отзыв", callback_data=f"rev:add:{pid}"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
    )
    return ikb


# =========================
# STATES
# =========================
class AdminAdd(StatesGroup):
    name = State()
    price = State()

class AdminPrice(StatesGroup):
    price = State()

class AdminDelivery(StatesGroup):
    photo_url = State()
    text = State()

class ReviewFlow(StatesGroup):
    rating_product = State()
    rating_service = State()
    text = State()


# =========================
# HANDLERS
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(m: types.Message):
    ensure_user(m.from_user.id)
    await m.answer("✅ <b>Бот запущен.</b>\nВыберите пункт меню:", reply_markup=main_menu_kb(is_admin(m.from_user.id)))


@dp.callback_query_handler(lambda c: c.data == "go:menu")
async def cb_go_menu(c: types.CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        pass
    await bot.send_message(c.from_user.id, "🏠 <b>Главное меню</b>", reply_markup=main_menu_kb(is_admin(c.from_user.id)))
    await c.answer()


# ---------- PROFILE ----------
@dp.message_handler(lambda m: m.text == "👤 Мой профиль")
async def profile(m: types.Message):
    u = get_user(m.from_user.id)
    bal = get_balance_ltc(m.from_user.id)
    txt = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{m.from_user.id}</code>\n"
        f"🏙 Город: <b>{u['city']}</b>\n"
        f"🏦 Ваш LTC-адрес для пополнения:\n<code>{u['ltc_address']}</code>\n\n"
        f"💰 Баланс: <b>{bal:.8f} LTC</b>"
    )
    await m.answer(txt, reply_markup=profile_kb())


@dp.callback_query_handler(lambda c: c.data == "profile:orders")
async def cb_profile_orders(c: types.CallbackQuery):
    rows = user_orders(c.from_user.id, 15)
    if not rows:
        await c.answer("Покупок пока нет", show_alert=True)
        return
    lines = ["🛍 <b>История покупок</b>\n"]
    for r in rows:
        dt = datetime.fromtimestamp(int(r["created_at"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"• #{r['id']} — <b>{r['product_name']}</b>\n  {float(r['amount_ltc']):.8f} LTC • {r['status']} • {dt}")
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(types.InlineKeyboardButton("⬅️ Назад в профиль", callback_data="city:back_profile"))
    await c.message.edit_text("\n".join(lines), reply_markup=ikb)
    await c.answer()


@dp.callback_query_handler(lambda c: c.data == "city:change")
async def cb_city_change(c: types.CallbackQuery):
    await c.message.edit_text("🏙 <b>Выберите город:</b>", reply_markup=city_kb())
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("city:set:"))
async def cb_city_set(c: types.CallbackQuery):
    city = c.data.split(":", 2)[2]
    if city not in CITIES:
        await c.answer("Неверный город", show_alert=True)
        return
    set_city(c.from_user.id, city)
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("⬅️ Назад в профиль", callback_data="city:back_profile"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
    )
    await c.message.edit_text(f"✅ Город изменён: <b>{city}</b>", reply_markup=ikb)
    await c.answer()


@dp.callback_query_handler(lambda c: c.data == "city:back_profile")
async def cb_back_profile(c: types.CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        pass
    # resend profile
    u = get_user(c.from_user.id)
    bal = get_balance_ltc(c.from_user.id)
    txt = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{c.from_user.id}</code>\n"
        f"🏙 Город: <b>{u['city']}</b>\n"
        f"🏦 Ваш LTC-адрес для пополнения:\n<code>{u['ltc_address']}</code>\n\n"
        f"💰 Баланс: <b>{bal:.8f} LTC</b>"
    )
    await bot.send_message(c.from_user.id, txt, reply_markup=profile_kb())
    await c.answer()


# ---------- BALANCE ----------
@dp.message_handler(lambda m: m.text == "💰 Баланс")
async def balance(m: types.Message):
    u = get_user(m.from_user.id)
    bal = get_balance_ltc(m.from_user.id)
    txt = (
        f"💰 <b>Баланс</b>\n\n"
        f"Текущий: <b>{bal:.8f} LTC</b>\n\n"
        f"➕ Для пополнения отправьте LTC на ваш персональный адрес:\n"
        f"<code>{u['ltc_address']}</code>\n\n"
        f"После отправки нажмите «Проверить пополнение»."
    )
    await m.answer(txt, reply_markup=balance_kb())


@dp.callback_query_handler(lambda c: c.data == "bal:check")
async def cb_balance_check(c: types.CallbackQuery):
    n = await asyncio.to_thread(credit_new_incoming_for_user, c.from_user.id)
    bal = get_balance_ltc(c.from_user.id)
    if n > 0:
        await c.answer("Зачислено ✅", show_alert=True)
        await c.message.edit_text(
            f"✅ <b>Пополнение зачислено</b>\nНовых транзакций: <b>{n}</b>\nБаланс: <b>{bal:.8f} LTC</b>",
            reply_markup=balance_kb()
        )
    else:
        await c.answer("Пока нет новых поступлений", show_alert=True)
    await c.answer()


# ---------- SHOP ----------
@dp.message_handler(lambda m: m.text == "🛍 Витрина")
async def shop(m: types.Message):
    await m.answer("🛍 <b>Витрина</b>\nВыберите товар:", reply_markup=shop_kb())


@dp.callback_query_handler(lambda c: c.data == "shop:back")
async def cb_shop_back(c: types.CallbackQuery):
    await c.message.edit_text("🛍 <b>Витрина</b>\nВыберите товар:", reply_markup=shop_kb())
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("p:"))
async def cb_product(c: types.CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await c.answer("Товар недоступен", show_alert=True)
        return
    txt = (
        f"🛍 <b>{p['name']}</b>\n"
        f"💳 Цена: <b>{float(p['price_ltc']):.8f} LTC</b>\n\n"
        f"Покупка списывает средства с вашего баланса."
    )
    await c.message.edit_text(txt, reply_markup=product_kb(pid))
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("buy:"))
async def cb_buy(c: types.CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await c.answer("Товар недоступен", show_alert=True)
        return

    price = float(p["price_ltc"])
    bal = get_balance_ltc(c.from_user.id)

    if bal + 1e-12 < price:
        u = get_user(c.from_user.id)
        await c.answer("Недостаточно средств", show_alert=True)
        await c.message.edit_text(
            f"❌ <b>Недостаточно средств</b>\n\n"
            f"Цена: <b>{price:.8f} LTC</b>\n"
            f"Баланс: <b>{bal:.8f} LTC</b>\n\n"
            f"Пополните баланс на ваш адрес:\n<code>{u['ltc_address']}</code>\n"
            f"Затем нажмите «Проверить пополнение» в разделе Баланс.",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("💰 Баланс", callback_data="go:balance"),
                types.InlineKeyboardButton("⬅️ Назад", callback_data=f"p:{pid}")
            )
        )
        return

    # Deduct & create paid order
    sub_balance_ltc(c.from_user.id, price)
    order_id = create_order_paid(c.from_user.id, pid, price)

    # Delivery
    delivery_text = (p["delivery_text"] or "").strip()
    if not delivery_text:
        delivery_text = "✅ Оплата подтверждена. Инструкция будет добавлена админом."

    caption = (
        f"✅ <b>Покупка успешна</b>\n"
        f"🧾 Заказ: <b>#{order_id}</b>\n"
        f"📦 Товар: <b>{p['name']}</b>\n"
        f"💳 Списано: <b>{price:.8f} LTC</b>\n\n"
        f"{delivery_text}"
    )

    photo_url = (p["delivery_photo_url"] or "").strip()
    try:
        if photo_url:
            await bot.send_photo(c.from_user.id, photo=photo_url, caption=caption)
        else:
            await bot.send_message(c.from_user.id, caption)
    except Exception:
        await bot.send_message(c.from_user.id, caption)

    # Ask for review
    await bot.send_message(
        c.from_user.id,
        "⭐ Хотите оставить отзыв после покупки?",
        reply_markup=after_purchase_kb(pid)
    )

    await c.message.edit_text("✅ Готово. Сообщение с доставкой отправлено в чат.", reply_markup=types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
        types.InlineKeyboardButton("🛍 Витрина", callback_data="shop:back")
    ))
    await c.answer()


@dp.callback_query_handler(lambda c: c.data == "go:balance")
async def cb_go_balance(c: types.CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        pass
    u = get_user(c.from_user.id)
    bal = get_balance_ltc(c.from_user.id)
    txt = (
        f"💰 <b>Баланс</b>\n\n"
        f"Текущий: <b>{bal:.8f} LTC</b>\n\n"
        f"➕ Для пополнения отправьте LTC на ваш персональный адрес:\n"
        f"<code>{u['ltc_address']}</code>\n\n"
        f"После отправки нажмите «Проверить пополнение»."
    )
    await bot.send_message(c.from_user.id, txt, reply_markup=balance_kb())
    await c.answer()


# ---------- REVIEWS VIEW ----------
@dp.message_handler(lambda m: m.text == "⭐ Отзывы")
async def reviews(m: types.Message):
    row, page, pages = get_review_page(1, 1)
    if not row:
        await m.answer("⭐ Отзывов пока нет.")
        return
    txt = (
        f"⭐ <b>Отзыв</b>\n\n"
        f"👤 {mask_user(int(row['tg_id']))}\n"
        f"📦 <b>{row['product_name']}</b>\n"
        f"⭐ Товар: <b>{int(row['rating_product'])}/5</b>\n"
        f"⭐ Сервис: <b>{int(row['rating_service'])}/5</b>\n\n"
        f"{row['text']}"
    )
    await m.answer(txt, reply_markup=reviews_nav_kb(page, pages))


@dp.callback_query_handler(lambda c: c.data.startswith("rev:"))
async def cb_reviews_nav(c: types.CallbackQuery):
    parts = c.data.split(":")
    action = parts[1]
    cur_page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1

    total = review_count()
    if total == 0:
        await c.answer("Отзывов нет", show_alert=True)
        return

    if action == "prev":
        new_page = max(1, cur_page - 1)
    elif action == "next":
        new_page = cur_page + 1
    else:
        await c.answer()
        return

    row, page, pages = get_review_page(new_page, 1)
    if not row:
        await c.answer("Нет данных", show_alert=True)
        return

    txt = (
        f"⭐ <b>Отзыв</b>\n\n"
        f"👤 {mask_user(int(row['tg_id']))}\n"
        f"📦 <b>{row['product_name']}</b>\n"
        f"⭐ Товар: <b>{int(row['rating_product'])}/5</b>\n"
        f"⭐ Сервис: <b>{int(row['rating_service'])}/5</b>\n\n"
        f"{row['text']}"
    )
    await c.message.edit_text(txt, reply_markup=reviews_nav_kb(page, pages))
    await c.answer()


# ---------- REVIEW ADD (after purchase only) ----------
@dp.callback_query_handler(lambda c: c.data.startswith("rev:add:"))
async def cb_review_add(c: types.CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    if not has_purchase(c.from_user.id, pid):
        await c.answer("Отзыв доступен только после покупки", show_alert=True)
        return

    await state.update_data(pid=pid)
    await c.message.edit_text(
        "⭐ <b>Оставить отзыв</b>\n\nОцените товар (1-5):",
        reply_markup=types.InlineKeyboardMarkup(row_width=5).row(
            *[types.InlineKeyboardButton(str(i), callback_data=f"rev:rp:{i}") for i in range(1, 6)]
        )
    )
    await ReviewFlow.rating_product.set()
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("rev:rp:"), state=ReviewFlow.rating_product)
async def cb_rev_rating_product(c: types.CallbackQuery, state: FSMContext):
    rp = int(c.data.split(":")[2])
    if rp < 1 or rp > 5:
        await c.answer("Ошибка", show_alert=True)
        return
    await state.update_data(rp=rp)
    await c.message.edit_text(
        "⭐ Оцените сервис (1-5):",
        reply_markup=types.InlineKeyboardMarkup(row_width=5).row(
            *[types.InlineKeyboardButton(str(i), callback_data=f"rev:rs:{i}") for i in range(1, 6)]
        )
    )
    await ReviewFlow.rating_service.set()
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("rev:rs:"), state=ReviewFlow.rating_service)
async def cb_rev_rating_service(c: types.CallbackQuery, state: FSMContext):
    rs = int(c.data.split(":")[2])
    if rs < 1 or rs > 5:
        await c.answer("Ошибка", show_alert=True)
        return
    await state.update_data(rs=rs)
    await c.message.edit_text("✍️ Напишите текст отзыва (1-3 предложения):")
    await ReviewFlow.text.set()
    await c.answer()


@dp.message_handler(state=ReviewFlow.text)
async def msg_rev_text(m: types.Message, state: FSMContext):
    data = await state.get_data()
    pid = int(data["pid"])
    rp = int(data["rp"])
    rs = int(data["rs"])
    text = (m.text or "").strip()
    if len(text) < 3:
        await m.answer("Слишком коротко. Напишите чуть подробнее.")
        return

    add_review(m.from_user.id, pid, rp, rs, text)
    await state.finish()

    await m.answer("✅ Отзыв добавлен. Спасибо!", reply_markup=main_menu_kb(is_admin(m.from_user.id)))


# ---------- OBMENNIKI / HELP ----------
@dp.message_handler(lambda m: m.text == "💱 Обменники")
async def obmenniki(m: types.Message):
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(types.InlineKeyboardButton("↗️ ltc_exp", url=f"https://t.me/{OBMENNIKI_USERNAME}"))
    await m.answer("💱 <b>Проверенный обменник:</b>", reply_markup=ikb)


@dp.message_handler(lambda m: m.text == "🆘 Помощь")
async def help_menu(m: types.Message):
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("Support", url=f"https://t.me/{SUPPORT_USERNAME}"),
        types.InlineKeyboardButton("Operator", url=f"https://t.me/{OPERATOR_USERNAME}"),
    )
    await m.answer("🆘 <b>Поддержка</b>\nВыберите, куда написать:", reply_markup=ikb)


# ---------- ADMIN PANEL ----------
@dp.message_handler(lambda m: m.text == "🛠 Админ-панель")
async def admin_panel(m: types.Message):
    if not is_admin(m.from_user.id):
        return await m.answer("❌ Нет доступа.")
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())


@dp.callback_query_handler(lambda c: c.data == "adm:back")
async def cb_adm_back(c: types.CallbackQuery):
    await c.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())
    await c.answer()


@dp.callback_query_handler(lambda c: c.data == "adm:products")
async def cb_adm_products(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await c.message.edit_text("📦 <b>Товары</b> (нажмите товар):", reply_markup=admin_products_kb())
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm:p:"))
async def cb_adm_product(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    p = get_product(pid)
    if not p:
        await c.answer("Не найдено", show_alert=True)
        return
    status = "ON ✅" if int(p["is_active"]) == 1 else "OFF ⛔️"
    photo = "YES" if (p["delivery_photo_url"] or "").strip() else "NO"
    text = "YES" if (p["delivery_text"] or "").strip() else "NO"
    msg = (
        f"📦 <b>Товар #{pid}</b>\n"
        f"Название: <b>{p['name']}</b>\n"
        f"Цена: <b>{float(p['price_ltc']):.8f} LTC</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"Delivery photo: <b>{photo}</b>\n"
        f"Delivery text: <b>{text}</b>"
    )
    await c.message.edit_text(msg, reply_markup=admin_product_actions_kb(pid))
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm:toggle:"))
async def cb_adm_toggle(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    toggle_product(pid)
    await c.answer("ОК")
    # refresh product card
    await cb_adm_product(types.CallbackQuery(
        id=c.id, from_user=c.from_user, chat_instance=c.chat_instance,
        message=c.message, data=f"adm:p:{pid}"
    ))


@dp.callback_query_handler(lambda c: c.data.startswith("adm:price:"))
async def cb_adm_price(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    await state.update_data(pid=pid)
    await c.message.edit_text(f"💲 Введите новую цену (LTC) для товара #{pid}.\nПример: <code>0.0042</code>")
    await AdminPrice.price.set()
    await c.answer()


@dp.message_handler(state=AdminPrice.price)
async def msg_adm_price(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return
    data = await state.get_data()
    pid = int(data["pid"])
    try:
        price = float((m.text or "").replace(",", ".").strip())
        if price <= 0:
            raise ValueError()
    except Exception:
        await m.answer("❌ Неверный формат. Пример: 0.0042")
        return

    set_price(pid, price)
    await state.finish()
    await m.answer("✅ Цена обновлена.", reply_markup=main_menu_kb(True))


@dp.callback_query_handler(lambda c: c.data.startswith("adm:delivery:"))
async def cb_adm_delivery(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    await state.update_data(pid=pid)
    await c.message.edit_text(
        f"🖼 Введите URL картинки для delivery товара #{pid}\n"
        f"• если не нужно — отправьте <code>-</code>"
    )
    await AdminDelivery.photo_url.set()
    await c.answer()


@dp.message_handler(state=AdminDelivery.photo_url)
async def msg_adm_delivery_photo(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return
    url = (m.text or "").strip()
    if url != "-" and url and not (url.startswith("http://") or url.startswith("https://")):
        await m.answer("❌ URL должен начинаться с http/https, или отправьте '-'")
        return
    await state.update_data(photo_url="" if url == "-" else url)
    await m.answer("📝 Введите текст инструкции (delivery text). Можно 1-10 строк:")
    await AdminDelivery.text.set()


@dp.message_handler(state=AdminDelivery.text)
async def msg_adm_delivery_text(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return
    data = await state.get_data()
    pid = int(data["pid"])
    photo_url = (data.get("photo_url") or "").strip()
    text = (m.text or "").strip()
    if len(text) < 1:
        await m.answer("❌ Текст пустой.")
        return
    set_delivery(pid, photo_url, text)
    await state.finish()
    await m.answer("✅ Delivery обновлён.", reply_markup=main_menu_kb(True))


@dp.callback_query_handler(lambda c: c.data == "adm:add")
async def cb_adm_add(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await c.message.edit_text("➕ Введите название товара:")
    await AdminAdd.name.set()
    await c.answer()


@dp.message_handler(state=AdminAdd.name)
async def msg_adm_add_name(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return
    name = (m.text or "").strip()
    if len(name) < 2:
        await m.answer("Слишком коротко. Введите нормальное название.")
        return
    await state.update_data(name=name)
    await m.answer("💲 Введите цену (LTC). Пример: 0.0042")
    await AdminAdd.price.set()


@dp.message_handler(state=AdminAdd.price)
async def msg_adm_add_price(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return
    try:
        price = float((m.text or "").replace(",", ".").strip())
        if price <= 0:
            raise ValueError()
    except Exception:
        await m.answer("❌ Неверный формат. Пример: 0.0042")
        return
    data = await state.get_data()
    add_product(data["name"], price)
    await state.finish()
    await m.answer("✅ Товар добавлен.", reply_markup=main_menu_kb(True))


# ---------- FALLBACK ----------
@dp.message_handler()
async def fallback(m: types.Message):
    ensure_user(m.from_user.id)
    await m.answer("Выберите пункт меню 👇", reply_markup=main_menu_kb(is_admin(m.from_user.id)))


# =========================
# STARTUP
# =========================
async def on_startup(_):
    init_db()
    asyncio.create_task(deposit_watcher_loop())


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
