import logging
from aiogram import Bot, Dispatcher, executor, types
from config import BOT_TOKEN, ADMIN_ID, LTC_WALLET

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def menu():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🛍 Товарлар", callback_data="products"))
    kb.add(types.InlineKeyboardButton("💳 Тўлов (LTC)", callback_data="pay_ltc"))
    kb.add(types.InlineKeyboardButton("☎️ Алоқа", callback_data="contact"))
    return kb

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.answer("✅ Бот ишлаяпти.\nМенюдан танланг:", reply_markup=menu())

@dp.callback_query_handler(lambda c: c.data == "products")
async def products(call: types.CallbackQuery):
    await call.message.edit_text(
        "🛍 Товарлар:\n1) Product A — 10$\n2) Product B — 25$",
        reply_markup=menu()
    )

@dp.callback_query_handler(lambda c: c.data == "pay_ltc")
async def pay_ltc(call: types.CallbackQuery):
    await call.message.edit_text(
        f"💳 Litecoin тўлов\n\nАдрес:\n{LTC_WALLET}\n\nTXID юборинг.",
        reply_markup=menu()
    )

@dp.callback_query_handler(lambda c: c.data == "contact")
async def contact(call: types.CallbackQuery):
    await call.message.edit_text("☎️ Алоқа: админ", reply_markup=menu())

@dp.message_handler()
async def forward(message: types.Message):
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"User {message.from_user.id}:\n{message.text}")
    await message.answer("Қабул қилинди ✅", reply_markup=menu())

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
