import asyncio
import os
import logging
from datetime import date

import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

TOKEN = "8821686299:AAHxFvZDbkm2mS-w05bx3gj9mFJf_vH8lxM"

DB_NAME = "qurilish.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class AvansState(StatesGroup):
    waiting_summa = State()


# --- BAZA ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS ishchilar (ism TEXT PRIMARY KEY)")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS attendance (ism TEXT, kun_sana TEXT, "
            "UNIQUE(ism, kun_sana))"
        )
        await db.execute("CREATE TABLE IF NOT EXISTS avans (ism TEXT, summa INTEGER, sana TEXT)")
        await db.commit()


# --- MENYULAR ---
def main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Ish kunini kiritish", callback_data="menu_ish")
    builder.button(text="💰 Avans yozish", callback_data="menu_avans")
    builder.button(text="📊 Hisobot", callback_data="menu_hisobot")
    builder.adjust(1)
    return builder.as_markup()


def back_button():
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Ortga", callback_data="back")
    return builder.as_markup()


async def get_workers():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT ism FROM ishchilar") as cursor:
            return await cursor.fetchall()


# --- START / ORTGA ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Boshqaruv menyusi:", reply_markup=main_menu())


@dp.callback_query(F.data == "back")
async def go_back(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Boshqaruv menyusi:", reply_markup=main_menu())
    await call.answer()


# --- ISHCHI QO'SHISH ---
@dp.message(F.text.startswith("add_ishchi "))
async def add_ishchi(message: types.Message):
    ism = message.text.split(" ", 1)[1].strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO ishchilar VALUES (?)", (ism,))
        await db.commit()
    await message.answer(f"✅ {ism.upper()} qo'shildi.")


# --- ISH KUNI ---
@dp.callback_query(F.data == "menu_ish")
async def show_workers(call: types.CallbackQuery):
    workers = await get_workers()
    if not workers:
        return await call.answer("Bazada ishchi yo'q! 'add_ishchi ism' deb yozing.", show_alert=True)

    builder = InlineKeyboardBuilder()
    for w in workers:
        builder.button(text=w[0].upper(), callback_data=f"work_{w[0]}")
    builder.button(text="⬅️ Ortga", callback_data="back")
    builder.adjust(2)
    await call.message.edit_text("Ishga chiqqan ishchini tanlang:", reply_markup=builder.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("work_"))
async def add_work(call: types.CallbackQuery):
    ism = call.data.split("_", 1)[1]  # to'liq ismni oladi, "_" borligidan qat'iy nazar
    bugun = date.today().isoformat()

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO attendance (ism, kun_sana) VALUES (?, ?)", (ism, bugun)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return await call.answer(f"{ism.upper()} uchun bugungi kun allaqachon yozilgan!", show_alert=True)

    await call.answer(f"{ism.upper()} uchun bugungi kun yozildi!", show_alert=True)


# --- AVANS ---
@dp.callback_query(F.data == "menu_avans")
async def show_workers_avans(call: types.CallbackQuery):
    workers = await get_workers()
    if not workers:
        return await call.answer("Bazada ishchi yo'q! 'add_ishchi ism' deb yozing.", show_alert=True)

    builder = InlineKeyboardBuilder()
    for w in workers:
        builder.button(text=w[0].upper(), callback_data=f"avans_{w[0]}")
    builder.button(text="⬅️ Ortga", callback_data="back")
    builder.adjust(2)
    await call.message.edit_text("Avans beriladigan ishchini tanlang:", reply_markup=builder.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("avans_"))
async def ask_avans_summa(call: types.CallbackQuery, state: FSMContext):
    ism = call.data.split("_", 1)[1]
    await state.update_data(ism=ism)
    await state.set_state(AvansState.waiting_summa)
    await call.message.edit_text(
        f"{ism.upper()} uchun avans summasini kiriting (faqat raqam, masalan: 500000):",
        reply_markup=back_button(),
    )
    await call.answer()


@dp.message(StateFilter(AvansState.waiting_summa))
async def save_avans(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Iltimos, faqat raqam kiriting (masalan: 500000).")

    data = await state.get_data()
    ism = data["ism"]
    summa = int(message.text)
    bugun = date.today().isoformat()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO avans (ism, summa, sana) VALUES (?, ?, ?)", (ism, summa, bugun))
        await db.commit()

    await state.clear()
    await message.answer(f"✅ {ism.upper()} ga {summa:,} so'm avans yozildi.".replace(",", " "),
                          reply_markup=main_menu())


# --- HISOBOT ---
@dp.callback_query(F.data == "menu_hisobot")
async def show_report(call: types.CallbackQuery):
    workers = await get_workers()
    if not workers:
        return await call.answer("Bazada ishchi yo'q!", show_alert=True)

    lines = []
    async with aiosqlite.connect(DB_NAME) as db:
        for (ism,) in workers:
            async with db.execute(
                "SELECT COUNT(*) FROM attendance WHERE ism = ?", (ism,)
            ) as cur:
                kunlar = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COALESCE(SUM(summa), 0) FROM avans WHERE ism = ?", (ism,)
            ) as cur:
                jami_avans = (await cur.fetchone())[0]
            lines.append(f"👤 {ism.upper()}: {kunlar} kun ishlagan, {jami_avans:,} so'm avans olgan".replace(",", " "))

    text = "📊 Hisobot:\n\n" + "\n".join(lines)
    await call.message.edit_text(text, reply_markup=back_button())
    await call.answer()


# --- RENDER UCHUN HTTP SERVER (port xatosini yopish) ---
async def web_handler(request):
    return web.Response(text="Bot is running!")


async def main():
    await init_db()

    app = web.Application()
    app.router.add_get("/", web_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
