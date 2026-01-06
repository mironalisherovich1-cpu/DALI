import os
import time
import sqlite3
import logging
import hashlib
import requests

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from bip_utils import Bip84, Bip84Coins, Bip44Changes

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")
LTC_XPUB = (os.getenv("LTC_XPUB") or "").strip()

DB = "shop.sqlite3"
CITIES = ["Buxoro", "Navoiy", "Samarqand", "Toshkent"]
BC_ADDR = "https://api.blockcypher.com/v1/ltc/main/addrs/{address}"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not LTC_XPUB:
    raise RuntimeError("LTC_XPUB missing")

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ---------- base58check (zpub -> xpub normalize) ----------
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    h = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + h

def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    pad = 0
    for bb in b:
        if bb == 0:
            pad += 1
        else:
            break
    return "1" * pad + (s or "")

def b58check_decode(s: str) -> bytes:
    raw = b58decode(s)
    if len(raw) < 4:
        raise ValueError("bad b58check")
    data, chk = raw[:-4], raw[-4:]
    h = hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]
    if h != chk:
        raise ValueError("bad checksum")
    return data

def b58check_encode(data: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]
    return b58encode(data + chk)

def normalize_extpub(exkey: str) -> str:
    exkey = exkey.strip()
    if exkey.lower().startswith("zpub"):
        data = b58check_decode(exkey)
        # zpub -> xpub version bytes
        xpub_ver = bytes.fromhex("0488b21e")
        return b58check_encode(xpub_ver + data[4:])
    return exkey

def derive_addr(index: int) -> str:
    key = normalize_extpub(LTC_XPUB)
    ctx = Bip84.FromExtendedKey(key, Bip84Coins.LITECOIN)
    return ctx.Change(Bip44Changes.CHAIN_EXT).AddressIndex(index).PublicKey().ToAddress()

