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
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}

DEFAULT_USD_PER_LTC = float(os.getenv("DEFAULT_USD_PER_LTC", "100") or "100")
MIN_CONFIRMATIONS = int(os.getenv("MIN_CONFIRMATIONS", "1") or "1")
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "45") or "45")

DB_PATH = os.getenv("DB_PATH", "shop.sqlite3")

# Tolerance: amount match (LTC)
AMOUNT_TOL_LTC = 0.00000001  # 1 litoshi

# APIs
SOCHAIN_RECEIVED = "https://sochain.com/api/v2/get_tx_received/LTC/{address}"
SOCHAIN_TX = "https://sochain.com/api/v2/get_tx/LTC/{txid}"
COINGECKO_RATE = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd"

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.MARKDOWN)
dp = Dispatcher(bot)


# =========================
# DB HELPERS
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, coldef: str):
    """coldef example: 'delivery_photo_url TEXT' """
    colname = coldef.split()[0]
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}
    if colname not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")


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

        # MIGRATIONS (delivery)
        ensure_column(conn, "products", "delivery_photo_url TEXT")
        ensure_column(conn, "products", "delivery_caption TEXT")
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


def add_product(name: str, description: str, price_usd: float, stock: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO products(name, description, price_usd, stock, is_active) VALUES(?,?,?,?,1)",
            (name, description, price_usd, stock)
        )
        conn.commit()


def set_delivery(pid: int, photo_url: str, caption: str):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE products SET delivery_photo_url=?, delivery_caption=? WHERE id=?",
            (photo_url, caption, pid)
        )
        conn.commit()


