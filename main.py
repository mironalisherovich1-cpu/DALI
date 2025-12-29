import asyncio
import json
import logging
import os
import random
import time
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional, List

import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import MessageNotModified

from config import BOT_TOKEN, ADMIN_ID, LTC_WALLET

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# =========================
# SETTINGS
# =========================
REQUIRED_CONFIRMATIONS = 1
POLL_INTERVAL_SEC = 30

# Unique amount "salt" (in litoshi): 1 litoshi = 0.00000001 LTC
# We'll add 700..9900 litoshi (~0.00000700..0.00009900 LTC) to make each invoice unique.
SALT_MIN_LITOSHI = 700
SALT_MAX_LITOSHI = 9900

# Persist state to file (Railway container may restart; this helps when it doesn't wipe)
STATE_FILE = "state.json"

# BlockCypher (optional token for higher limits)
BLOCKCYPHER_TOKEN = os.getenv("BLOCKCYPHER_TOKEN", "").strip()
BC_BASE = "https://api.blockcypher.com/v1/ltc/main"

# CoinGecko rate (free, no key typically)
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd"


# =========================
# PRODUCTS
# =========================
PRODUCTS = {
    "1": {"name": "GSH MAROCCO 0.5", "price_usd": 25},
    "2": {"name": "GSH MAROCCO 1", "price_usd": 45},
}


# =========================
# STATE
# =========================
STATE: Dict[str, Any] = {
    "order_seq": 0,
    "pending": {},   # order_id(str) -> order dict
    "paid": {},      # order_id(str) -> order dict
    "seen_tx": [],   # list of tx_hash already counted (best-effort)
}

http: Optional[aiohttp.ClientSession] = None


def load_state():
    global STATE
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                STATE = json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load state: {e}")


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Failed to save state: {e}")


def new_order_id() -> int:
    STATE["order_seq"] = int(STATE.get("order_seq", 0)) + 1
    save_state()
    return STATE["order_seq"]


def ltc_to_litoshi(ltc: Decimal) -> int:
    # 1 LTC = 100,000,000 litoshi
    return int((ltc * Decimal("100000000")).to_integral_value(rounding=ROUND_DOWN))


def litoshi_to_ltc_str(litoshi: int) -> str:
    ltc = Decimal(litoshi) / Decimal("100000000")
    # show up to 8 decimals
    s = f"{ltc:.8f}"
    # trim trailing zeros
    s = s.rstrip("0").rstrip(".")
    return s


async def get_http() -> aiohttp.ClientSession:
    global http
    if http is None or http.closed:
        http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    return http


def bc_url(path: str) -> str:
    url = f"{BC_BASE}{path}"
    if BLOCKCYPHER_TOKEN:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}token={BLOCKCYPHER_TOKEN}"
    return url


async def fetch_json(url: str) -> Dict[str, Any]:
    session = await get_http()
    async with session.get(url) as r:
        txt = await r.text()
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {txt[:300]}")
        try:
            return json.loads(txt)
        except Exception:
            raise RuntimeError(f"Non-JSON response: {txt[:200]}")


async def get_ltc_usd_rate() -> Decimal:
    """
    Returns: 1 LTC in USD
    """
    data = await fetch_json(COINGECKO_URL)
    usd = data.get("litecoin", {}).get("usd")
    if usd is None:
        raise RuntimeError("Rate not found")
    return Decimal(str(usd))


async def get_chain_height() -> int:
    data = await fetch_json(bc_url(""))
    h = data.get("height")
    if not isinstance(h, int):
        raise RuntimeError("Chain height not found")
    return h


def confirmations_from_tx(tx: Dict[str, Any], chain_height: int) -> int:
    conf = tx.get("confirmations")
    if isinstance(conf, int):
        return conf
    bh = tx.get("block_height")
    if isinstance(bh, int) and bh >= 0:
        return max(0, (chain_height - bh + 1))
    return 0


