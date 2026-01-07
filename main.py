import os
import time
import sqlite3
import logging
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from bip_utils import Bip84, Bip84Coins, Bip44Changes

# =======================
# CONFIG
# =======================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shopbot")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")
LTC_XPUB = (os.getenv("LTC_XPUB") or "").strip()

DB_PATH = os.getenv("DB_PATH", "shop.sqlite3")
MIN_CONFIRMATIONS = int(os.getenv("MIN_CONFIRMATIONS", "1") or "1")

OBMENNIKI_USERNAME = "ltc_exp"
SUPPORT_USERNAME = "qwerty7777jass"
OPERATOR_USERNAME = "qwerty7777jass"

CITIES = ["Buxoro", "Navoiy", "Samarqand", "Toshkent"]
BC_ADDR = "https://api.blockcypher.com/v1/ltc/main/addrs/{address}"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not LTC_XPUB:
    raise RuntimeError("LTC_XPUB missing")

bot = Bot(BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot, storage=MemoryStorage())

# =======================
# Base58Check (zpub -> xpub normalize)
# =======================
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58_ALPHABET.index(ch)
    h = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + h

def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n > 0:
        n, r = divmod(n, 58)
        s = B58_ALPHABET[r] + s
    pad = 0
    for byte in b:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + (s or "")

def _b58check_decode(s: str) -> bytes:
    raw = _b58decode(s.strip())
    if len(raw) < 4:
        raise ValueError("bad base58check length")
    data, chk = raw[:-4], raw[-4:]
    h = hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]
    if h != chk:
        raise ValueError("bad base58check checksum")
    return data

def _b58check_encode(data: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]
    return _b58encode(data + chk)

def normalize_extpub(key: str) -> str:
    """
    Some libs don't accept SLIP-0132 prefixes (zpub/ypub/etc).
    Convert to classic xpub by swapping version bytes.
    This keeps payload unchanged (chaincode + pubkey).
    """
    k = key.strip()
    p = k[:4].lower()
    if p in ("zpub", "ypub", "xpub"):
        if p == "xpub":
            return k
        data = _b58check_decode(k)
        xpub_ver = bytes.fromhex("0488b21e")
        return _b58check_encode(xpub_ver + data[4:])
    # Accept also Zpub/Ypub
    if k[:4] in ("Zpub", "Ypub", "Xpub"):
        data = _b58check_decode(k)
        xpub_ver = bytes.fromhex("0488b21e")
        return _b58check_encode(xpub_ver + data[4:])
    return k

def derive_ltc_address(index: int) -> str:
    key = normalize_extpub(LTC_XPUB)
    ctx = Bip84.FromExtendedKey(key, Bip84Coins.LITECOIN)
    return ctx.Change(Bip44Changes.CHAIN_EXT).AddressIndex(index).PublicKey().ToAddress()

# =======================
# DB
# =======================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
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
                delivery_photo TEXT,   -- URL or Telegram file_id
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
                created_at INTEGER NOT NULL
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

        # seed products if empty
        cur.execute("SELECT COUNT(*) c FROM products")
        if int(cur.fetchone()["c"]) == 0:
            cur.executemany("""
                INSERT INTO products(name, price_ltc, is_active, delivery_photo, delivery_text)
                VALUES(?,?,?,?,?)
            """, [
                ("Product A", 0.0035, 1, "", "Инструкция: ..."),
                ("Product B", 0.0056, 1, "", "Инструкция: ..."),
                ("Product C", 0.0084, 1, "", "Инструкция: ..."),
                ("Product D", 0.0063, 1, "", "Инструкция: ..."),
            ])
            conn.commit()

# =======================
# Core helpers
# =======================
def is_admin(uid: int) -> bool:
    return ADMIN_ID > 0 and uid == ADMIN_ID

