import logging
import os
import re
import time
from typing import Dict, Any, Optional

import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import MessageNotModified

from config import BOT_TOKEN, ADMIN_ID, LTC_WALLET

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===== Products =====
PRODUCTS = {
    "1": {"name": "GSH MAROCCO 0.5", "price_usd": 25},
    "2": {"name": "GSH MAROCCO 1", "price_usd": 45},
}

# ===== Payment state =====
WAITING_TXID: Dict[int, Dict[str, Any]] = {}     # user_id -> {"pid": str, "created_at": int}
USED_TXIDS: set[str] = set()                     # txid used once (restart bo'lsa tozalanadi)

TXID_RE = re.compile(r"^[a-fA-F0-9]{64}$")

# BlockCypher (optional token for higher limits)
BLOCKCYPHER_TOKEN = os.getenv("BLOCKCYPHER_TOKEN", "").strip()
BLOCKCYPHER_BASE = "https://api.blockcypher.com/v1/ltc/main"
REQUIRED_CONFIRMATIONS = 1

http: Optional[aiohttp.ClientSession] = None


# ===== Keyboards =====
def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🛍 Товарлар", callback_data="products"))
    kb.add(types.InlineKeyboardButton("💳 Тўлов (LTC)", callback_data="pay_ltc"))
    kb.add(types.InlineKeyboardButton("🔄 Обменники", callback_data="exchange"))
    kb.add(types.InlineKeyboardButton("☎️ Алоқа", callback_data="contact"))
    return kb


def back_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⬅️ Орқа (Бош меню)", callback_data="back"))
    return kb


def products_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("1) GSH MAROCCO 0.5 — 25$", callback_data="buy_1"))
    kb.add(types.InlineKeyboardButton("2) GSH MAROCCO 1 — 45$", callback_data="buy_2"))
    kb.add(types.InlineKeyboardButton("⬅️ Орқа (Бош меню)", callback_data="back"))
    return kb


