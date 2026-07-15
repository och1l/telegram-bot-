import asyncio
import os
import logging
from datetime import date, datetime
import json

import aiosqlite
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# --- KONFIGURATSIYA ---
TOKEN = "8821686299:AAHGqLVCYC2nwKHZkKXKdgrM6slhN-Jbrko"
# AI kaliti Render'dagi Environment Variable'dan olinadi
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPER_ADMIN_PASSWORD = "w1234w4321"
DB_NAME = "qurilish.db"

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

STATUS_LABELS = {
    "full": "✅ To'liq kun",
    "half_before": "🌗 Tushgacha (0.5)",
    "half_after": "🌓 Tushdan keyin (0.5)",
}
STATUS_VALUE = {"full": 1.0, "half_before": 0.5, "half_after": 0.5}

class Form(StatesGroup):
    obj_name = State()
    obj_owner_username = State()
    add_manager_username = State()
    add_cook_username = State()
    merge_cook_username = State()
    payment_amount = State()
    emp_username = State()
    emp_fullname = State()

# ---------------- BAZA ----------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS objects (id INTEGER PRIMARY KEY AUTOINCREMENT, nomi TEXT, owner_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS managers (telegram_id INTEGER PRIMARY KEY, object_id INTEGER, full_name TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS cook_groups (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        await db.execute("CREATE TABLE IF NOT EXISTS cooks (telegram_id INTEGER PRIMARY KEY, group_id INTEGER, full_name TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS object_cook_group (object_id INTEGER PRIMARY KEY, group_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS workers (id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER, ism TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS attendance (worker_id INTEGER, sana TEXT, status TEXT, UNIQUE(worker_id, sana))")
        await db.execute("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER, summa INTEGER, sana_vaqt TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS pending_roles (username TEXT, role TEXT, object_id INTEGER, full_name TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS super_admins (telegram_id INTEGER PRIMARY KEY)")
        await db.commit()

async def remember_user(telegram_id: int, username: str | None):
    if not username: return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO users (telegram_id, username) VALUES (?, ?)", (telegram_id, norm_username(username)))
        await db.commit()

def norm_username(u: str) -> str:
    return u.strip().lstrip("@").lower()

# ---------------- ROL ANIQLASH ----------------
async def resolve_pending(db, telegram_id: int, username: str | None):
    if not username: return
    uname = norm_username(username)
    async with db.execute("SELECT rowid, role, object_id, full_name FROM pending_roles WHERE username = ?", (uname,)) as cur:
        rows = await cur.fetchall()
    for rowid, role, object_id, full_name in rows:
        if role == "admin":
            await db.execute("UPDATE objects SET owner_id = ? WHERE id = ?", (telegram_id, object_id))
        elif role == "manager":
            await db.execute("INSERT OR REPLACE INTO managers (telegram_id, object_id, full_name) VALUES (?, ?, ?)", (telegram_id, object_id, full_name))
        elif role == "cook":
            async with db.execute("SELECT group_id FROM cooks WHERE telegram_id = ?", (telegram_id,)) as c2:
                existing = await c2.fetchone()
            async with db.execute("SELECT group_id FROM object_cook_group WHERE object_id = ?", (object_id,)) as c3:
                object_group = await c3.fetchone()
            if object_group: group_id = object_group[0]
            elif existing: group_id = existing[0]
            else:
                cur2 = await db.execute("INSERT INTO cook_groups DEFAULT VALUES")
                group_id = cur2.lastrowid
            await db.execute("INSERT OR REPLACE INTO cooks (telegram_id, group_id, full_name) VALUES (?, ?, ?)", (telegram_id, group_id, full_name))
            await db.execute("INSERT OR REPLACE INTO object_cook_group (object_id, group_id) VALUES (?, ?)", (object_id, group_id))
        await db.execute("DELETE FROM pending_roles WHERE rowid = ?", (rowid,))
    await db.commit()

