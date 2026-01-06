import os
import time
import random
import sqlite3
import requests
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
LTC_WALLET = os.getenv("LTC_WALLET")

OBMENNIKI_USERNAME = "ltc_exp"
SUPPORT_USERNAME = "qwerty7777jass"
OPERATOR_USERNAME = "qwerty7777jass"

CITIES = ["Buxoro", "Navoiy", "Samarqand", "Toshkent"]
DB = "shop.db"

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ================== DB ==================
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
            city TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            price REAL,
            is_active INTEGER
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            product_id INTEGER,
            amount REAL,
            status TEXT
        )
        """)
        conn.commit()

        cur.execute("SELECT COUNT(*) c FROM products")
        if cur.fetchone()["c"] == 0:
            cur.executemany(
                "INSERT INTO products(name,price,is_active) VALUES(?,?,1)",
                [
                    ("GW MAROCCO 0.5", 25),
                    ("GW MAROCCO 1", 45),
                    ("AMNESIA TYSON", 40),
                    ("RGP 300 (5 шт)", 60),
                ]
            )
            conn.commit()

def get_user(tg_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        u = cur.fetchone()
        if not u:
            cur.execute("INSERT INTO users VALUES(?,?)", (tg_id, CITIES[0]))
            conn.commit()
            return {"tg_id": tg_id, "city": CITIES[0]}
        return u

def set_city(tg_id, city):
    with db() as conn:
        conn.execute("UPDATE users SET city=? WHERE tg_id=?", (city, tg_id))
        conn.commit()

# ================== KEYBOARDS ==================
def main_menu(admin=False):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("👤 Мой профиль")
    kb.add("🛍 Витрина", "💱 Обменники")
    kb.add("⭐ Отзывы", "🆘 Помощь")
    kb.add("💬 Канал", "💼 Работа")
    if admin:
        kb.add("🛠 Админ")
    return kb

# ================== START ==================
@dp.message_handler(commands=["start"])
async def start(m: types.Message):
    get_user(m.from_user.id)
    await m.answer(
        "✅ <b>Добро пожаловать!</b>\nВыберите пункт меню:",
        reply_markup=main_menu(m.from_user.id == ADMIN_ID)
    )

# ================== PROFILE ==================
async def send_profile(chat_id, user_id):
    u = get_user(user_id)
    ikb = types.InlineKeyboardMarkup()
    ikb.add(types.InlineKeyboardButton("🔄 Изменить город", callback_data="city_change"))
    ikb.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu"))
    await bot.send_message(
        chat_id,
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"🏙 Город: <b>{u['city']}</b>",
        reply_markup=ikb
    )

@dp.message_handler(lambda m: m.text == "👤 Мой профиль")
async def profile(m: types.Message):
    await send_profile(m.chat.id, m.from_user.id)

@dp.callback_query_handler(lambda c: c.data == "city_change")
async def city_change(c: types.CallbackQuery):
    ikb = types.InlineKeyboardMarkup(row_width=2)
    for city in CITIES:
        ikb.insert(types.InlineKeyboardButton(city, callback_data=f"set_city:{city}"))
    ikb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="back_profile"))
    await c.message.edit_text("🏙 Выберите город:", reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data.startswith("set_city:"))
async def set_city_cb(c: types.CallbackQuery):
    city = c.data.split(":")[1]
    set_city(c.from_user.id, city)
    ikb = types.InlineKeyboardMarkup()
    ikb.add(types.InlineKeyboardButton("⬅️ В профиль", callback_data="back_profile"))
    ikb.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="go_menu"))
    await c.message.edit_text(f"✅ Город изменён: <b>{city}</b>", reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data == "back_profile")
async def back_profile(c: types.CallbackQuery):
    await c.message.delete()
    await send_profile(c.from_user.id, c.from_user.id)

@dp.callback_query_handler(lambda c: c.data == "go_menu")
async def go_menu(c: types.CallbackQuery):
    await c.message.delete()
    await bot.send_message(
        c.from_user.id,
        "🏠 Главное меню:",
        reply_markup=main_menu(c.from_user.id == ADMIN_ID)
    )

# ================== VITRINA ==================
@dp.message_handler(lambda m: m.text == "🛍 Витрина")
async def shop(m: types.Message):
    with db() as conn:
        items = conn.execute("SELECT * FROM products WHERE is_active=1").fetchall()
    ikb = types.InlineKeyboardMarkup()
    for p in items:
        ikb.add(types.InlineKeyboardButton(
            f"{p['name']} — ${p['price']}",
            callback_data=f"buy:{p['id']}"
        ))
    await m.answer("🛍 <b>Доступные товары:</b>", reply_markup=ikb)

@dp.callback_query_handler(lambda c: c.data.startswith("buy:"))
async def buy(c: types.CallbackQuery):
    pid = int(c.data.split(":")[1])
    with db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return
    order_amount = p["price"]
    with db() as conn:
        conn.execute(
            "INSERT INTO orders(tg_id,product_id,amount,status) VALUES(?,?,?,?)",
            (c.from_user.id, pid, order_amount, "PENDING")
        )
        conn.commit()

    await c.message.edit_text(
        f"💳 <b>Оплата</b>\n\n"
        f"Товар: <b>{p['name']}</b>\n"
        f"Сумма: <b>${order_amount}</b>\n\n"
        f"LTC адрес:\n<code>{LTC_WALLET}</code>\n\n"
        f"После оплаты нажмите кнопку ниже.",
        reply_markup=types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("🔄 Проверить оплату", callback_data="paid")
        )
    )

@dp.callback_query_handler(lambda c: c.data == "paid")
async def paid(c: types.CallbackQuery):
    await c.answer("⏳ Проверка…", show_alert=True)

# ================== OBMENNIKI ==================
@dp.message_handler(lambda m: m.text == "💱 Обменники")
async def obmenniki(m: types.Message):
    ikb = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("↗️ ltc_exp", url="https://t.me/ltc_exp")
    )
    await m.answer("💱 <b>Проверенный обменник:</b>", reply_markup=ikb)

# ================== HELP ==================
@dp.message_handler(lambda m: m.text == "🆘 Помощь")
async def help_menu(m: types.Message):
    ikb = types.InlineKeyboardMarkup()
    ikb.add(types.InlineKeyboardButton("Support", url="https://t.me/qwerty7777jass"))
    ikb.add(types.InlineKeyboardButton("Operator", url="https://t.me/qwerty7777jass"))
    await m.answer("🆘 <b>Поддержка</b>", reply_markup=ikb)

# ================== FALLBACK ==================
@dp.message_handler()
async def fallback(m: types.Message):
    await m.answer("Выберите пункт меню 👇", reply_markup=main_menu(m.from_user.id == ADMIN_ID))

# ================== RUN ==================
if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
