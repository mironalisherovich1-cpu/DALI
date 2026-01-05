import os
import re
import time
import uuid
import math
import sqlite3
import random
import logging
from datetime import datetime, timezone

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputFile
)
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor

# =========================
# CONFIG
# =========================
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
LTC_WALLET = os.getenv("LTC_WALLET", "").strip()

START_IMAGE_URL = os.getenv("START_IMAGE_URL", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/LTCEXP").strip()
OPERATOR_URL = os.getenv("OPERATOR_URL", "https://t.me/LTCEXP").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()

MIN_CONFIRMATIONS = int(os.getenv("MIN_CONFIRMATIONS", "1"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

CITIES = ["Buxoro", "Navoiy", "Samarqand", "Toshkent"]
OBMENNIKI_USERNAME = "LTCEXP"  # @LTCEXP

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env yo'q")
if not LTC_WALLET:
    raise RuntimeError("LTC_WALLET env yo'q")
if ADMIN_ID <= 0:
    logging.warning("ADMIN_ID env yo'q yoki noto'g'ri. Admin panel ishlamaydi.")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE,
        city TEXT DEFAULT 'Buxoro',
        discount REAL DEFAULT 0.0,
        balance_usd REAL DEFAULT 0.0,
        balance_ltc REAL DEFAULT 0.0,
        invited_by INTEGER,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price_usd REAL,
        city TEXT,
        photo_url TEXT,
        description TEXT,
        is_active INTEGER DEFAULT 1
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_code TEXT UNIQUE,
        tg_id INTEGER,
        product_id INTEGER,
        amount_usd REAL,
        ltc_amount REAL,
        ltc_address TEXT,
        status TEXT DEFAULT 'PENDING',
        txid TEXT,
        created_at TEXT,
        paid_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reviews(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER,
        product_name TEXT,
        rating_product INTEGER,
        rating_service INTEGER,
        text TEXT,
        purchased_at TEXT,
        published_at TEXT
    )
    """)
    conn.commit()

    # default products (2 ta, keyin admin orqali yana qo'shasan)
    cur.execute("SELECT COUNT(*) as c FROM products")
    if cur.fetchone()["c"] == 0:
        defaults = [
            ("GSH MAROCCO 0.5", 25.0, "Buxoro", "", "Gadjet. Tez yetkazib berish. (Demo)"),
            ("GSH MAROCCO 1", 45.0, "Buxoro", "", "Gadjet. Premium variant. (Demo)"),
        ]
        cur.executemany(
            "INSERT INTO products(name, price_usd, city, photo_url, description) VALUES(?,?,?,?,?)",
            defaults
        )
        conn.commit()
    conn.close()

def get_user(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users(tg_id, city, discount, balance_usd, balance_ltc, created_at) VALUES(?,?,?,?,?,?)",
            (tg_id, CITIES[0], 0.0, 0.0, 0.0, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
    conn.close()
    return row

def set_user_city(tg_id: int, city: str):
    conn = db()
    conn.execute("UPDATE users SET city=? WHERE tg_id=?", (city, tg_id))
    conn.commit()
    conn.close()

def set_user_discount(tg_id: int, discount: float):
    conn = db()
    conn.execute("UPDATE users SET discount=? WHERE tg_id=?", (discount, tg_id))
    conn.commit()
    conn.close()

# =========================
# PRICE (LTC/USD)
# =========================
_price_cache = {"ts": 0, "price": None}

def get_ltc_usd_price() -> float:
    # cache 60s
    now = time.time()
    if _price_cache["price"] and now - _price_cache["ts"] < 60:
        return _price_cache["price"]

    # CoinGecko
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        r = requests.get(url, params={"ids": "litecoin", "vs_currencies": "usd"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        price = float(data["litecoin"]["usd"])
        _price_cache.update({"ts": now, "price": price})
        return price
    except Exception as e:
        # fallback (so‘nggi cache bo‘lsa o‘shani qaytar)
        if _price_cache["price"]:
            return _price_cache["price"]
        raise RuntimeError(f"LTC narxini olishda xatolik: {e}")

def usd_to_ltc(usd: float) -> float:
    p = get_ltc_usd_price()
    return usd / p

# =========================
# PAYMENT CHECK (address tx)
# =========================
def to_satoshi(ltc_amount: float) -> int:
    return int(round(ltc_amount * 100_000_000))

def satoshi_to_ltc(sat: int) -> float:
    return sat / 100_000_000.0

def fetch_txrefs_blockcypher(address: str):
    # Returns list of {tx_hash, value_satoshi, confirmations}
    # https://api.blockcypher.com/v1/ltc/main/addrs/<address>?limit=50
    url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}"
    r = requests.get(url, params={"limit": 50}, timeout=20)
    r.raise_for_status()
    data = r.json()
    txrefs = data.get("txrefs", []) or []
    res = []
    for t in txrefs:
        if t.get("tx_input_n", -1) != -1:
            continue  # incoming only (tx_input_n == -1 is output to us)
        res.append({
            "tx_hash": t.get("tx_hash"),
            "value": int(t.get("value", 0)),
            "confirmations": int(t.get("confirmations", 0)),
        })
    return res

def try_find_payment(expected_satoshi: int, tolerance_satoshi: int = 20):
    """
    Search incoming txrefs to LTC_WALLET and match by received satoshi.
    tolerance_satoshi allows tiny rounding differences.
    """
    try:
        txrefs = fetch_txrefs_blockcypher(LTC_WALLET)
    except Exception:
        return None

    for t in txrefs:
        if t["confirmations"] < MIN_CONFIRMATIONS:
            continue
        if abs(t["value"] - expected_satoshi) <= tolerance_satoshi:
            return t["tx_hash"]
    return None

# =========================
# UI (reply keyboards)
# =========================
def main_menu_kb(is_admin: bool = False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("👤 Profilim"))
    kb.row(KeyboardButton("🛍 Vitrina"), KeyboardButton("💱 Obmenniki"))
    kb.row(KeyboardButton("⭐ Izohlar"), KeyboardButton("🆘 Yordam"))
    kb.row(KeyboardButton("💬 Kanal"), KeyboardButton("🤖 Shaxsiy bot"))
    kb.row(KeyboardButton("💼 Ish"))
    if is_admin:
        kb.row(KeyboardButton("🛠 Admin panel"))
    return kb

def back_to_menu_kb(is_admin: bool = False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🏠 Asosiy menyu"))
    if is_admin:
        kb.row(KeyboardButton("🛠 Admin panel"))
    return kb

# =========================
# STATES (admin add product)
# =========================
class AddProduct(StatesGroup):
    name = State()
    price = State()
    city = State()
    photo = State()
    desc = State()

# =========================
# HELPERS
# =========================
def is_admin(user_id: int) -> bool:
    return ADMIN_ID > 0 and user_id == ADMIN_ID

def fmt_money(x: float) -> str:
    return f"{x:.2f}"

def fmt_ltc(x: float) -> str:
    # show up to 8 decimals
    return f"{x:.8f}".rstrip("0").rstrip(".")

def gen_order_code() -> str:
    return f"DALI-{random.randint(100000, 999999)}"

# =========================
# START / MENU
# =========================
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    user = get_user(message.from_user.id)

    # referral parse: /start ref_<id>
    if message.get_args():
        m = re.match(r"ref_(\d+)", message.get_args().strip())
        if m:
            inviter = int(m.group(1))
            # simple referral: if first time invited_by is null -> set + 5% discount
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT invited_by FROM users WHERE tg_id=?", (message.from_user.id,))
            inv = cur.fetchone()
            if inv and inv["invited_by"] is None and inviter != message.from_user.id:
                cur.execute("UPDATE users SET invited_by=?, discount=? WHERE tg_id=?",
                            (inviter, 5.0, message.from_user.id))
                conn.commit()
            conn.close()

    text = (
        "🚗 <b>DALI SHOP</b>\n\n"
        "✅ Keng tanlov (qonuniy gadjetlar/aksessuarlar)\n"
        "🔄 Oson xarid jarayoni\n"
        "🛡 Sifat kafolati\n\n"
        "<b>Asosiy menyu:</b>"
    )
    kb = main_menu_kb(is_admin=is_admin(message.from_user.id))
    if START_IMAGE_URL:
        try:
            await message.answer_photo(START_IMAGE_URL, caption=text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)

@dp.message_handler(lambda m: m.text in ["🏠 Asosiy menyu", "Главное меню", "Главное меню:"])
async def go_menu(message: types.Message):
    kb = main_menu_kb(is_admin=is_admin(message.from_user.id))
    await message.answer("🏠 Asosiy menyu:", reply_markup=kb)

# =========================
# PROFILE
# =========================
@dp.message_handler(lambda m: m.text == "👤 Profilim")
async def profile(message: types.Message):
    user = get_user(message.from_user.id)

    # show balance (internal) + show city + discount
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start=ref_{message.from_user.id}"
    text = (
        f"🪪 <b>Profil</b>\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"🏙 Tanlangan shahar: <b>{user['city']}</b>\n\n"
        f"🎟 Shaxsiy chegirma: <b>{fmt_money(user['discount'])}%</b>\n"
        f"💰 Balans: <b>${fmt_money(user['balance_usd'])}</b> | <b>{fmt_ltc(user['balance_ltc'])} LTC</b>\n\n"
        f"🔗 Taklif havolasi:\n{ref_link}"
    )

    ikb = InlineKeyboardMarkup(row_width=1)
    ikb.add(
        InlineKeyboardButton("✍️ Promokod aktivatsiya", callback_data="promo"),
        InlineKeyboardButton("🛍 Xaridlar tarixi", callback_data="history"),
        InlineKeyboardButton("🔄 Shaharni o'zgartirish", callback_data="city_change"),
        InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="back_menu"),
    )
    await message.answer(text, reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data == "back_menu")
async def cb_back_menu(call: types.CallbackQuery):
    await call.message.delete()
    kb = main_menu_kb(is_admin=is_admin(call.from_user.id))
    await bot.send_message(call.from_user.id, "🏠 Asosiy menyu:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "city_change")
async def cb_city_change(call: types.CallbackQuery):
    ikb = InlineKeyboardMarkup(row_width=2)
    for c in CITIES:
        ikb.insert(InlineKeyboardButton(c, callback_data=f"city_set:{c}"))
    ikb.add(InlineKeyboardButton("⬅️ Orqaga", callback_data="profile_back"))
    await call.message.edit_text("🏙 Shaharni tanlang:", reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data.startswith("city_set:"))
async def cb_city_set(call: types.CallbackQuery):
    city = call.data.split(":", 1)[1]
    if city not in CITIES:
        await call.answer("Noto'g'ri shahar", show_alert=True)
        return
    set_user_city(call.from_user.id, city)
    await call.answer("Saqlandi ✅")
    await call.message.edit_text(f"✅ Shahar o'zgardi: <b>{city}</b>", reply_markup=InlineKeyboardMarkup().add(
        InlineKeyboardButton("⬅️ Profilga qaytish", callback_data="profile_back")
    ))

@dp.callback_query_handler(lambda c: c.data == "profile_back")
async def cb_profile_back(call: types.CallbackQuery):
    # re-render profile
    fake = types.Message(
        message_id=call.message.message_id,
        date=call.message.date,
        chat=call.message.chat,
        from_user=call.from_user,
        sender_chat=None,
        content_type="text",
        options={}
    )
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    await profile(fake)

# Promo (simple)
@dp.callback_query_handler(lambda c: c.data == "promo")
async def cb_promo(call: types.CallbackQuery):
    await call.message.edit_text(
        "✍️ Promokod kiriting (masalan: <code>DALI5</code>)\n\n"
        "Bekor qilish: /menu",
        reply_markup=None
    )
    state = dp.current_state(user=call.from_user.id)
    await state.set_state("await_promo")

@dp.message_handler(state="await_promo")
async def promo_enter(message: types.Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if code == "DALI5":
        set_user_discount(message.from_user.id, 5.0)
        await message.answer("✅ Promokod qabul qilindi. Chegirma: <b>5%</b>", reply_markup=back_to_menu_kb(is_admin=is_admin(message.from_user.id)))
    else:
        await message.answer("❌ Promokod noto'g'ri.", reply_markup=back_to_menu_kb(is_admin=is_admin(message.from_user.id)))
    await state.finish()

# History
@dp.callback_query_handler(lambda c: c.data == "history")
async def cb_history(call: types.CallbackQuery):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.order_code, o.status, o.amount_usd, o.ltc_amount, p.name
        FROM orders o
        LEFT JOIN products p ON p.id=o.product_id
        WHERE o.tg_id=?
        ORDER BY o.id DESC
        LIMIT 10
    """, (call.from_user.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await call.message.edit_text("🛍 Xaridlar tarixi bo'sh.", reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ Orqaga", callback_data="profile_back")
        ))
        return

    lines = ["🛍 <b>So‘nggi 10 ta xarid</b>\n"]
    for r in rows:
        lines.append(
            f"• <b>{r['name'] or 'Mahsulot'}</b>\n"
            f"  ID: <code>{r['order_code']}</code>\n"
            f"  Status: <b>{r['status']}</b>\n"
            f"  Summa: <b>${fmt_money(r['amount_usd'])}</b> | <b>{fmt_ltc(r['ltc_amount'])} LTC</b>\n"
        )
    await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup().add(
        InlineKeyboardButton("⬅️ Orqaga", callback_data="profile_back")
    ))

# =========================
# OBMENNIKI
# =========================
@dp.message_handler(lambda m: m.text == "💱 Obmenniki")
async def obmenniki(message: types.Message):
    text = "💱 <b>Ishonchli obmenniklar ro'yxati:</b>"
    ikb = InlineKeyboardMarkup(row_width=1)
    ikb.add(
        InlineKeyboardButton("↗️ LTCEXP", url=f"https://t.me/{OBMENNIKI_USERNAME}")
    )
    await message.answer(text, reply_markup=ikb)

# =========================
# CHANNEL / PERSONAL BOT / JOB (placeholders)
# =========================
@dp.message_handler(lambda m: m.text == "💬 Kanal")
async def channel(message: types.Message):
    if CHANNEL_URL:
        await message.answer("💬 Kanal:", reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("↗️ Kanalga o'tish", url=CHANNEL_URL)
        ))
    else:
        await message.answer("💬 Kanal hali ulanmagan.", reply_markup=back_to_menu_kb(is_admin=is_admin(message.from_user.id)))

@dp.message_handler(lambda m: m.text == "🤖 Shaxsiy bot")
async def personal_bot(message: types.Message):
    await message.answer("🤖 Shaxsiy bot: tez orada.", reply_markup=back_to_menu_kb(is_admin=is_admin(message.from_user.id)))

@dp.message_handler(lambda m: m.text == "💼 Ish")
async def job(message: types.Message):
    await message.answer("💼 Ish: tez orada.", reply_markup=back_to_menu_kb(is_admin=is_admin(message.from_user.id)))

# =========================
# HELP
# =========================
@dp.message_handler(lambda m: m.text == "🆘 Yordam")
async def help_menu(message: types.Message):
    text = (
        "🆘 <b>Yordam</b>\n\n"
        "Qoidalar matnini kiriting yoki telegram post havolasini qo'shing.\n"
        "(Bu bo‘limni keyin kengaytiramiz.)"
    )
    ikb = InlineKeyboardMarkup(row_width=1)
    ikb.add(
        InlineKeyboardButton("↗️ Support", url=SUPPORT_URL),
        InlineKeyboardButton("↗️ Operator", url=OPERATOR_URL),
    )
    await message.answer(text, reply_markup=ikb)

# =========================
# REVIEWS (pagination)
# =========================
def render_review_page(page: int, per_page: int = 1):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM reviews")
    total = cur.fetchone()["c"]
    if total == 0:
        conn.close()
        return None, 0

    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page

    cur.execute("""
        SELECT * FROM reviews ORDER BY id DESC LIMIT ? OFFSET ?
    """, (per_page, offset))
    row = cur.fetchone()
    conn.close()

    text = (
        f"📝 <b>Izoh #{row['id']}:</b>\n\n"
        f"• Mahsulot: <b>{row['product_name']}</b>\n"
        f"• Mahsulot bahosi: {'⭐' * int(row['rating_product'])}\n"
        f"• Xizmat bahosi: {'⭐' * int(row['rating_service'])}\n\n"
        f"• {row['text']}\n\n"
        f"• Xarid sanasi: {row['purchased_at']}\n"
        f"• E'lon qilingan: {row['published_at']}\n"
    )
    return (text, pages)

@dp.message_handler(lambda m: m.text == "⭐ Izohlar")
async def reviews(message: types.Message):
    text, pages = render_review_page(1)
    if not text:
        await message.answer("⭐ Izohlar hozircha yo'q.", reply_markup=back_to_menu_kb(is_admin=is_admin(message.from_user.id)))
        return

    ikb = InlineKeyboardMarkup(row_width=3)
    ikb.row(
        InlineKeyboardButton("◀️", callback_data="rev:prev:1"),
        InlineKeyboardButton(f"1/{pages}", callback_data="rev:noop"),
        InlineKeyboardButton("▶️", callback_data="rev:next:1"),
    )
    ikb.add(InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="back_menu"))
    await message.answer(text, reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data.startswith("rev:"))