def ensure_user(uid: int):
    now = int(time.time())
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (uid,))
        if cur.fetchone() is not None:
            return

        cur.execute("SELECT MAX(addr_index) mx FROM users")
        mx = cur.fetchone()["mx"]
        idx = int(mx) + 1 if mx is not None else 0

        try:
            addr = derive_ltc_address(idx)
        except Exception as e:
            # Keep bot alive even if derivation fails; admin can fix xpub later.
            log.exception("Address derivation failed: %s", e)
            addr = "DERIVE_ERROR"

        cur.execute("""
            INSERT INTO users(tg_id, city, addr_index, ltc_address, created_at)
            VALUES(?,?,?,?,?)
        """, (uid, CITIES[0], idx, addr, now))

        cur.execute("""
            INSERT OR IGNORE INTO balances(tg_id, ltc, updated_at) VALUES(?,?,?)
        """, (uid, 0.0, now))

        conn.commit()

def get_user(uid: int) -> sqlite3.Row:
    ensure_user(uid)
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone()

def set_city(uid: int, city: str):
    with db() as conn:
        conn.execute("UPDATE users SET city=? WHERE tg_id=?", (city, uid))
        conn.commit()

def get_balance(uid: int) -> float:
    ensure_user(uid)
    with db() as conn:
        row = conn.execute("SELECT ltc FROM balances WHERE tg_id=?", (uid,)).fetchone()
        return float(row["ltc"]) if row else 0.0

def add_balance(uid: int, amt: float):
    now = int(time.time())
    with db() as conn:
        conn.execute("UPDATE balances SET ltc=ltc+?, updated_at=? WHERE tg_id=?", (amt, now, uid))
        conn.commit()

def sub_balance(uid: int, amt: float):
    now = int(time.time())
    with db() as conn:
        conn.execute("UPDATE balances SET ltc=ltc-?, updated_at=? WHERE tg_id=?", (amt, now, uid))
        conn.commit()

def list_products(active_only: bool = True) -> List[sqlite3.Row]:
    with db() as conn:
        if active_only:
            return conn.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id").fetchall()
        return conn.execute("SELECT * FROM products ORDER BY id").fetchall()