def total_paid_to_address_litoshi(tx: Dict[str, Any], addr: str) -> int:
    total = 0
    for out in tx.get("outputs", []) or []:
        addrs = out.get("addresses") or []
        if addr in addrs:
            v = out.get("value")
            if isinstance(v, int) and v > 0:
                total += v
    return total


async def fetch_recent_txs_for_address(addr: str, limit: int = 50) -> List[Dict[str, Any]]:
    # /addrs/<addr>/full returns txs (may be heavy; limit helps)
    data = await fetch_json(bc_url(f"/addrs/{addr}/full?limit={limit}"))
    txs = data.get("txs") or []
    if not isinstance(txs, list):
        return []
    return txs


# =========================
# KEYBOARDS
# =========================
def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🛍 Товарлар", callback_data="products"))
    kb.add(types.InlineKeyboardButton("📦 Менинг заказларим", callback_data="my_orders"))
    kb.add(types.InlineKeyboardButton("🔄 Обменники", callback_data="exchange"))
    kb.add(types.InlineKeyboardButton("☎️ Алоқа", callback_data="contact"))
    return kb


def back_main_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⬅️ Орқа (Бош меню)", callback_data="back"))
    return kb


def products_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("1) GSH MAROCCO 0.5 — 25$", callback_data="buy_1"))
    kb.add(types.InlineKeyboardButton("2) GSH MAROCCO 1 — 45$", callback_data="buy_2"))
    kb.add(types.InlineKeyboardButton("⬅️ Орқа (Бош меню)", callback_data="back"))
    return kb


def invoice_kb(order_id: int):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔄 Текшириш (ҳозир)", callback_data=f"check_{order_id}"))
    kb.add(types.InlineKeyboardButton("🏠 Бош меню", callback_data="back"))
    return kb


# =========================
# ORDER HELPERS
# =========================
def make_order(user: types.User, pid: str, need_litoshi: int, salt_litoshi: int) -> Dict[str, Any]:
    oid = new_order_id()
    p = PRODUCTS[pid]
    order = {
        "order_id": oid,
        "user_id": user.id,
        "username": user.username,
        "pid": pid,
        "product_name": p["name"],
        "price_usd": p["price_usd"],
        "need_litoshi": need_litoshi,
        "salt_litoshi": salt_litoshi,
        "total_litoshi": need_litoshi + salt_litoshi,
        "address": LTC_WALLET,
        "status": "PENDING",
        "created_at": int(time.time()),
        "paid_tx": None,
        "confirmations": 0,
    }
    return order


def store_pending(order: Dict[str, Any]):
    STATE["pending"][str(order["order_id"])] = order
    save_state()


def mark_paid(order_id: int, tx_hash: str, confirmations: int):
    oid = str(order_id)
    order = STATE["pending"].get(oid)
    if not order:
        return
    order["status"] = "PAID"
    order["paid_tx"] = tx_hash
    order["confirmations"] = confirmations
    STATE["paid"][oid] = order
    STATE["pending"].pop(oid, None)
    save_state()


def find_user_orders_text(user_id: int) -> str:
    pend = [o for o in STATE["pending"].values() if o.get("user_id") == user_id]
    paid = [o for o in STATE["paid"].values() if o.get("user_id") == user_id]

    lines = []
    if not pend and not paid:
        return "Сизда ҳали заказ йўқ."

    if pend:
        lines.append("⏳ Pending заказлар:")
        for o in sorted(pend, key=lambda x: x.get("order_id", 0)):
            lines.append(
                f"• #{o['order_id']} — {o['product_name']} — {o['price_usd']}$ — "
                f"{litoshi_to_ltc_str(o['total_litoshi'])} LTC"
            )
    if paid:
        lines.append("\n✅ Тасдиқланган заказлар:")
        for o in sorted(paid, key=lambda x: x.get("order_id", 0)):
            lines.append(
                f"• #{o['order_id']} — {o['product_name']} — PAID (conf: {o.get('confirmations', 0)})"
            )
    return "\n".join(lines)