async def cb_reviews(call: types.CallbackQuery):
    parts = call.data.split(":")
    action = parts[1]
    current = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

    text, pages = render_review_page(current)
    if not text:
        await call.answer("Izoh yo'q", show_alert=True)
        return

    if action == "prev":
        new_page = max(1, current - 1)
    elif action == "next":
        new_page = current + 1
    else:
        await call.answer()
        return

    text, pages = render_review_page(new_page)
    ikb = InlineKeyboardMarkup(row_width=3)
    ikb.row(
        InlineKeyboardButton("◀️", callback_data=f"rev:prev:{new_page}"),
        InlineKeyboardButton(f"{new_page}/{pages}", callback_data="rev:noop"),
        InlineKeyboardButton("▶️", callback_data=f"rev:next:{new_page}"),
    )
    ikb.add(InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="back_menu"))
    await call.message.edit_text(text, reply_markup=ikb)
    await call.answer()

# =========================
# SHOWCASE (VITRINA)
# =========================
@dp.message_handler(lambda m: m.text == "🛍 Vitrina")
async def showcase(message: types.Message):
    user = get_user(message.from_user.id)
    city = user["city"]

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM products
        WHERE is_active=1 AND (city=? OR city='ALL')
        ORDER BY id ASC
    """, (city,))
    items = cur.fetchall()
    conn.close()

    header = (
        f"🛒 <b>Shahardagi aktual tovarlar:</b> <b>{city}</b>\n\n"
        f"ℹ️ Boshqa shahar tovarlarini ko‘rish uchun profil bo‘limida shaharni o‘zgartiring."
    )

    if not items:
        await message.answer(header + "\n\n❌ Hozircha tovar yo'q.", reply_markup=back_to_menu_kb(is_admin=is_admin(message.from_user.id)))
        return

    ikb = InlineKeyboardMarkup(row_width=1)
    for p in items:
        ikb.add(InlineKeyboardButton(f"{p['name']} — ${fmt_money(p['price_usd'])}", callback_data=f"prod:{p['id']}"))
    ikb.add(InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="back_menu"))

    await message.answer(header, reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data.startswith("prod:"))
async def cb_product(call: types.CallbackQuery):
    pid = int(call.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id=?", (pid,))
    p = cur.fetchone()
    conn.close()

    if not p or p["is_active"] != 1:
        await call.answer("Mahsulot topilmadi", show_alert=True)
        return

    user = get_user(call.from_user.id)
    disc = float(user["discount"] or 0.0)
    price = float(p["price_usd"])
    final_price = price * (1.0 - disc / 100.0)

    text = (
        f"🛍 <b>{p['name']}</b>\n\n"
        f"📝 {p['description'] or '—'}\n\n"
        f"🏙 Shahar: <b>{p['city']}</b>\n"
        f"💵 Narx: <b>${fmt_money(price)}</b>\n"
        f"🎟 Chegirma: <b>{fmt_money(disc)}%</b>\n"
        f"✅ Yakuniy narx: <b>${fmt_money(final_price)}</b>\n"
    )

    ikb = InlineKeyboardMarkup(row_width=1)
    ikb.add(
        InlineKeyboardButton("💳 Sotib olish (LTC)", callback_data=f"buy:{pid}"),
        InlineKeyboardButton("⬅️ Vitrinaga qaytish", callback_data="back_showcase")
    )

    # product photo if any
    if p["photo_url"]:
        try:
            await call.message.edit_caption(caption=text, reply_markup=ikb)
        except Exception:
            try:
                await call.message.delete()
            except Exception:
                pass
            await bot.send_photo(call.from_user.id, p["photo_url"], caption=text, reply_markup=ikb)
    else:
        await call.message.edit_text(text, reply_markup=ikb)

    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "back_showcase")
async def cb_back_showcase(call: types.CallbackQuery):
    await call.message.delete()
    fake = types.Message(
        message_id=call.message.message_id,
        date=call.message.date,
        chat=call.message.chat,
        from_user=call.from_user,
        sender_chat=None,
        content_type="text",
        options={}
    )
    await showcase(fake)

# =========================
# BUY / PAY / DELIVER
# =========================
@dp.callback_query_handler(lambda c: c.data.startswith("buy:"))
async def cb_buy(call: types.CallbackQuery):
    pid = int(call.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id=?", (pid,))
    p = cur.fetchone()
    conn.close()
    if not p or p["is_active"] != 1:
        await call.answer("Mahsulot topilmadi", show_alert=True)
        return

    user = get_user(call.from_user.id)
    disc = float(user["discount"] or 0.0)
    price = float(p["price_usd"])
    final_usd = price * (1.0 - disc / 100.0)

    # compute LTC amount + tiny uniqueness tag
    ltc = usd_to_ltc(final_usd)
    # add uniqueness tag (1..80 satoshis)
    tag_sat = random.randint(1, 80)
    ltc_sat = to_satoshi(ltc) + tag_sat
    ltc_final = satoshi_to_ltc(ltc_sat)

    order_code = gen_order_code()
    created_at = datetime.now(timezone.utc).isoformat()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders(order_code, tg_id, product_id, amount_usd, ltc_amount, ltc_address, status, created_at)
        VALUES(?,?,?,?,?,?, 'PENDING', ?)
    """, (order_code, call.from_user.id, pid, float(final_usd), float(ltc_final), LTC_WALLET, created_at))
    conn.commit()
    conn.close()

    text = (
        f"💳 <b>To‘lov</b>\n\n"
        f"🛍 Mahsulot: <b>{p['name']}</b>\n"
        f"🧾 Buyurtma: <code>{order_code}</code>\n\n"
        f"✅ To‘lash kerak: <b>{fmt_ltc(ltc_final)} LTC</b>\n"
        f"🏦 Manzil (LTC):\n<code>{LTC_WALLET}</code>\n\n"
        f"ℹ️ Eslatma: bot to‘lovni avtomatik tekshiradi.\n"
        f"⏳ Tasdiq: <b>{MIN_CONFIRMATIONS} conf</b>\n"
    )

    ikb = InlineKeyboardMarkup(row_width=1)
    ikb.add(
        InlineKeyboardButton("🔄 To‘lovni tekshirish", callback_data=f"check:{order_code}"),
        InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="back_menu")
    )

    await call.message.edit_text(text, reply_markup=ikb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("check:"))
