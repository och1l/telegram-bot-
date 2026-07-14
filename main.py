import asyncio
import aiosqlite
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# TOKENingiz (Tokeningizni maxfiy saqlang!)
TOKEN = "8821686299:AAGoFFrp1Ys1Dl5jufkEGlsiEyazPZAv1Qg"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Baza bilan ishlash (aiosqlite)
async def init_db():
    async with aiosqlite.connect('qurilish.db') as db:
        await db.execute('CREATE TABLE IF NOT EXISTS ishchilar (ism TEXT PRIMARY KEY)')
        await db.execute('CREATE TABLE IF NOT EXISTS attendance (ism TEXT, kun INTEGER)')
        await db.execute('CREATE TABLE IF NOT EXISTS avans (ism TEXT, summa INTEGER)')
        await db.commit()

# --- HANDLERLAR ---
def main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Ish kunini kiritish", callback_data="menu_ish")
    builder.button(text="💰 Avans yozish", callback_data="menu_avans")
    builder.adjust(1)
    return builder.as_markup()

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Boshqaruv menyusi:", reply_markup=main_menu())

@dp.callback_query(F.data == "menu_ish")
async def show_workers(call: types.CallbackQuery):
    async with aiosqlite.connect('qurilish.db') as db:
        async with db.execute("SELECT ism FROM ishchilar") as cursor:
            workers = await cursor.fetchall()
    
    if not workers:
        return await call.answer("Bazada ishchi yo'q! 'add_ishchi ism' yozing.")
    
    builder = InlineKeyboardBuilder()
    for w in workers:
        builder.button(text=w[0].upper(), callback_data=f"work_{w[0]}")
    builder.button(text="⬅️ Ortga", callback_data="back")
    builder.adjust(2)
    await call.message.edit_text("Ishga chiqqan ishchini tanlang:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("work_"))
async def add_work(call: types.CallbackQuery):
    ism = call.data.split("_")[1]
    async with aiosqlite.connect('qurilish.db') as db:
        await db.execute("INSERT INTO attendance VALUES (?, 1)", (ism,))
        await db.commit()
    await call.answer(f"{ism.upper()} uchun 1 kun yozildi!", show_alert=True)

@dp.message(F.text.startswith("add_ishchi "))
async def add_ishchi(message: types.Message):
    ism = message.text.split(" ", 1)[1].lower()
    async with aiosqlite.connect('qurilish.db') as db:
        await db.execute("INSERT OR IGNORE INTO ishchilar VALUES (?)", (ism,))
        await db.commit()
    await message.answer(f"✅ {ism.upper()} qo'shildi.")

# --- RENDER UCHUN PORT XATOSINI YOPISH ---
async def web_handler(request):
    return web.Response(text="Bot is running!")

async def main():
    await init_db()
    
    # Render uchun HTTP serverni fon rejimida ishga tushirish
    app = web.Application()
    app.router.add_get('/', web_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