# =========================
# BACKGROUND PAYMENT CHECK
# =========================
async def check_payments_once():
    """
    Pull recent txs to LTC_WALLET and match against pending orders by total_litoshi.
    """
    if not LTC_WALLET:
        return
    if not STATE["pending"]:
        return

    try:
        chain_h = await get_chain_height()
        txs = await fetch_recent_txs_for_address(LTC_WALLET, limit=50)
    except Exception as e:
        logging.warning(f"Payment check failed: {e}")
        return

    seen_tx = set(STATE.get("seen_tx", []))
    pending_orders = list(STATE["pending"].values())

    # Build lookup by expected amount
    by_amount: Dict[int, List[Dict[str, Any]]] = {}
    for o in pending_orders:
        by_amount.setdefault(int(o["total_litoshi"]), []).append(o)

    for tx in txs:
        tx_hash = tx.get("hash")
        if not isinstance(tx_hash, str):
            continue

        paid_litoshi = total_paid_to_address_litoshi(tx, LTC_WALLET)
        if paid_litoshi <= 0:
            continue

        # match only if exact amount matches a pending order
        if paid_litoshi not in by_amount:
            continue

        conf = confirmations_from_tx(tx, chain_h)
        if conf < REQUIRED_CONFIRMATIONS:
            continue

        # Prevent counting same tx multiple times (best-effort)
        if tx_hash in seen_tx:
            continue

        # Choose the oldest pending order with that amount
        candidates = sorted(by_amount[paid_litoshi], key=lambda x: x.get("created_at", 0))
        if not candidates:
            continue

        order = candidates[0]
        order_id = int(order["order_id"])
        user_id = int(order["user_id"])

        # Mark paid
        mark_paid(order_id, tx_hash, conf)

        # Remember tx
        seen_tx.add(tx_hash)
        STATE["seen_tx"] = list(seen_tx)[-500:]  # keep last 500
        save_state()

        # Notify user
        try:
            await bot.send_message(
                user_id,
                f"✅ Тўлов автоматик тасдиқланди.\n"
                f"Заказ #{order_id}\n"
                f"Товар: {order['product_name']}\n"
                f"Тушган сумма: {litoshi_to_ltc_str(paid_litoshi)} LTC\n"
                f"Confirmations: {conf}\n\n"
                f"🏠 Бош меню: /start",
                reply_markup=main_menu()
            )
        except Exception as e:
            logging.warning(f"Notify user failed: {e}")

        # Optional admin info
        if isinstance(ADMIN_ID, int) and ADMIN_ID != 0:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"✅ AUTO-CONFIRM\nOrder #{order_id}\nUser: {user_id}\n"
                    f"Amount: {litoshi_to_ltc_str(paid_litoshi)} LTC\nTX: {tx_hash}"
                )
            except Exception:
                pass


async def payments_loop():
    while True:
        await check_payments_once()
        await asyncio.sleep(POLL_INTERVAL_SEC)


# =========================
# HANDLERS
# =========================
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.answer("✅ Бот ишлаяпти.\nБош меню:", reply_markup=main_menu())