async def cb_check(call: types.CallbackQuery):
    order_code = call.data.split(":", 1)[1]

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.*, p.name as product_name, p.photo_url as photo_url
        FROM orders o
        LEFT JOIN products p ON p.id=o.product_id
        WHERE o.order_code=? AND o.tg_id=?
    """, (order_code, call.from_user.id))
    o = cur.fetchone()
    conn.close()

    if not o:
        await call.answer("Buyurtma topilmadi", show_alert=True)
        return

    if o["status"] == "PAID":
        await call.answer("Allaqachon to'langan ✅", show_alert=True)
        return

    expected_sat = to_satoshi(float(o["ltc_amount"]))
    txid = try_find_payment(expected_sat)

    if not txid:
        await call.answer("Hali to‘lov topilmadi. Keyinroq qayta urinib ko‘ring.", show_alert=True)
        return

    # mark paid
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status='PAID', txid=?, paid_at=? WHERE order_code=?",
                (txid, datetime.now(timezone.utc).isoformat(), order_code))
    conn.commit()
    conn.close()

    # deliver: send photo (if available) + caption info
    deliver_caption = (
        f"✅ <b>To‘lov qabul qilindi!</b>\n\n"
        f"🧾 Buyurtma: <code>{order_code}</code>\n"
        f"🛍 Mahsulot: <b>{o['product_name']}</b>\n"
        f"💳 To‘langan: <b>{fmt_ltc(float(o['ltc_amount']))} LTC</b>\n"
        f"🔗 TXID: <code>{txid}</code>\n\n"
        f"📦 Yetkazib berish / instruktsiya:\n"
        f"— (bu yerga mahsulot bo‘yicha kerakli info yoziladi)\n"
    )

    try:
        if o["photo_url"]:
            await bot.send_photo(call.from_user.id, o["photo_url"], caption=deliver_caption)
        else:
            await bot.send_message(call.from_user.id, deliver_caption)
    except Exception:
        await bot.send_message(call.from_user.id, deliver_caption)

    # notify admin
    if ADMIN_ID > 0:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"✅ <b>Yangi to‘lov</b>\n"
                f"Buyurtma: <code>{order_code}</code>\n"
                f"User: <code>{call.from_user.id}</code>\n"
                f"Mahsulot: <b>{o['product_name']}</b>\n"
                f"LTC: <b>{fmt_ltc(float(o['ltc_amount']))}</b>\n"
                f"TXID: <code>{txid}</code>"
            )
        except Exception:
            pass

    await call.message.edit_text(
        f"✅ To‘lov tasdiqlandi!\n\nBuyurtma: <code>{order_code}</code>\nTXID: <code>{txid}</code>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🏠 Asosiy menyu", callback_data="back_menu")
        )
    )
    await call.answer("To‘landi ✅", show_alert=True)

# =========================
# ADMIN PANEL
# =========================
@dp.message_handler(lambda m: m.text == "🛠 Admin panel")
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    ikb = InlineKeyboardMarkup(row_width=1)
    ikb.add(
        InlineKeyboardButton("➕ Mahsulot qo‘shish", callback_data="adm:addp"),
        InlineKeyboardButton("📦 Buyurtmalar (10)", callback_data="adm:orders"),
        InlineKeyboardButton("🛒 Mahsulotlar", callback_data="adm:products"),
    )
    await message.answer("🛠 <b>Admin panel</b>", reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data == "adm:addp")
async def adm_addp(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    await call.message.edit_text("➕ Mahsulot nomini kiriting:")
    await AddProduct.name.set()
    await call.answer()

@dp.message_handler(state=AddProduct.name)
async def adm_addp_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("💵 Narx (USD) kiriting. Masalan: 25")
    await AddProduct.price.set()

@dp.message_handler(state=AddProduct.price)
async def adm_addp_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",", "."))
        if price <= 0:
            raise ValueError()
    except Exception:
        await message.answer("❌ Narx noto'g'ri. Masalan: 25 yoki 45")
        return
    await state.update_data(price=price)

    ikb = ReplyKeyboardMarkup(resize_keyboard=True)
    for c in CITIES:
        ikb.add(KeyboardButton(c))
    ikb.add(KeyboardButton("ALL"))
    await message.answer("🏙 Qaysi shahar uchun? (yoki ALL)", reply_markup=ikb)
    await AddProduct.city.set()

@dp.message_handler(state=AddProduct.city)
async def adm_addp_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    if city != "ALL" and city not in CITIES:
        await message.answer("❌ Shahar noto'g'ri. Tugmadan tanlang.")
        return
    await state.update_data(city=city)
    await message.answer("🖼 Photo URL kiriting (bo‘sh qoldirsang ham bo‘ladi). Agar bo‘sh bo‘lsa: '-' yubor.")
    await AddProduct.photo.set()

@dp.message_handler(state=AddProduct.photo)
async def adm_addp_photo(message: types.Message, state: FSMContext):
    photo = message.text.strip()
    if photo == "-":
        photo = ""
    await state.update_data(photo=photo)
    await message.answer("📝 Tavsif kiriting (1-2 qator):")
    await AddProduct.desc.set()

@dp.message_handler(state=AddProduct.desc)
async def adm_addp_desc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    price = data["price"]
    city = data["city"]
    photo = data["photo"]
    desc = message.text.strip()

    conn = db()
    conn.execute(
        "INSERT INTO products(name, price_usd, city, photo_url, description, is_active) VALUES(?,?,?,?,?,1)",
        (name, price, city, photo, desc)
    )
    conn.commit()
    conn.close()

    await state.finish()
    await message.answer("✅ Mahsulot qo‘shildi.", reply_markup=main_menu_kb(is_admin=True))

@dp.callback_query_handler(lambda c: c.data == "adm:orders")
async def adm_orders(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.order_code, o.status, o.amount_usd, o.ltc_amount, o.txid, o.created_at, p.name
        FROM orders o
        LEFT JOIN products p ON p.id=o.product_id
        ORDER BY o.id DESC
        LIMIT 10
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await call.message.edit_text("📦 Buyurtmalar yo‘q.")
        return

    lines = ["📦 <b>So‘nggi 10 ta buyurtma</b>\n"]
    for r in rows:
        lines.append(
            f"• <b>{r['name'] or 'Mahsulot'}</b>\n"
            f"  ID: <code>{r['order_code']}</code>\n"
            f"  Status: <b>{r['status']}</b>\n"
            f"  ${fmt_money(r['amount_usd'])} | {fmt_ltc(r['ltc_amount'])} LTC\n"
            f"  TXID: <code>{r['txid'] or '-'}</code>\n"
        )

    ikb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:back"))
    await call.message.edit_text("\n".join(lines), reply_markup=ikb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "adm:products")
async def adm_products(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products ORDER BY id DESC LIMIT 20")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await call.message.edit_text("🛒 Mahsulot yo‘q.")
        return

    ikb = InlineKeyboardMarkup(row_width=1)
    txt = ["🛒 <b>Mahsulotlar</b> (20)\n"]
    for p in rows:
        status = "✅" if p["is_active"] == 1 else "⛔️"
        txt.append(f"{status} <b>{p['name']}</b> — ${fmt_money(p['price_usd'])} — {p['city']}")
        ikb.add(InlineKeyboardButton(f"{status} Toggle: {p['name']}", callback_data=f"adm:toggle:{p['id']}"))
    ikb.add(InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:back"))
    await call.message.edit_text("\n".join(txt), reply_markup=ikb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("adm:toggle:"))
async def adm_toggle(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    pid = int(call.data.split(":")[2])
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM products WHERE id=?", (pid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await call.answer("Topilmadi", show_alert=True)
        return
    new_val = 0 if row["is_active"] == 1 else 1
    cur.execute("UPDATE products SET is_active=? WHERE id=?", (new_val, pid))
    conn.commit()
    conn.close()
    await call.answer("O'zgardi ✅")
    # refresh list
    await adm_products(call)

@dp.callback_query_handler(lambda c: c.data == "adm:back")
async def adm_back(call: types.CallbackQuery):
    await call.message.delete()
    await bot.send_message(call.from_user.id, "🛠 Admin panel", reply_markup=main_menu_kb(is_admin=True))

# =========================
# FALLBACK
# =========================
@dp.message_handler()
async def fallback(message: types.Message):
    # keep it simple: send menu hint
    kb = main_menu_kb(is_admin=is_admin(message.from_user.id))
    await message.answer("Menyudan tanlang 👇", reply_markup=kb)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