def disable_product(pid: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
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
            SELECT o.*, p.name as product_name, p.description as product_desc,
                   p.delivery_photo_url as delivery_photo_url, p.delivery_caption as delivery_caption
            FROM orders o JOIN products p ON p.id=o.product_id
            WHERE o.id=?
        """, (order_id,))
        return cur.fetchone()


def user_orders(tg_id: int, limit: int = 15) -> List[sqlite3.Row]:
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


def pending_orders(limit=50) -> List[sqlite3.Row]:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE status='pending' ORDER BY created_at ASC LIMIT ?", (limit,))
        return cur.fetchall()


def mark_order_paid(order_id: int, txid: str):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET status='paid', paid_at=?, txid=? WHERE id=? AND status='pending'",
            (int(time.time()), txid, order_id)
        )
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


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


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
    for p in list_products(True)[:50]:
        rows.append([types.InlineKeyboardButton(
            text=f"#{p['id']} • {p['name']} • ${float(p['price_usd']):.2f}",
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
    # base LTC => unique by adding 1..50 litoshis
    litoshis = int(round(base * 1e8))
    offset = random.randint(1, 50)
    return (litoshis + offset) / 1e8


def confirmations(tx_data: Dict) -> int:
    try:
        return int(tx_data.get("confirmations", 0))
    except Exception:
        return 0


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


async def try_match_order(o: sqlite3.Row) -> Tuple[bool, Optional[str], int]:
    expected = float(o["amount_ltc"])
    try:
        received = await http_get_json(SOCHAIN_RECEIVED.format(address=LTC_ADDRESS), timeout=15)
        if received.get("status") != "success":
            return (False, None, 0)
        txs = received["data"].get("txs", [])
    except Exception:
        return (False, None, 0)

    for t in txs[:80]:
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


async def send_delivery(tg_id: int, order_id: int):
    """Send delivery photo to buyer after payment."""
    o = get_order(order_id)
    if not o:
        return
    url = (o["delivery_photo_url"] or "").strip()
    caption = (o["delivery_caption"] or "").strip()

    if not url:
        # fallback if admin forgot set delivery
        await bot.send_message(tg_id, f"✅ To‘lov tasdiqlandi. Order #{order_id}\n❗ Delivery rasm sozlanmagan.")
        return

    text = caption if caption else "✅ Xarid uchun rahmat."
    text = f"{text}\n\nOrder: #{order_id}\nTX: `{o['txid']}`" if o["txid"] else f"{text}\n\nOrder: #{order_id}"
    await bot.send_photo(tg_id, photo=url, caption=text)


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

                        # attach txid into order row for delivery message
                        # (re-read order after mark paid)
                        await send_delivery(int(o["tg_id"]), int(o["id"]))

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
    await m.answer(
        "🧑‍💼 *Admin panel*\n\n"
        "1) Product qo‘shish:\n"
        "`/add_product Name | PriceUSD | Stock | Description`\n\n"
        "2) Delivery rasm biriktirish:\n"
        "`/set_delivery ProductID | https://image-url | Caption`\n\n"
        "3) Product disable:\n"
        "`/disable_product ID`\n\n"
        "4) Ro‘yxat:\n"
        "`/list_products`\n\n"
        "5) Pending orderlar:\n"
        "`/pending`\n"
    )


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
        await m.answer("❌ Format:\n`/add_product Name | 25 | 999 | Description`")


@dp.message_handler(commands=["set_delivery"])
async def setdel(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    try:
        raw = m.get_args()
        pid_s, url, caption = [x.strip() for x in raw.split("|", 2)]
        pid = int(pid_s)
        if not url.startswith("http"):
            return await m.answer("❌ URL http/https bo‘lsin.")
        set_delivery(pid, url, caption)
        await m.answer("✅ Delivery rasm biriktirildi.")
    except Exception:
        await m.answer("❌ Format:\n`/set_delivery 2 | https://image-url | Caption`")


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


@dp.message_handler(commands=["list_products"])
async def listprod(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    ps = list_products(active_only=False)
    if not ps:
        return await m.answer("Bo‘sh.")
    lines = []
    for p in ps[:50]:
        lines.append(
            f"#{p['id']} | {'ON' if int(p['is_active'])==1 else 'OFF'} | "
            f"{p['name']} | ${float(p['price_usd']):.2f} | stock:{int(p['stock'])} | "
            f"delivery:{'YES' if (p['delivery_photo_url'] or '') else 'NO'}"
        )
    await m.answer("\n".join(lines))


@dp.message_handler(commands=["pending"])
async def pending(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    os_ = pending_orders(30)
    if not os_:
        return await m.answer("Pending yo‘q.")
    lines = []
    for o in os_:
        lines.append(f"#{o['id']} | user:{o['tg_id']} | pid:{o['product_id']} | {float(o['amount_ltc']):.8f} LTC | ${float(o['amount_usd']):.2f}")
    await m.answer("\n".join(lines))


@dp.message_handler(lambda m: m.text == "🛒 Shop")
async def shop(m: types.Message):
    ensure_user(m.from_user.id)
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


@dp.message_handler(lambda m: m.text == "📦 My Orders")
async def my_orders(m: types.Message):
    ensure_user(m.from_user.id)
    rows = user_orders(m.from_user.id, 15)
    if not rows:
        return await m.answer("Orderlar yo‘q.")
    lines = []
    for o in rows:
        lines.append(
            f"#{o['id']} | {o['product_name']} | {o['status']} | "
            f"{float(o['amount_ltc']):.8f} LTC | ${float(o['amount_usd']):.2f}"
        )
    await m.answer("\n".join(lines))


@dp.message_handler(lambda m: m.text == "ℹ️ Info")
async def info(m: types.Message):
    await m.answer(
        "ℹ️ *To‘lov:* Litecoin (LTC)\n"
        "Bot order uchun aniq LTC miqdorini beradi.\n"
        "To‘lov tasdiqlansa, bot sizga *delivery rasm* yuboradi."
    )


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

    txt = (
        f"🧾 *{p['name']}*\n"
        f"💵 ${float(p['price_usd']):.2f}\n"
        f"📦 Stock: {int(p['stock'])}\n\n"
        f"{p['description']}"
    )
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

    msg = (
        f"🧾 *Order #{order_id}*\n"
        f"📦 {p['name']} x{qty}\n"
        f"💵 ${amount_usd:.2f}\n\n"
        f"➡️ Address: `{LTC_ADDRESS}`\n"
        f"➡️ Amount: *{amount_ltc:.8f} LTC*\n\n"
        f"⚠️ Aynan shu miqdorni yuboring.\n"
        f"Confirmations: {MIN_CONFIRMATIONS}+ bo‘lsa auto tasdiq."
    )
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

        # send delivery
        await send_delivery(c.from_user.id, order_id)

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

    # seed 2 products if empty (optional)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) c FROM products")
        if int(cur.fetchone()["c"]) == 0:
            add_product("Product 1", "Description 1", 25.0, 999)
            add_product("Product 2", "Description 2", 45.0, 999)

    asyncio.create_task(payment_watcher())


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
