import logging
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import MessageNotModified

from config import BOT_TOKEN, ADMIN_ID, LTC_WALLET

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)


def menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🛍 Товарлар", callback_data="products"))
    kb.add(types.InlineKeyboardButton("💳 Тўлов (LTC)", callback_data="pay_ltc"))
    kb.add(types.InlineKeyboardButton("☎️ Алоқа", callback_data="contact"))
    return kb


@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.answer("✅ Бот ишлаяпти.\nМенюдан танланг:", reply_markup=menu())


@dp.callback_query_handler(lambda c: c.data == "products")
async def products(call: types.CallbackQuery):
    text = (
        "🛍 Товарлар:\n"
        "1) Product A — 10$\n"
        "2) Product B — 25$\n\n"
        "Сотиб олиш механикасини кейинги қадамда қўшамиз."
    )
    try:
        await call.message.edit_text(text, reply_markup=menu())
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
        await call.message.edit_text(text, reply_markup=menu())
    except MessageNotModified:
        pass
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "contact")
async def contact(call: types.CallbackQuery):
    # Edit билан эмас — янги хабар билан чиқарамиз (100% ишлайди)
    await call.answer()
    await call.message.answer("☎️ Алоқа: админ", reply_markup=menu())


@dp.message_handler()
async def forward_to_admin(message: types.Message):
    # Админга форвард (ADMIN_ID фақат рақам бўлиши шарт)
    if isinstance(ADMIN_ID, int) and ADMIN_ID != 0:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"📩 User {message.from_user.id}:\n{message.text}"
            )
        except Exception:
            pass

    await message.answer("Қабул қилинди ✅", reply_markup=menu())


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env йўқ (Railway Variables'га қўй)")
    if not LTC_WALLET:
        logging.warning("LTC_WALLET env йўқ — 'Тўлов (LTC)' бўлимида адрес чиқмайди.")
    executor.start_polling(dp, skip_updates=True)
