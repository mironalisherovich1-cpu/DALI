import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import BOT_TOKEN, ADMIN_ID, LTC_WALLET

dp = Dispatcher()

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Товарлар", callback_data="products")],
        [InlineKeyboardButton(text="💳 Тўлов (LTC)", callback_data="pay_ltc")],
        [InlineKeyboardButton(text="☎️ Алоқа", callback_data="contact")],
    ])

@dp.message(CommandStart())
async def start(m: types.Message):
    await m.answer("✅ Бот ишлаяпти.\nМенюдан танланг:", reply_markup=menu())

@dp.callback_query(F.data == "products")
async def products(c: types.CallbackQuery):
    await c.message.edit_text(
        "🛍 Товарлар:\n1) Product A — 10$\n2) Product B — 25$",
        reply_markup=menu()
    )
    await c.answer()

@dp.callback_query(F.data == "pay_ltc")
async def pay_ltc(c: types.CallbackQuery):
    if not LTC_WALLET:
        await c.message.edit_text("❌ LTC_WALLET env қўйилмаган.", reply_markup=menu())
        await c.answer()
        return
    await c.message.edit_text(
        f"💳 Litecoin тўлов\n\nАдрес:\n`{LTC_WALLET}`\n\nТўловдан кейин TXID юборинг.",
        parse_mode="Markdown",
        reply_markup=menu()
    )
    await c.answer()

@dp.callback_query(F.data == "contact")
async def contact(c: types.CallbackQuery):
    await c.message.edit_text("☎️ Алоқа: админ.", reply_markup=menu())
    await c.answer()

@dp.message()
async def forward_to_admin(m: types.Message):
    if ADMIN_ID:
        try:
            await m.bot.send_message(ADMIN_ID, f"User {m.from_user.id}:\n{m.text}")
        except Exception:
            pass
    await m.answer("Қабул қилинди ✅", reply_markup=menu())

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env йўқ")
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