# ---------- DB ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY,
            city TEXT NOT NULL,
            idx INTEGER NOT NULL,
            addr TEXT NOT NULL
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS balances(
            tg_id INTEGER PRIMARY KEY,
            ltc REAL NOT NULL DEFAULT 0
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS credited(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            addr TEXT NOT NULL,
            tx TEXT NOT NULL,
            value_sat INTEGER NOT NULL,
            UNIQUE(addr, tx, value_sat)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price_ltc REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )""")
        cur.execute("SELECT COUNT(*) c FROM products")
        if int(cur.fetchone()["c"]) == 0:
            cur.executemany(
                "INSERT INTO products(name,price_ltc,is_active) VALUES(?,?,1)",
                [
                    ("Product A", 0.0035),
                    ("Product B", 0.0056),
                    ("Product C", 0.0084),
                    ("Product D", 0.0063),
                ],
            )
        conn.commit()

def ensure_user(tg_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute("SELECT MAX(idx) mx FROM users")
        mx = cur.fetchone()["mx"]
        idx = int(mx) + 1 if mx is not None else 0
        addr = derive_addr(idx)
        cur.execute("INSERT INTO users(tg_id,city,idx,addr) VALUES(?,?,?,?)", (tg_id, CITIES[0], idx, addr))
        cur.execute("INSERT OR IGNORE INTO balances(tg_id,ltc) VALUES(?,0)", (tg_id,))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return cur.fetchone()

def get_balance(tg_id: int) -> float:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ltc FROM balances WHERE tg_id=?", (tg_id,))
        r = cur.fetchone()
        return float(r["ltc"]) if r else 0.0

def add_balance(tg_id: int, amt: float):
    with db() as conn:
        conn.execute("UPDATE balances SET ltc=ltc+? WHERE tg_id=?", (amt, tg_id))
        conn.commit()

# ---------- UI ----------
def menu(admin=False):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("👤 Профиль", "💰 Баланс")
    kb.row("🛍 Витрина")
    if admin:
        kb.row("🛠 Админ")
    return kb

# ---------- Blockchain check ----------
def fetch_incoming(addr: str):
    r = requests.get(BC_ADDR.format(address=addr), timeout=20).json()
    txrefs = r.get("txrefs", []) or []
    res = []
    for t in txrefs:
        if int(t.get("tx_input_n", 0)) != -1:
            continue
        if int(t.get("confirmations", 0)) < 1:
            continue
        res.append((t.get("tx_hash"), int(t.get("value", 0))))
    return res

def credit_new(tg_id: int, addr: str) -> int:
    credited = 0
    outs = fetch_incoming(addr)
    with db() as conn:
        cur = conn.cursor()
        for tx, val in outs:
            try:
                cur.execute("INSERT INTO credited(tg_id,addr,tx,value_sat) VALUES(?,?,?,?)", (tg_id, addr, tx, val))
                add_balance(tg_id, val / 1e8)
                credited += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    return credited

# ---------- Handlers ----------
@dp.message_handler(commands=["start"])
async def start(m: types.Message):
    u = ensure_user(m.from_user.id)
    await m.answer(
        "✅ <b>Бот работает (SAFE MODE)</b>\n"
        "Если что-то не отвечает — значит процесс падал раньше. Сейчас он живой.\n\n"
        f"Ваш адрес для пополнения:\n<code>{u['addr']}</code>",
        reply_markup=menu(m.from_user.id == ADMIN_ID)
    )

@dp.message_handler(lambda m: m.text == "👤 Профиль")
async def prof(m: types.Message):
    u = ensure_user(m.from_user.id)
    bal = get_balance(m.from_user.id)
    await m.answer(
        f"👤 <b>Профиль</b>\n"
        f"ID: <code>{m.from_user.id}</code>\n"
        f"Город: <b>{u['city']}</b>\n"
        f"Адрес:\n<code>{u['addr']}</code>\n"
        f"Баланс: <b>{bal:.8f} LTC</b>"
    )

@dp.message_handler(lambda m: m.text == "💰 Баланс")
async def bal(m: types.Message):
    u = ensure_user(m.from_user.id)
    bal = get_balance(m.from_user.id)
    ikb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔄 Проверить", callback_data="chk"))
    await m.answer(
        f"💰 Баланс: <b>{bal:.8f} LTC</b>\n\n"
        f"Пополнение отправляйте на:\n<code>{u['addr']}</code>",
        reply_markup=ikb
    )

@dp.callback_query_handler(lambda c: c.data == "chk")
async def chk(c: types.CallbackQuery):
    u = ensure_user(c.from_user.id)
    n = await asyncio.to_thread(credit_new, c.from_user.id, u["addr"])
    bal = get_balance(c.from_user.id)
    await c.answer(f"Зачислено: {n}", show_alert=True)
    await bot.send_message(c.from_user.id, f"✅ Проверка завершена. Баланс: <b>{bal:.8f} LTC</b>")

@dp.message_handler(lambda m: m.text == "🛍 Витрина")
async def shop(m: types.Message):
    with db() as conn:
        items = conn.execute("SELECT * FROM products WHERE is_active=1").fetchall()
    ikb = types.InlineKeyboardMarkup()
    for p in items:
        ikb.add(types.InlineKeyboardButton(f"{p['name']} — {float(p['price_ltc']):.8f} LTC", callback_data=f"p:{p['id']}"))
    await m.answer("🛍 Товары:", reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data.startswith("p:"))
async def pinfo(c: types.CallbackQuery):
    pid = int(c.data.split(":")[1])
    with db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    await c.message.edit_text(
        f"📦 <b>{p['name']}</b>\nЦена: <b>{float(p['price_ltc']):.8f} LTC</b>\n\n(Покупку подключим после того как всё стабильно заработает.)"
    )
    await c.answer()

@dp.message_handler(lambda m: m.text == "🛠 Админ")
async def adm(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("Нет доступа")
    await m.answer("🛠 Админ (SAFE MODE): пока только проверка что меню живое.")

@dp.message_handler()
async def fallback(m: types.Message):
    await m.answer("Выберите пункт меню 👇", reply_markup=menu(m.from_user.id == ADMIN_ID))

# ---------- RUN ----------
if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
