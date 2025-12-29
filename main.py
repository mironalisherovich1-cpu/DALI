import logging
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import MessageNotModified

from config import BOT_TOKEN, ADMIN_ID, LTC_WALLET

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===== Data (A variant) =====
PRODUCTS = {
    "1": {"name": "GSH MAROCCO 0.5", "price": 25},
    "2": {"name": "GSH MAROCCO 1", "price": 45},
}

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


# ===== Handlers =====
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
        await call.message.edit_text(
            "🛍 Товарлар (танланг):",
            reply_markup=products_kb()
        )
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
        f"Нарх: {p['price']}$\n\n"
        "Давом этиш учун тўлов бўлимига ўтинг."
    )
    try:
        await call.message.edit_text(text, reply_markup=buy_kb(pid))
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("pay_"))
async def pay_for_product(call: types.CallbackQuery):
    # pay_1, pay_2 ва шу каби
    pid = call.data.split("_", 1)[1]
    p = PRODUCTS.get(pid)
    if not p:
        await call.answer("Товар топилмади", show_alert=True)
        return

    text = (
        "💳 Тўлов (LTC)\n\n"
        f"Товар: {p['name']}\n"
        f"Сумма: {p['price']}$\n\n"
        f"LTC адрес:\n{LTC_WALLET}\n\n"
        "Тўловдан кейин TXID юборинг."
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
async def forward_to_admin(message: types.Message):
    # Админга хабар юбориш (ADMIN_ID рақам бўлиши шарт)
    if isinstance(ADMIN_ID, int) and ADMIN_ID != 0:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"📩 User {message.from_user.id}:\n{message.text}"
            )
        except Exception:
            pass

    await message.answer("Қабул қилинди ✅", reply_markup=main_menu())


# ===== Run =====
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env йўқ")
    if not LTC_WALLET:
        logging.warning("LTC_WALLET env йўқ — тўлов адреси чиқмайди.")
    executor.start_polling(dp, skip_updates=True)