@dp.callback_query_handler(lambda c: c.data == "back")
async def back(call: types.CallbackQuery):
    try:
        await call.message.edit_text("🏠 Бош меню:", reply_markup=main_menu())
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "products")
async def products(call: types.CallbackQuery):
    try:
        await call.message.edit_text("🛍 Товарлар (танланг):", reply_markup=products_kb())
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("buy_"))
async def buy(call: types.CallbackQuery):
    pid = call.data.split("_", 1)[1]
    if pid not in PRODUCTS:
        await call.answer("Товар топилмади", show_alert=True)
        return

    p = PRODUCTS[pid]

    # 1) get rate
    try:
        ltc_usd = await get_ltc_usd_rate()  # 1 LTC in USD
    except Exception as e:
        await call.answer()
        await call.message.answer(f"Курсни олишда хато: {e}", reply_markup=main_menu())
        return

    # 2) compute needed LTC (USD / (USD per LTC))
    usd = Decimal(str(p["price_usd"]))
    need_ltc = (usd / ltc_usd)

    # round DOWN to 8 decimals in litoshi
    need_litoshi = ltc_to_litoshi(need_ltc)

    # 3) add salt
    salt_litoshi = random.randint(SALT_MIN_LITOSHI, SALT_MAX_LITOSHI)

    order = make_order(call.from_user, pid, need_litoshi, salt_litoshi)
    store_pending(order)

    total_ltc_str = litoshi_to_ltc_str(order["total_litoshi"])
    rate_str = f"{ltc_usd:.2f}"

    text = (
        "🧾 Invoice (TXID керак эмас)\n\n"
        f"Заказ #{order['order_id']}\n"
        f"Товар: {order['product_name']}\n"
        f"Нарх: {order['price_usd']}$\n"
        f"Курс: 1 LTC = {rate_str} USD\n\n"
        f"✅ Тўлашингиз керак бўлган сумма (аниқ):\n"
        f"**{total_ltc_str} LTC**\n\n"
        f"📩 Адрес:\n{LTC_WALLET}\n\n"
        f"⚠️ Фақат шу аниқ суммани юборинг.\n"
        f"Тушиши билан бот автоматик тасдиқлайди (conf ≥ {REQUIRED_CONFIRMATIONS})."
    )

    try:
        await call.message.edit_text(text, reply_markup=invoice_kb(order["order_id"]), parse_mode="Markdown")
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("check_"))
async def manual_check(call: types.CallbackQuery):
    oid = call.data.split("_", 1)[1]
    order = STATE["pending"].get(str(oid)) or STATE["paid"].get(str(oid))
    if not order:
        await call.answer("Заказ топилмади", show_alert=True)
        return

    if str(oid) in STATE["paid"]:
        await call.answer("Аллақачон тасдиқланган ✅", show_alert=True)
        return

    await call.answer("Текширяпман...")
    await check_payments_once()
    # After one check, show status
    if str(oid) in STATE["paid"]:
        await call.message.answer(f"✅ Заказ #{oid} тасдиқланди.", reply_markup=main_menu())
    else:
        await call.message.answer("⏳ Ҳали тушмади ёки confirmations кам. Кейинроқ қайта текширинг.", reply_markup=main_menu())


@dp.callback_query_handler(lambda c: c.data == "my_orders")
async def my_orders(call: types.CallbackQuery):
    text = find_user_orders_text(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=back_main_kb())
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "exchange")
async def exchange(call: types.CallbackQuery):
    text = (
        "🔄 Обменники (LTC → USDT / UZS)\n\n"
        "• Binance P2P\n"
        "• OKX P2P\n"
        "• Bybit P2P\n\n"
        "⚠️ Фақат ишончли P2P сотувчилардан фойдаланинг."
    )
    try:
        await call.message.edit_text(text, reply_markup=back_main_kb())
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "contact")
async def contact(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer("☎️ Алоқа:\nАдмин билан боғланиш учун хабар ёзинг.", reply_markup=back_main_kb())


@dp.message_handler()
async def any_text(message: types.Message):
    # normal chat forward to admin (optional)
    if isinstance(ADMIN_ID, int) and ADMIN_ID != 0:
        try:
            await bot.send_message(ADMIN_ID, f"📩 User {message.from_user.id}:\n{message.text}")
        except Exception:
            pass
    await message.answer("Менюдан фойдаланинг:", reply_markup=main_menu())


async def on_startup(_dp: Dispatcher):
    load_state()
    if not LTC_WALLET:
        raise RuntimeError("LTC_WALLET env йўқ")

    # Start background payments loop
    asyncio.create_task(payments_loop())
    logging.info("Started payments loop")


async def on_shutdown(_dp: Dispatcher):
    global http
    save_state()
    if http and not http.closed:
        await http.close()


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env йўқ")
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
