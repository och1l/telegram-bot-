import sqlite3
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

conn = sqlite3.connect('qurilish.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS ishchilar (ism TEXT PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS attendance (ism TEXT, kun INTEGER)')
cursor.execute('CREATE TABLE IF NOT EXISTS avans (ism TEXT, summa INTEGER)')
conn.commit()

bot = Bot(token='8821686299:AAGoFFrp1Ys1Dl5jufkEGlsiEyazPZAv1Qg')
dp = Dispatcher()

# Ishchilar ro'yxatini bazadan olish
def get_workers():
    cursor.execute("SELECT ism FROM ishchilar")
    return [row[0] for row in cursor.fetchall()]

# Asosiy menyu
def main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Ish kunini kiritish", callback_data="menu_ish")
    builder.button(text="💰 Avans yozish", callback_data="menu_avans")
    builder.adjust(1)
    return builder.as_markup()

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Boshqaruv menyusi:", reply_markup=main_menu())

# Ishchilarni ko'rsatish
@dp.callback_query(F.data == "menu_ish")
async def show_workers(call: types.CallbackQuery):
    workers = get_workers()
    if not workers:
        return await call.answer("Bazada ishchi yo'q! Avval add_ishchi qiling.")
    
    builder = InlineKeyboardBuilder()
    for w in workers:
        builder.button(text=w.upper(), callback_data=f"work_{w}")
    builder.button(text="⬅️ Ortga", callback_data="back")
    builder.adjust(2)
    await call.message.edit_text("Ishga chiqqan ishchini tanlang:", reply_markup=builder.as_markup())

# Ishni yozish
@dp.callback_query(F.data.startswith("work_"))
async def add_work(call: types.CallbackQuery):
    ism = call.data.split("_")[1]
    cursor.execute("INSERT INTO attendance VALUES (?, 1)", (ism,))
    conn.commit()
    await call.answer(f"{ism.upper()} uchun 1 kun yozildi!", show_alert=True)

# Ortga qaytish
@dp.callback_query(F.data == "back")
async def back(call: types.CallbackQuery):
    await call.message.edit_text("Boshqaruv menyusi:", reply_markup=main_menu())

# Ishchi qo'shish buyrug'i (yozib qo'shish uchun)
@dp.message(F.text.startswith("add_ishchi "))
async def add_ishchi(message: types.Message):
    ism = message.text.split(" ", 1)[1].lower()
    cursor.execute("INSERT OR IGNORE INTO ishchilar VALUES (?)", (ism,))
    conn.commit()
    await message.answer(f"✅ {ism.upper()} qo'shildi.")

async def main(): await dp.start_polling(bot)
if __name__ == '__main__': asyncio.run(main())
