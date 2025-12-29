import logging
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import MessageNotModified

from config import BOT_TOKEN, ADMIN_ID, LTC_WALLET

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

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


# ===== Handlers =====

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.answer(
        "✅ Бот ишлаяпти.\nБош меню:",
        reply_markup=main_menu()
    )


@dp.callback_query_handler(lambda c: c.data == "back")
async def back(call: types.CallbackQuery):
    try:
        await call.message.edit_text(
            "🏠 Бош меню:",
            reply_markup=main_menu()
        )
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "products")
async def products(call: types.CallbackQuery):
    text = (
        "🛍 Товарлар:\n"
        "1) Product A — 10$\n"
        "2) Product B — 25$\n\n"
        "Сотиб олиш кейинги қадамда қўшилади."
    )
    try:
        await call.message.edit_text(text, reply_markup=back_menu())
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "pay_ltc")
async def pay_ltc(call: types.CallbackQuery):
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
    executor.start_polling(dp, skip_updates=True)