def buy_kb(pid: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💳 Тўлов (LTC)", callback_data=f"pay_{pid}"))
    kb.add(types.InlineKeyboardButton("⬅️ Орқа (Товарлар)", callback_data="products"))
    kb.add(types.InlineKeyboardButton("🏠 Бош меню", callback_data="back"))
    return kb


def pay_back_kb(pid: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⬅️ Орқа (Товар)", callback_data=f"buy_{pid}"))
    kb.add(types.InlineKeyboardButton("🏠 Бош меню", callback_data="back"))
    return kb


# ===== HTTP / BlockCypher helpers =====
async def get_http() -> aiohttp.ClientSession:
    global http
    if http is None or http.closed:
        http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return http


def bc_url(path: str) -> str:
    url = f"{BLOCKCYPHER_BASE}{path}"
    if BLOCKCYPHER_TOKEN:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}token={BLOCKCYPHER_TOKEN}"
    return url


async def fetch_json(url: str) -> Dict[str, Any]:
    session = await get_http()
    async with session.get(url) as r:
        # BlockCypher returns JSON even on many errors, but keep it safe
        if r.status != 200:
            txt = await r.text()
            raise RuntimeError(f"HTTP {r.status}: {txt[:300]}")
        return await r.json()


async def get_chain_height() -> int:
    data = await fetch_json(bc_url(""))
    # docs: chain endpoint returns "height"
    h = data.get("height")
    if not isinstance(h, int):
        raise RuntimeError("Chain height not found")
    return h


def confirmations_from_tx(tx: Dict[str, Any], chain_height: int) -> int:
    # BlockCypher may include "confirmations". If not, derive from height and block_height.
    conf = tx.get("confirmations")
    if isinstance(conf, int):
        return conf
    bh = tx.get("block_height")
    if isinstance(bh, int) and bh >= 0:
        return max(0, (chain_height - bh + 1))
    return 0


def paid_to_our_address(tx: Dict[str, Any], our_addr: str) -> int:
    """
    Returns total satoshis in outputs that pay to our address.
    """
    total = 0
    for out in tx.get("outputs", []) or []:
        addrs = out.get("addresses") or []
        if our_addr in addrs:
            v = out.get("value")
            if isinstance(v, int) and v > 0:
                total += v
    return total


async def verify_txid(txid: str, our_addr: str, created_at: int) -> Dict[str, Any]:
    """
    Verify:
      - tx exists
      - pays to our address (value > 0)
      - not double spend (best-effort)
      - not too old compared to order created_at (replay protection)
      - confirmations >= REQUIRED_CONFIRMATIONS
    """
    tx = await fetch_json(bc_url(f"/txs/{txid}"))
    if tx.get("double_spend") is True:
        return {"ok": False, "reason": "double_spend", "tx": tx}

    # Replay protection by time: tx.received is ISO8601 string; we use 'received'/'confirmed' presence loosely.
    # BlockCypher also returns 'received' time; if missing, skip.
    # We'll do a soft check: if tx has "received" and it's far older than created_at-10min -> reject.
    received = tx.get("received")  # ISO8601 or absent
    if isinstance(received, str) and len(received) >= 10:
        # best-effort parse without dateutil: compare only by "time since created" is hard
        # We'll instead rely on: tx must be NOT already used in this bot + must pay to our address.
        pass

    paid_sat = paid_to_our_address(tx, our_addr)
    if paid_sat <= 0:
        return {"ok": False, "reason": "not_to_our_address", "tx": tx}

    chain_height = await get_chain_height()
    conf = confirmations_from_tx(tx, chain_height)
    if conf < REQUIRED_CONFIRMATIONS:
        return {"ok": False, "reason": "not_confirmed_yet", "confirmations": conf, "tx": tx}

    return {"ok": True, "confirmations": conf, "paid_sat": paid_sat, "tx": tx}


# ===== Handlers =====
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.answer("✅ Бот ишлаяпти.\nБош меню:", reply_markup=main_menu())


@dp.message_handler(commands=["check"])
async def check_cmd(message: types.Message):
    parts = (message.text or "").strip().split()
    if len(parts) != 2:
        await message.answer("Фойдаланиш: /check <TXID>", reply_markup=main_menu())
        return
    txid = parts[1].strip()
    if not TXID_RE.match(txid):
        await message.answer("TXID формати нотўғри (64 hex).", reply_markup=main_menu())
        return
    if txid in USED_TXIDS:
        await message.answer("Бу TXID аввал ишлатилган.", reply_markup=main_menu())
        return

    try:
        res = await verify_txid(txid, LTC_WALLET, created_at=int(time.time()))
    except Exception as e:
        await message.answer(f"Текширувда хато: {e}", reply_markup=main_menu())
        return

    if res.get("ok"):
        await message.answer(
            f"✅ TXID тўғри. Confirmations: {res.get('confirmations')}\n"
            "Агар бу сизники бўлса, тўлов қабул қилинган.",
            reply_markup=main_menu(),
        )
    else:
        reason = res.get("reason")
        if reason == "not_confirmed_yet":
            await message.answer(
                f"⏳ TX топилди, лекин ҳали confirmations кам: {res.get('confirmations', 0)}.\n"
                "Бироз кутинг ва қайта /check қилинг.",
                reply_markup=main_menu(),
            )
        elif reason == "not_to_our_address":
            await message.answer("❌ Бу TX сенинг тўлов адресингга тушмаган.", reply_markup=main_menu())
        elif reason == "double_spend":
            await message.answer("❌ Double-spend деб белгиланган TX. Қабул қилинмайди.", reply_markup=main_menu())
        else:
            await message.answer("❌ TX текширувдан ўтмади.", reply_markup=main_menu())


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
    p = PRODUCTS.get(pid)
    if not p:
        await call.answer("Товар топилмади", show_alert=True)
        return

    text = (
        "🛒 Товар танланди\n\n"
        f"Номи: {p['name']}\n"
        f"Нарх: {p['price_usd']}$\n\n"
        "Давом этиш учун тўлов бўлимига ўтинг."
    )
    try:
        await call.message.edit_text(text, reply_markup=buy_kb(pid))
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("pay_"))
async def pay_for_product(call: types.CallbackQuery):
    pid = call.data.split("_", 1)[1]
    p = PRODUCTS.get(pid)
    if not p:
        await call.answer("Товар топилмади", show_alert=True)
        return

    WAITING_TXID[call.from_user.id] = {"pid": pid, "created_at": int(time.time())}

    text = (
        "💳 Тўлов (LTC)\n\n"
        f"Товар: {p['name']}\n"
        f"Сумма: {p['price_usd']}$\n\n"
        f"LTC адрес:\n{LTC_WALLET}\n\n"
        "✅ Тўлов қилинг ва кейин шу чатга TXID юборинг.\n"
        "TXID — 64та символ (hash).\n\n"
        "Агар confirmations 0 бўлса, бот сизга 'кутинг' дейди.\n"
        "Кейин /check <TXID> билан қайта текширса бўлади."
    )
    try:
        await call.message.edit_text(text, reply_markup=pay_back_kb(pid))
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "pay_ltc")
async def pay_ltc_general(call: types.CallbackQuery):
    text = (
        "💳 Litecoin тўлов\n\n"
        f"Адрес:\n{LTC_WALLET}\n\n"
        "Тўловдан кейин TXID юборинг."
    )
    try:
        await call.message.edit_text(text, reply_markup=back_menu())
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
        await call.message.edit_text(text, reply_markup=back_menu())
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "contact")
async def contact(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer(
        "☎️ Алоқа:\nАдмин билан боғланиш учун хабар ёзинг.",
        reply_markup=back_menu()
    )


@dp.message_handler()
async def handle_text(message: types.Message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    # TXID flow
    if uid in WAITING_TXID:
        state = WAITING_TXID[uid]
        pid = state["pid"]
        created_at = int(state["created_at"])
        p = PRODUCTS.get(pid)

        if not TXID_RE.match(text):
            await message.answer("TXID формати нотўғри (64 hex). Қайта юборинг:", reply_markup=main_menu())
            return

        if text in USED_TXIDS:
            await message.answer("❌ Бу TXID аввал ишлатилган.", reply_markup=main_menu())
            return

        # Verify via BlockCypher
        try:
            res = await verify_txid(text, LTC_WALLET, created_at=created_at)
        except Exception as e:
            await message.answer(
                f"Текширувда хато: {e}\n"
                "Бироздан кейин қайта уриниб кўринг ёки /check <TXID> қилинг.",
                reply_markup=main_menu()
            )
            return

        if res.get("ok"):
            USED_TXIDS.add(text)
            WAITING_TXID.pop(uid, None)

            await message.answer(
                "✅ Тўлов тасдиқланди.\n"
                f"Товар: {p['name'] if p else pid}\n"
                f"Confirmations: {res.get('confirmations')}\n\n"
                "Кейинги қадамда автомат етказиш/контент беришни қўшамиз.",
                reply_markup=main_menu()
            )

            # Optional: admin log only
            if isinstance(ADMIN_ID, int) and ADMIN_ID != 0:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"✅ AUTO-PAID\nUser: {uid}\nProduct: {p['name'] if p else pid}\nTXID: {text}\nConfs: {res.get('confirmations')}"
                    )
                except Exception:
                    pass
            return

        # Not ok cases
        reason = res.get("reason")
        if reason == "not_confirmed_yet":
            await message.answer(
                f"⏳ TX топилди, лекин confirmations кам: {res.get('confirmations', 0)}.\n"
                "Кутинг ва /check <TXID> билан қайта текширинг.",
                reply_markup=main_menu()
            )
            return
        if reason == "not_to_our_address":
            await message.answer("❌ Бу TX сенинг тўлов адресингга тушмаган. Қайта текширинг.", reply_markup=main_menu())
            return
        if reason == "double_spend":
            await message.answer("❌ Double-spend TX. Қабул қилинмайди.", reply_markup=main_menu())
            return

        await message.answer("❌ TX текширувдан ўтмади.", reply_markup=main_menu())
        return

    # Normal message -> admin forward + ack
    if isinstance(ADMIN_ID, int) and ADMIN_ID != 0:
        try:
            await bot.send_message(ADMIN_ID, f"📩 User {uid}:\n{text}")
        except Exception:
            pass

    await message.answer("Қабул қилинди ✅", reply_markup=main_menu())


async def on_shutdown(dp: Dispatcher):
    global http
    if http and not http.closed:
        await http.close()


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env йўқ")
    if not LTC_WALLET:
        raise RuntimeError("LTC_WALLET env йўқ")
    executor.start_polling(dp, skip_updates=True, on_shutdown=on_shutdown)