def get_product(pid: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()

def create_order(uid: int, pid: int, amt: float) -> int:
    now = int(time.time())
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders(tg_id, product_id, amount_ltc, status, created_at)
            VALUES(?,?,?,'PAID',?)
        """, (uid, pid, amt, now))
        conn.commit()
        return int(cur.lastrowid)

def user_orders(uid: int, limit: int = 15) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("""
            SELECT o.id, o.amount_ltc, o.status, o.created_at, p.name product_name
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.tg_id=?
            ORDER BY o.id DESC
            LIMIT ?
        """, (uid, limit)).fetchall()

def has_purchase(uid: int, pid: int) -> bool:
    with db() as conn:
        row = conn.execute("""
            SELECT 1 FROM orders WHERE tg_id=? AND product_id=? AND status='PAID' LIMIT 1
        """, (uid, pid)).fetchone()
        return row is not None

# =======================
# Blockchain credit (manual check)
# =======================
def fetch_incoming(address: str) -> List[Tuple[str, int]]:
    r = requests.get(BC_ADDR.format(address=address), timeout=20).json()
    txrefs = r.get("txrefs", []) or []
    outs = []
    for t in txrefs:
        # incoming outputs: tx_input_n == -1
        if int(t.get("tx_input_n", 0)) != -1:
            continue
        if int(t.get("confirmations", 0)) < MIN_CONFIRMATIONS:
            continue
        outs.append((t.get("tx_hash", ""), int(t.get("value", 0))))
    return outs

def credit_new(uid: int) -> int:
    u = get_user(uid)
    addr = (u["ltc_address"] or "").strip()
    if not addr or addr == "DERIVE_ERROR":
        return 0

    try:
        outs = fetch_incoming(addr)
    except Exception as e:
        log.exception("fetch_incoming failed: %s", e)
        return 0

    credited = 0
    now = int(time.time())
    with db() as conn:
        cur = conn.cursor()
        for tx, val_sat in outs:
            if not tx or val_sat <= 0:
                continue
            try:
                cur.execute("""
                    INSERT INTO credited_utx(tg_id, address, tx_hash, value_sat, credited_at)
                    VALUES(?,?,?,?,?)
                """, (uid, addr, tx, val_sat, now))
                add_balance(uid, val_sat / 100_000_000.0)
                credited += 1
            except sqlite3.IntegrityError:
                continue
        conn.commit()
    return credited

# =======================
# Reviews
# =======================
def reviews_count() -> int:
    with db() as conn:
        return int(conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"])

def get_review_page(page: int, per_page: int = 1) -> Tuple[Optional[sqlite3.Row], int, int]:
    total = reviews_count()
    if total == 0:
        return None, 0, 0
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    with db() as conn:
        row = conn.execute("""
            SELECT r.*, p.name product_name
            FROM reviews r JOIN products p ON p.id=r.product_id
            ORDER BY r.id DESC LIMIT ? OFFSET ?
        """, (per_page, offset)).fetchone()
    return row, page, pages

def add_review(uid: int, pid: int, rp: int, rs: int, text: str):
    now = int(time.time())
    with db() as conn:
        conn.execute("""
            INSERT INTO reviews(tg_id, product_id, rating_product, rating_service, text, created_at)
            VALUES(?,?,?,?,?,?)
        """, (uid, pid, rp, rs, text.strip(), now))
        conn.commit()

def mask_uid(uid: int) -> str:
    s = str(uid)
    if len(s) <= 6:
        return s
    return s[:3] + "****" + s[-2:]

# =======================
# Keyboards (RU)
# =======================
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
    ikb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="profile:back"))
    return ikb

def shop_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    for p in list_products(True):
        ikb.add(types.InlineKeyboardButton(
            f"{p['name']} — {float(p['price_ltc']):.8f} LTC",
            callback_data=f"p:{p['id']}"
        ))
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
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
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

def after_purchase_kb(pid: int) -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("✍️ Оставить отзыв", callback_data=f"rev:add:{pid}"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
    )
    return ikb

# =======================
# States
# =======================
class AdminAdd(StatesGroup):
    name = State()
    price = State()

class AdminPrice(StatesGroup):
    price = State()

class AdminDelivery(StatesGroup):
    photo = State()
    text = State()

class ReviewFlow(StatesGroup):
    rp = State()
    rs = State()
    text = State()

# =======================
# Admin UI
# =======================
def admin_menu_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("📦 Товары", callback_data="adm:products"),
        types.InlineKeyboardButton("➕ Добавить товар", callback_data="adm:add"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
    )
    return ikb

def admin_products_kb() -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    for p in list_products(False):
        st = "✅" if int(p["is_active"]) == 1 else "⛔️"
        ikb.add(types.InlineKeyboardButton(
            f"{st} #{p['id']} {p['name']} ({float(p['price_ltc']):.8f})",
            callback_data=f"adm:p:{p['id']}"
        ))
    ikb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="adm:back"))
    return ikb

def admin_product_actions_kb(pid: int) -> types.InlineKeyboardMarkup:
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("💲 Изменить цену", callback_data=f"adm:price:{pid}"),
        types.InlineKeyboardButton("🖼/📝 Delivery", callback_data=f"adm:delivery:{pid}"),
        types.InlineKeyboardButton("🔁 Toggle ON/OFF", callback_data=f"adm:toggle:{pid}"),
        types.InlineKeyboardButton("⬅️ К товарам", callback_data="adm:products"),
    )
    return ikb

# =======================
# Handlers
# =======================
@dp.message_handler(commands=["start"])
async def start(m: types.Message):
    ensure_user(m.from_user.id)
    await m.answer("✅ <b>Бот запущен.</b>\nВыберите пункт меню:", reply_markup=main_menu_kb(is_admin(m.from_user.id)))

@dp.callback_query_handler(lambda c: c.data == "go:menu")
async def go_menu(c: types.CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        pass
    await bot.send_message(c.from_user.id, "🏠 <b>Главное меню</b>", reply_markup=main_menu_kb(is_admin(c.from_user.id)))
    await c.answer()

# ---- Profile
@dp.message_handler(lambda m: m.text == "👤 Мой профиль")
async def profile(m: types.Message):
    u = get_user(m.from_user.id)
    bal = get_balance(m.from_user.id)
    await m.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{m.from_user.id}</code>\n"
        f"🏙 Город: <b>{u['city']}</b>\n"
        f"🏦 Адрес для пополнения:\n<code>{u['ltc_address']}</code>\n\n"
        f"💰 Баланс: <b>{bal:.8f} LTC</b>",
        reply_markup=profile_kb()
    )

@dp.callback_query_handler(lambda c: c.data == "profile:orders")
async def profile_orders(c: types.CallbackQuery):
    rows = user_orders(c.from_user.id, 15)
    if not rows:
        await c.answer("Покупок пока нет", show_alert=True)
        return
    lines = ["🛍 <b>История покупок</b>\n"]
    for r in rows:
        dt = datetime.fromtimestamp(int(r["created_at"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"• #{r['id']} — <b>{r['product_name']}</b> — {float(r['amount_ltc']):.8f} LTC — {dt}")
    ikb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Назад", callback_data="profile:back"))
    await c.message.edit_text("\n".join(lines), reply_markup=ikb)
    await c.answer()

@dp.callback_query_handler(lambda c: c.data == "profile:back")
async def profile_back(c: types.CallbackQuery):
    await c.message.delete()
    # resend profile
    u = get_user(c.from_user.id)
    bal = get_balance(c.from_user.id)
    await bot.send_message(
        c.from_user.id,
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{c.from_user.id}</code>\n"
        f"🏙 Город: <b>{u['city']}</b>\n"
        f"🏦 Адрес для пополнения:\n<code>{u['ltc_address']}</code>\n\n"
        f"💰 Баланс: <b>{bal:.8f} LTC</b>",
        reply_markup=profile_kb()
    )
    await c.answer()

@dp.callback_query_handler(lambda c: c.data == "city:change")
async def city_change(c: types.CallbackQuery):
    await c.message.edit_text("🏙 <b>Выберите город:</b>", reply_markup=city_kb())
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("city:set:"))
async def city_set(c: types.CallbackQuery):
    city = c.data.split(":", 2)[2]
    if city not in CITIES:
        await c.answer("Неверный город", show_alert=True)
        return
    set_city(c.from_user.id, city)
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("⬅️ В профиль", callback_data="profile:back"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
    )
    await c.message.edit_text(f"✅ Город изменён: <b>{city}</b>", reply_markup=ikb)
    await c.answer()

# ---- Balance
@dp.message_handler(lambda m: m.text == "💰 Баланс")
async def balance(m: types.Message):
    u = get_user(m.from_user.id)
    bal = get_balance(m.from_user.id)
    await m.answer(
        f"💰 <b>Баланс</b>\n\n"
        f"Текущий: <b>{bal:.8f} LTC</b>\n\n"
        f"Отправьте LTC на ваш адрес:\n<code>{u['ltc_address']}</code>\n\n"
        f"Потом нажмите «Проверить пополнение».",
        reply_markup=balance_kb()
    )

@dp.callback_query_handler(lambda c: c.data == "bal:check")
async def bal_check(c: types.CallbackQuery):
    n = await asyncio.to_thread(credit_new, c.from_user.id)
    bal = get_balance(c.from_user.id)
    if n > 0:
        await c.message.edit_text(
            f"✅ <b>Зачислено</b>: <b>{n}</b>\nБаланс: <b>{bal:.8f} LTC</b>",
            reply_markup=balance_kb()
        )
        await c.answer("Зачислено ✅", show_alert=True)
    else:
        await c.answer("Новых поступлений нет", show_alert=True)

# ---- Shop
@dp.message_handler(lambda m: m.text == "🛍 Витрина")
async def shop(m: types.Message):
    await m.answer("🛍 <b>Витрина</b>\nВыберите товар:", reply_markup=shop_kb())

@dp.callback_query_handler(lambda c: c.data == "shop:back")
async def shop_back(c: types.CallbackQuery):
    await c.message.edit_text("🛍 <b>Витрина</b>\nВыберите товар:", reply_markup=shop_kb())
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("p:"))
async def product_view(c: types.CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await c.answer("Товар недоступен", show_alert=True)
        return
    await c.message.edit_text(
        f"📦 <b>{p['name']}</b>\n"
        f"💳 Цена: <b>{float(p['price_ltc']):.8f} LTC</b>\n\n"
        f"Покупка списывает средства с баланса.",
        reply_markup=product_kb(pid)
    )
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("buy:"))
async def buy(c: types.CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await c.answer("Товар недоступен", show_alert=True)
        return

    price = float(p["price_ltc"])
    bal = get_balance(c.from_user.id)
    if bal + 1e-12 < price:
        await c.answer("Недостаточно средств", show_alert=True)
        u = get_user(c.from_user.id)
        await c.message.edit_text(
            f"❌ <b>Недостаточно средств</b>\n\n"
            f"Цена: <b>{price:.8f} LTC</b>\n"
            f"Баланс: <b>{bal:.8f} LTC</b>\n\n"
            f"Пополните на адрес:\n<code>{u['ltc_address']}</code>\n"
            f"Затем нажмите «Проверить пополнение» в разделе Баланс.",
            reply_markup=types.InlineKeyboardMarkup(row_width=1).add(
                types.InlineKeyboardButton("💰 Баланс", callback_data="go:menu")
            )
        )
        return

    sub_balance(c.from_user.id, price)
    order_id = create_order(c.from_user.id, pid, price)

    delivery_text = (p["delivery_text"] or "").strip() or "Инструкция будет добавлена админом."
    caption = (
        f"✅ <b>Покупка успешна</b>\n"
        f"🧾 Заказ: <b>#{order_id}</b>\n"
        f"📦 Товар: <b>{p['name']}</b>\n"
        f"💳 Списано: <b>{price:.8f} LTC</b>\n\n"
        f"{delivery_text}"
    )
    photo = (p["delivery_photo"] or "").strip()

    try:
        if photo:
            # photo can be URL or Telegram file_id
            await bot.send_photo(c.from_user.id, photo=photo, caption=caption)
        else:
            await bot.send_message(c.from_user.id, caption)
    except Exception:
        await bot.send_message(c.from_user.id, caption)

    await bot.send_message(c.from_user.id, "⭐ Хотите оставить отзыв?", reply_markup=after_purchase_kb(pid))
    await c.message.edit_text("✅ Готово. Доставка отправлена в чат.", reply_markup=types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="go:menu"),
        types.InlineKeyboardButton("🛍 Витрина", callback_data="shop:back"),
    ))
    await c.answer()

# ---- Reviews (view)
@dp.message_handler(lambda m: m.text == "⭐ Отзывы")
async def reviews(m: types.Message):
    row, page, pages = get_review_page(1, 1)
    if not row:
        await m.answer("⭐ Отзывов пока нет.")
        return
    txt = (
        f"⭐ <b>Отзыв</b>\n\n"
        f"👤 {mask_uid(int(row['tg_id']))}\n"
        f"📦 <b>{row['product_name']}</b>\n"
        f"⭐ Товар: <b>{int(row['rating_product'])}/5</b>\n"
        f"⭐ Сервис: <b>{int(row['rating_service'])}/5</b>\n\n"
        f"{row['text']}"
    )
    await m.answer(txt, reply_markup=reviews_nav_kb(page, pages))

@dp.callback_query_handler(lambda c: c.data.startswith("rev:"))
async def reviews_nav(c: types.CallbackQuery):
    parts = c.data.split(":")
    action = parts[1]
    cur_page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

    total = reviews_count()
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
        f"👤 {mask_uid(int(row['tg_id']))}\n"
        f"📦 <b>{row['product_name']}</b>\n"
        f"⭐ Товар: <b>{int(row['rating_product'])}/5</b>\n"
        f"⭐ Сервис: <b>{int(row['rating_service'])}/5</b>\n\n"
        f"{row['text']}"
    )
    await c.message.edit_text(txt, reply_markup=reviews_nav_kb(page, pages))
    await c.answer()

# ---- Reviews (add after purchase)
@dp.callback_query_handler(lambda c: c.data.startswith("rev:add:"))
async def review_add_start(c: types.CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    if not has_purchase(c.from_user.id, pid):
        await c.answer("Отзыв доступен только после покупки", show_alert=True)
        return
    await state.update_data(pid=pid)
    ikb = types.InlineKeyboardMarkup(row_width=5).row(
        *[types.InlineKeyboardButton(str(i), callback_data=f"rev_rp:{i}") for i in range(1, 6)]
    )
    await c.message.edit_text("⭐ Оцените товар (1-5):", reply_markup=ikb)
    await ReviewFlow.rp.set()
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("rev_rp:"), state=ReviewFlow.rp)
async def review_rp(c: types.CallbackQuery, state: FSMContext):
    rp = int(c.data.split(":")[1])
    await state.update_data(rp=rp)
    ikb = types.InlineKeyboardMarkup(row_width=5).row(
        *[types.InlineKeyboardButton(str(i), callback_data=f"rev_rs:{i}") for i in range(1, 6)]
    )
    await c.message.edit_text("⭐ Оцените сервис (1-5):", reply_markup=ikb)
    await ReviewFlow.rs.set()
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("rev_rs:"), state=ReviewFlow.rs)
async def review_rs(c: types.CallbackQuery, state: FSMContext):
    rs = int(c.data.split(":")[1])
    await state.update_data(rs=rs)
    await c.message.edit_text("✍️ Напишите текст отзыва (1-3 предложения):")
    await ReviewFlow.text.set()
    await c.answer()

@dp.message_handler(state=ReviewFlow.text)
async def review_text(m: types.Message, state: FSMContext):
    text = (m.text or "").strip()
    if len(text) < 3:
        await m.answer("Слишком коротко. Напишите чуть подробнее.")
        return
    data = await state.get_data()
    pid = int(data["pid"])
    rp = int(data["rp"])
    rs = int(data["rs"])
    add_review(m.from_user.id, pid, rp, rs, text)
    await state.finish()
    await m.answer("✅ Отзыв добавлен. Спасибо!", reply_markup=main_menu_kb(is_admin(m.from_user.id)))

# ---- Obmenniki / Help
@dp.message_handler(lambda m: m.text == "💱 Обменники")
async def obmenniki(m: types.Message):
    ikb = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("↗️ ltc_exp", url=f"https://t.me/{OBMENNIKI_USERNAME}")
    )
    await m.answer("💱 <b>Проверенный обменник:</b>", reply_markup=ikb)

@dp.message_handler(lambda m: m.text == "🆘 Помощь")
async def help_menu(m: types.Message):
    ikb = types.InlineKeyboardMarkup(row_width=1)
    ikb.add(
        types.InlineKeyboardButton("Support", url=f"https://t.me/{SUPPORT_USERNAME}"),
        types.InlineKeyboardButton("Operator", url=f"https://t.me/{OPERATOR_USERNAME}"),
    )
    await m.answer("🆘 <b>Поддержка</b>", reply_markup=ikb)

# ---- Admin panel
@dp.message_handler(lambda m: m.text == "🛠 Админ-панель")
async def admin_panel(m: types.Message):
    if not is_admin(m.from_user.id):
        await m.answer("❌ Нет доступа.")
        return
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())

@dp.callback_query_handler(lambda c: c.data == "adm:back")
async def adm_back(c: types.CallbackQuery):
    await c.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())
    await c.answer()

@dp.callback_query_handler(lambda c: c.data == "adm:products")
async def adm_products(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await c.message.edit_text("📦 <b>Товары</b>:", reply_markup=admin_products_kb())
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("adm:p:"))
async def adm_product(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    pid = int(c.data.split(":")[2])
    p = get_product(pid)
    if not p:
        await c.answer("Не найдено", show_alert=True); return
    status = "ON ✅" if int(p["is_active"]) == 1 else "OFF ⛔️"
    msg = (
        f"📦 <b>Товар #{pid}</b>\n"
        f"Название: <b>{p['name']}</b>\n"
        f"Цена: <b>{float(p['price_ltc']):.8f} LTC</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"Delivery photo: <b>{'YES' if (p['delivery_photo'] or '').strip() else 'NO'}</b>\n"
        f"Delivery text: <b>{'YES' if (p['delivery_text'] or '').strip() else 'NO'}</b>"
    )
    await c.message.edit_text(msg, reply_markup=admin_product_actions_kb(pid))
    await c.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("adm:toggle:"))
async def adm_toggle(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    pid = int(c.data.split(":")[2])
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT is_active FROM products WHERE id=?", (pid,))
        r = cur.fetchone()
        if not r:
            await c.answer("Не найдено", show_alert=True); return
        new_val = 0 if int(r["is_active"]) == 1 else 1
        cur.execute("UPDATE products SET is_active=? WHERE id=?", (new_val, pid))
        conn.commit()
    await c.answer("OK")
    await c.message.edit_text("📦 <b>Товары</b>:", reply_markup=admin_products_kb())

@dp.callback_query_handler(lambda c: c.data.startswith("adm:price:"))
async def adm_price_start(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    pid = int(c.data.split(":")[2])
    await state.update_data(pid=pid)
    await c.message.edit_text(f"💲 Введите новую цену (LTC) для товара #{pid}\nПример: <code>0.0042</code>")
    await AdminPrice.price.set()
    await c.answer()

@dp.message_handler(state=AdminPrice.price)
async def adm_price_set(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish(); return
    data = await state.get_data()
    pid = int(data["pid"])
    try:
        price = float((m.text or "").replace(",", ".").strip())
        if price <= 0:
            raise ValueError()
    except Exception:
        await m.answer("❌ Неверный формат. Пример: 0.0042")
        return
    with db() as conn:
        conn.execute("UPDATE products SET price_ltc=? WHERE id=?", (price, pid))
        conn.commit()
    await state.finish()
    await m.answer("✅ Цена обновлена.", reply_markup=main_menu_kb(True))

@dp.callback_query_handler(lambda c: c.data.startswith("adm:delivery:"))
async def adm_delivery_start(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    pid = int(c.data.split(":")[2])
    await state.update_data(pid=pid)
    await c.message.edit_text(
        f"🖼 Отправьте <b>URL</b> картинки или <b>Telegram file_id</b> для delivery товара #{pid}\n"
        f"Если не нужно — отправьте <code>-</code>"
    )
    await AdminDelivery.photo.set()
    await c.answer()

@dp.message_handler(state=AdminDelivery.photo)
async def adm_delivery_photo(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish(); return
    photo = (m.text or "").strip()
    if photo == "-":
        photo = ""
    await state.update_data(photo=photo)
    await m.answer("📝 Введите текст инструкции (delivery text):")
    await AdminDelivery.text.set()

@dp.message_handler(state=AdminDelivery.text)
async def adm_delivery_text(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish(); return
    text = (m.text or "").strip()
    if len(text) < 1:
        await m.answer("❌ Текст пустой.")
        return
    data = await state.get_data()
    pid = int(data["pid"])
    photo = (data.get("photo") or "").strip()
    with db() as conn:
        conn.execute("UPDATE products SET delivery_photo=?, delivery_text=? WHERE id=?", (photo, text, pid))
        conn.commit()
    await state.finish()
    await m.answer("✅ Delivery обновлён.", reply_markup=main_menu_kb(True))

@dp.callback_query_handler(lambda c: c.data == "adm:add")
async def adm_add_start(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await c.message.edit_text("➕ Введите название товара:")
    await AdminAdd.name.set()
    await c.answer()

@dp.message_handler(state=AdminAdd.name)
async def adm_add_name(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish(); return
    name = (m.text or "").strip()
    if len(name) < 2:
        await m.answer("Слишком коротко. Введите нормальное название.")
        return
    await state.update_data(name=name)
    await m.answer("💲 Введите цену (LTC). Пример: 0.0042")
    await AdminAdd.price.set()

@dp.message_handler(state=AdminAdd.price)
async def adm_add_price(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish(); return
    try:
        price = float((m.text or "").replace(",", ".").strip())
        if price <= 0:
            raise ValueError()
    except Exception:
        await m.answer("❌ Неверный формат. Пример: 0.0042")
        return
    data = await state.get_data()
    name = data["name"]
    with db() as conn:
        conn.execute("""
            INSERT INTO products(name, price_ltc, is_active, delivery_photo, delivery_text)
            VALUES(?,?,?,?,?)
        """, (name, price, 1, "", "Инструкция: ..."))
        conn.commit()
    await state.finish()
    await m.answer("✅ Товар добавлен.", reply_markup=main_menu_kb(True))

# ---- Fallback (never silent)
@dp.message_handler()
async def fallback(m: types.Message):
    ensure_user(m.from_user.id)
    await m.answer("Выберите пункт меню 👇", reply_markup=main_menu_kb(is_admin(m.from_user.id)))

# =======================
# RUN
# =======================
if __name__ == "__main__":
    init_db()
    log.info("Bot starting polling...")
    executor.start_polling(dp, skip_updates=True)