async def get_role_and_objects(telegram_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM super_admins WHERE telegram_id = ?", (telegram_id,)) as cur:
            if await cur.fetchone(): return "super", []
        async with db.execute("SELECT id FROM objects WHERE owner_id = ?", (telegram_id,)) as cur:
            owned = [r[0] for r in await cur.fetchall()]
        if owned: return "admin", owned
        async with db.execute("SELECT object_id FROM managers WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
        if row: return "manager", [row[0]]
        async with db.execute("SELECT group_id FROM cooks WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
        if row: return "cook", [row[0]]
        return None, []

# ---------------- MENYULAR ----------------
def super_menu():
    b = InlineKeyboardBuilder()
    b.button(text="🏗 Obyekt qo'shish", callback_data="sadmin_add_object")
    b.button(text="📋 Obyektlar ro'yxati", callback_data="sadmin_list_objects")
    b.button(text="🔗 Obyektlarni birlashtirish", callback_data="sadmin_merge_pick")
    b.adjust(1)
    return b.as_markup()

def admin_menu(object_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="➕ Xodim qo'shish", callback_data=f"emp_add_{object_id}")
    b.button(text="📋 Xodimlar ro'yxati", callback_data=f"emp_list_{object_id}")
    b.button(text="📊 Hisobot", callback_data=f"admin_report_{object_id}")
    b.adjust(1)
    return b.as_markup()

def manager_menu():
    b = InlineKeyboardBuilder()
    b.button(text="📅 Bugungi davomat", callback_data="mgr_attendance")
    b.button(text="💰 To'lov yozish", callback_data="mgr_payment")
    b.button(text="📊 Hisobot", callback_data="mgr_report")
    b.adjust(1)
    return b.as_markup()

def back_kb(callback: str):
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Ortga", callback_data=callback)
    return b.as_markup()

@dp.message.middleware()
async def remember_msg_user(handler, event: types.Message, data):
    await remember_user(event.from_user.id, event.from_user.username)
    return await handler(event, data)

@dp.callback_query.middleware()
async def remember_cb_user(handler, event: types.CallbackQuery, data):
    await remember_user(event.from_user.id, event.from_user.username)
    return await handler(event, data)

# ---------------- COMMANDS ----------------
@dp.message(F.text == SUPER_ADMIN_PASSWORD)
async def claim_super_admin(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO super_admins (telegram_id) VALUES (?)", (message.from_user.id,))
        await db.commit()
    try: await message.delete()
    except: pass
    await message.answer("✅ Siz bosh admin sifatida tasdiqlandingiz.", reply_markup=super_menu())

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_NAME) as db:
        await resolve_pending(db, message.from_user.id, message.from_user.username)
    role, ids = await get_role_and_objects(message.from_user.id)
    if role == "super": await message.answer("👑 Bosh admin paneli:", reply_markup=super_menu())
    elif role == "admin":
        if len(ids) == 1: await message.answer("🏗 Obyekt egasi paneli:", reply_markup=admin_menu(ids[0]))
        else:
            b = InlineKeyboardBuilder()
            async with aiosqlite.connect(DB_NAME) as db:
                for oid in ids:
                    async with db.execute("SELECT nomi FROM objects WHERE id = ?", (oid,)) as cur:
                        nomi = (await cur.fetchone())[0]
                    b.button(text=nomi, callback_data=f"admin_select_{oid}")
            b.adjust(1)
            await message.answer("Qaysi obyekt?", reply_markup=b.as_markup())
    elif role == "manager": await message.answer("👷 Ish boshqaruvchi paneli:", reply_markup=manager_menu())
    elif role == "cook": await show_cook_report(message.from_user.id, message)
    else: await message.answer("🔑 Parolni kiriting:")

@dp.callback_query(F.data.startswith("admin_select_"))
async def admin_select(call: types.CallbackQuery):
    object_id = int(call.data.split("_")[-1])
    await call.message.edit_text("🏗 Obyekt egasi paneli:", reply_markup=admin_menu(object_id))
    await call.answer()

# --- SADMIN, ADMIN, MANAGER QISMLARI (Oldingi kodingizdagi barcha funksiyalar shu yerda bo'lishi kerak) ---
# (Sizning asl kodingizdagi barcha @dp.callback_query va @dp.message qismlarini bu yerga qo'ying)

# ---------------- AI YORDAMCHI ----------------
async def ai_process_data(text: str):
    if not GEMINI_API_KEY: return {"error": True}
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"Sen qurilish botining yordamchisisan. '{text}' matnidan ishchi ismi va statusini (full/half_before) aniqla. JSON qaytar: {{\"ism\": \"...\", \"status\": \"...\"}}"
    try:
        response = await model.generate_content_async(prompt)
        clean = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except: return {"error": True}

@dp.message(F.text.startswith("AI "))
async def ai_handler(message: types.Message):
    role, ids = await get_role_and_objects(message.from_user.id)
    if role != "manager": return
    content = message.text.replace("AI ", "")
    await message.answer("⏳ Tahlil qilinmoqda...")
    data = await ai_process_data(content)
    if "error" in data: return await message.answer("❌ Xatolik. 'AI Ali +' formatida yozing.")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM workers WHERE ism = ? AND object_id = ?", (data['ism'].lower(), ids[0])) as cur:
            w = await cur.fetchone()
            if w:
                await db.execute("INSERT OR REPLACE INTO attendance (worker_id, sana, status) VALUES (?, ?, ?)", (w[0], date.today().isoformat(), data['status']))
                await db.commit()
                await message.answer(f"✅ {data['ism'].upper()} uchun {STATUS_LABELS.get(data['status'], data['status'])} yozildi.")
            else: await message.answer(f"❌ '{data['ism']}' bazada topilmadi.")

# ---------------- SERVER ----------------
async def web_handler(request): return web.Response(text="Bot is running!")

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
