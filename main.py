import asyncio
import os
import logging
from datetime import date, datetime

import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ⚠️ Bu yerga @BotFather'dan olgan tokeningizni qo'ying
TOKEN = "8821686299:AAHGqLVCYC2nwKHZkKXKdgrM6slhN-Jbrko"

# Ushbu parolni botga yozgan birinchi kishi (yoki har kim yozsa) avtomatik bosh admin bo'ladi.
# Parolni ISHONCHLI SAQLANG — uni bilgan har kim botni to'liq boshqarib oladi.
SUPER_ADMIN_PASSWORD = "w1234w4321"

DB_NAME = "qurilish.db"

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


# ---------------- BAZA ----------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS objects ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, nomi TEXT, owner_id INTEGER)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS managers ("
            "telegram_id INTEGER PRIMARY KEY, object_id INTEGER)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS cook_groups (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS cooks ("
            "telegram_id INTEGER PRIMARY KEY, group_id INTEGER)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS object_cook_group ("
            "object_id INTEGER PRIMARY KEY, group_id INTEGER)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS workers ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER, ism TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS attendance ("
            "worker_id INTEGER, sana TEXT, status TEXT, UNIQUE(worker_id, sana))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS payments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER, "
            "summa INTEGER, sana_vaqt TEXT)"
        )
        # username orqali hali telegram_id'si noma'lum odamlarni "kutish" jadvali
        await db.execute(
            "CREATE TABLE IF NOT EXISTS pending_roles ("
            "username TEXT, role TEXT, object_id INTEGER)"
        )
        # har bir botga murojaat qilgan odamning username'ini eslab qolish uchun
        await db.execute(
            "CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT)"
        )
        # parol orqali bosh admin bo'lganlar
        await db.execute(
            "CREATE TABLE IF NOT EXISTS super_admins (telegram_id INTEGER PRIMARY KEY)"
        )
        await db.commit()


async def remember_user(telegram_id: int, username: str | None):
    if not username:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, norm_username(username)),
        )
        await db.commit()


def norm_username(u: str) -> str:
    return u.strip().lstrip("@").lower()


# ---------------- ROL ANIQLASH ----------------
async def resolve_pending(db, telegram_id: int, username: str | None):
    """Agar shu username uchun kutilayotgan rol bo'lsa, uni haqiqiy jadvalga o'tkazadi."""
    if not username:
        return
    uname = norm_username(username)
    async with db.execute(
        "SELECT rowid, role, object_id FROM pending_roles WHERE username = ?", (uname,)
    ) as cur:
        rows = await cur.fetchall()

    for rowid, role, object_id in rows:
        if role == "admin":
            await db.execute("UPDATE objects SET owner_id = ? WHERE id = ?", (telegram_id, object_id))
        elif role == "manager":
            await db.execute(
                "INSERT OR REPLACE INTO managers (telegram_id, object_id) VALUES (?, ?)",
                (telegram_id, object_id),
            )
        elif role == "cook":
            # Agar bu odam allaqachon boshqa joyda oshpaz bo'lsa, o'sha guruhini ishlatamiz.
            async with db.execute(
                "SELECT group_id FROM cooks WHERE telegram_id = ?", (telegram_id,)
            ) as c2:
                existing = await c2.fetchone()

            # Agar bu OBYEKT allaqachon biror guruhga bog'langan bo'lsa (masalan boshqa
            # oshpaz orqali), yangi oshpazni ham O'SHA guruhga qo'shamiz — shunda bitta
            # obyektga bir nechta oshpaz (4 tagacha va undan ko'p ham) biriktirilishi mumkin.
            async with db.execute(
                "SELECT group_id FROM object_cook_group WHERE object_id = ?", (object_id,)
            ) as c3:
                object_group = await c3.fetchone()

            if object_group:
                group_id = object_group[0]
            elif existing:
                group_id = existing[0]
            else:
                cur2 = await db.execute("INSERT INTO cook_groups DEFAULT VALUES")
                group_id = cur2.lastrowid

            await db.execute(
                "INSERT OR REPLACE INTO cooks (telegram_id, group_id) VALUES (?, ?)",
                (telegram_id, group_id),
            )
            await db.execute(
                "INSERT OR REPLACE INTO object_cook_group (object_id, group_id) VALUES (?, ?)",
                (object_id, group_id),
            )
        await db.execute("DELETE FROM pending_roles WHERE rowid = ?", (rowid,))
    await db.commit()


async def get_role_and_objects(telegram_id: int):
    """Qaytaradi: (rol, [object_id ...]) . rol: 'super', 'admin', 'manager', 'cook', None"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT 1 FROM super_admins WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            if await cur.fetchone():
                return "super", []

        async with db.execute("SELECT id FROM objects WHERE owner_id = ?", (telegram_id,)) as cur:
            owned = [r[0] for r in await cur.fetchall()]
        if owned:
            return "admin", owned

        async with db.execute(
            "SELECT object_id FROM managers WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            return "manager", [row[0]]

        async with db.execute(
            "SELECT group_id FROM cooks WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            return "cook", [row[0]]  # bu yerda group_id qaytadi

        return None, []


# ---------------- MENYULAR ----------------
def super_menu():
    b = InlineKeyboardBuilder()
    b.button(text="🏗 Obyekt qo'shish", callback_data="sadmin_add_object")
    b.button(text="📋 Obyektlar ro'yxati", callback_data="sadmin_list_objects")
    b.adjust(1)
    return b.as_markup()


def admin_menu(object_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="👷 Ish boshqaruvchi tayinlash", callback_data=f"admin_addmgr_{object_id}")
    b.button(text="👨‍🍳 Oshpaz tayinlash", callback_data=f"admin_addcook_{object_id}")
    b.button(text="🔗 Oshpazlarni birlashtirish", callback_data=f"admin_merge_{object_id}")
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


# ---------------- PAROL ORQALI BOSH ADMIN BO'LISH ----------------
@dp.message(F.text == SUPER_ADMIN_PASSWORD)
async def claim_super_admin(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO super_admins (telegram_id) VALUES (?)", (message.from_user.id,)
        )
        await db.commit()
    # Xavfsizlik uchun: parolni yozgan xabarni chatdan o'chirishga harakat qilamiz
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("✅ Siz bosh admin sifatida tasdiqlandingiz.", reply_markup=super_menu())


# ---------------- /start ----------------
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_NAME) as db:
        await resolve_pending(db, message.from_user.id, message.from_user.username)

    role, ids = await get_role_and_objects(message.from_user.id)

    if role == "super":
        await message.answer("👑 Bosh admin paneli:", reply_markup=super_menu())
    elif role == "admin":
        if len(ids) == 1:
            await message.answer("🏗 Obyekt egasi paneli:", reply_markup=admin_menu(ids[0]))
        else:
            b = InlineKeyboardBuilder()
            async with aiosqlite.connect(DB_NAME) as db:
                for oid in ids:
                    async with db.execute("SELECT nomi FROM objects WHERE id = ?", (oid,)) as cur:
                        nomi = (await cur.fetchone())[0]
                    b.button(text=nomi, callback_data=f"admin_select_{oid}")
            b.adjust(1)
            await message.answer("Qaysi obyekt?", reply_markup=b.as_markup())
    elif role == "manager":
        await message.answer("👷 Ish boshqaruvchi paneli:", reply_markup=manager_menu())
    elif role == "cook":
        await show_cook_report(message.from_user.id, message)
    else:
        await message.answer(
            "Sizga hali hech qanday rol biriktirilmagan. Obyekt egasi yoki bosh admin sizni "
            "tizimga qo'shishi kerak."
        )


@dp.callback_query(F.data.startswith("admin_select_"))
async def admin_select(call: types.CallbackQuery):
    object_id = int(call.data.split("_")[-1])
    await call.message.edit_text("🏗 Obyekt egasi paneli:", reply_markup=admin_menu(object_id))
    await call.answer()


# ---------------- SUPER ADMIN: OBYEKT QO'SHISH ----------------
@dp.callback_query(F.data == "sadmin_add_object")
async def sadmin_add_object(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(Form.obj_name)
    await call.message.edit_text("Yangi obyekt nomini kiriting:")
    await call.answer()


@dp.message(StateFilter(Form.obj_name))
async def obj_name_entered(message: types.Message, state: FSMContext):
    await state.update_data(obj_name=message.text.strip())
    await state.set_state(Form.obj_owner_username)
    await message.answer("Obyekt egasining Telegram username'ini kiriting (masalan: @alivaliyev):")


@dp.message(StateFilter(Form.obj_owner_username))
async def obj_owner_entered(message: types.Message, state: FSMContext):
    data = await state.get_data()
    username = norm_username(message.text)

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO objects (nomi, owner_id) VALUES (?, NULL)", (data["obj_name"],)
        )
        object_id = cur.lastrowid
        await db.execute(
            "INSERT INTO pending_roles (username, role, object_id) VALUES (?, 'admin', ?)",
            (username, object_id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"✅ '{data['obj_name']}' obyekti yaratildi.\n"
        f"@{username} birinchi marta botga /start bosganida, u avtomatik shu obyekt egasi bo'ladi.",
        reply_markup=super_menu(),
    )


@dp.callback_query(F.data == "sadmin_list_objects")
async def sadmin_list_objects(call: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT nomi, owner_id FROM objects") as cur:
            rows = await cur.fetchall()
    if not rows:
        text = "Hali obyektlar yo'q."
    else:
        lines = [f"🏗 {nomi} — {'egasi hali /start bosmagan' if not owner else 'faol'}" for nomi, owner in rows]
        text = "\n".join(lines)
    await call.message.edit_text(text, reply_markup=back_kb("sadmin_back"))
    await call.answer()


@dp.callback_query(F.data == "sadmin_back")
async def sadmin_back(call: types.CallbackQuery):
    await call.message.edit_text("👑 Bosh admin paneli:", reply_markup=super_menu())
    await call.answer()


# ---------------- ADMIN: ISH BOSHQARUVCHI TAYINLASH ----------------
@dp.callback_query(F.data.startswith("admin_addmgr_"))
async def admin_addmgr(call: types.CallbackQuery, state: FSMContext):
    object_id = int(call.data.split("_")[-1])
    await state.update_data(object_id=object_id)
    await state.set_state(Form.add_manager_username)
    await call.message.edit_text("Ish boshqaruvchining Telegram username'ini kiriting:")
    await call.answer()


@dp.message(StateFilter(Form.add_manager_username))
async def add_manager_entered(message: types.Message, state: FSMContext):
    data = await state.get_data()
    username = norm_username(message.text)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO pending_roles (username, role, object_id) VALUES (?, 'manager', ?)",
            (username, data["object_id"]),
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"✅ @{username} ish boshqaruvchi sifatida belgilandi. U /start bosganida panel ochiladi.",
        reply_markup=admin_menu(data["object_id"]),
    )


# ---------------- ADMIN: OSHPAZ TAYINLASH ----------------
@dp.callback_query(F.data.startswith("admin_addcook_"))
async def admin_addcook(call: types.CallbackQuery, state: FSMContext):
    object_id = int(call.data.split("_")[-1])
    await state.update_data(object_id=object_id)
    await state.set_state(Form.add_cook_username)
    await call.message.edit_text("Oshpazning Telegram username'ini kiriting:")
    await call.answer()


@dp.message(StateFilter(Form.add_cook_username))
async def add_cook_entered(message: types.Message, state: FSMContext):
    data = await state.get_data()
    username = norm_username(message.text)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO pending_roles (username, role, object_id) VALUES (?, 'cook', ?)",
            (username, data["object_id"]),
        )
        await db.commit()
    await state.clear()
    await message.answer(
        f"✅ @{username} oshpaz sifatida belgilandi. U /start bosganida panel ochiladi.",
        reply_markup=admin_menu(data["object_id"]),
    )


# ---------------- ADMIN: OSHPAZLARNI BIRLASHTIRISH ----------------
@dp.callback_query(F.data.startswith("admin_merge_"))
async def admin_merge(call: types.CallbackQuery, state: FSMContext):
    object_id = int(call.data.split("_")[-1])
    await state.update_data(object_id=object_id)
    await state.set_state(Form.merge_cook_username)
    await call.message.edit_text(
        "Ushbu obyektning oshpazini QAYSI BOSHQA obyektdagi oshpaz bilan bitta guruh qilib "
        "birlashtirmoqchisiz? O'sha oshpazning Telegram username'ini kiriting "
        "(u allaqachon botdan /start bosgan bo'lishi kerak):"
    )
    await call.answer()


@dp.message(StateFilter(Form.merge_cook_username))
async def merge_cook_entered(message: types.Message, state: FSMContext):
    data = await state.get_data()
    object_id = data["object_id"]
    username = norm_username(message.text)

    async with aiosqlite.connect(DB_NAME) as db:
        # 1) o'sha username qaysi telegram_id ekanini topamiz
        async with db.execute(
            "SELECT telegram_id FROM users WHERE username = ?", (username,)
        ) as cur:
            target_user = await cur.fetchone()

        if not target_user:
            await state.clear()
            return await message.answer(
                f"❌ @{username} hali botga umuman murojaat qilmagan. "
                f"Avval o'sha odamga botga /start bosishini so'rang, so'ng qayta urinib ko'ring.",
                reply_markup=admin_menu(object_id),
            )
        target_id = target_user[0]

        # 2) o'sha odam allaqachon biror obyektda oshpaz sifatida ro'yxatdan o'tganmi
        async with db.execute(
            "SELECT group_id FROM cooks WHERE telegram_id = ?", (target_id,)
        ) as cur:
            target_cook = await cur.fetchone()

        if not target_cook:
            await state.clear()
            return await message.answer(
                f"❌ @{username} hali hech qanday obyektda oshpaz sifatida tayinlanmagan. "
                f"Avval uni biror obyektga oshpaz qilib tayinlang, so'ng birlashtiring.",
                reply_markup=admin_menu(object_id),
            )
        target_group = target_cook[0]

        # 3) joriy obyektning hozirgi guruhini topamiz
        async with db.execute(
            "SELECT group_id FROM object_cook_group WHERE object_id = ?", (object_id,)
        ) as cur:
            current = await cur.fetchone()

        if current and current[0] == target_group:
            await state.clear()
            return await message.answer(
                "Bu ikkalasi allaqachon bitta guruhda.", reply_markup=admin_menu(object_id)
            )

        if current:
            old_group = current[0]
            await db.execute(
                "UPDATE object_cook_group SET group_id = ? WHERE group_id = ?",
                (target_group, old_group),
            )
            await db.execute(
                "UPDATE cooks SET group_id = ? WHERE group_id = ?", (target_group, old_group)
            )
        else:
            await db.execute(
                "INSERT OR REPLACE INTO object_cook_group (object_id, group_id) VALUES (?, ?)",
                (object_id, target_group),
            )

        await db.commit()

    await state.clear()
    await message.answer(
        f"✅ Birlashtirildi! Endi bu obyekt va @{username} bir xil oshpaz guruhida — "
        f"barchasi umumiy ishchilar sonini ko'radi.",
        reply_markup=admin_menu(object_id),
    )


# ---------------- ADMIN: HISOBOT ----------------
@dp.callback_query(F.data.startswith("admin_report_"))
async def admin_report(call: types.CallbackQuery):
    object_id = int(call.data.split("_")[-1])
    bugun = date.today().isoformat()

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM attendance a JOIN workers w ON a.worker_id = w.id "
            "WHERE w.object_id = ? AND a.sana = ?",
            (object_id, bugun),
        ) as cur:
            bugungi_soni = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COALESCE(SUM(p.summa), 0) FROM payments p JOIN workers w ON p.worker_id = w.id "
            "WHERE w.object_id = ?",
            (object_id,),
        ) as cur:
            jami_summa = (await cur.fetchone())[0]

    text = (
        f"📊 Obyekt hisoboti:\n\n"
        f"👷 Bugun ishga chiqqanlar: {bugungi_soni} kishi\n"
        f"💰 Jami berilgan pul: {jami_summa:,} so'm".replace(",", " ")
    )
    await call.message.edit_text(text, reply_markup=admin_menu(object_id))
    await call.answer()


# ---------------- MANAGER: WORKER QO'SHISH ----------------
@dp.message(F.text.startswith("add_ishchi "))
async def add_ishchi(message: types.Message):
    role, ids = await get_role_and_objects(message.from_user.id)
    if role != "manager":
        return
    object_id = ids[0]
    ism = message.text.split(" ", 1)[1].strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO workers (object_id, ism) VALUES (?, ?)", (object_id, ism))
        await db.commit()
    await message.answer(f"✅ {ism.upper()} ishchi sifatida qo'shildi.")


# ---------------- MANAGER: DAVOMAT ----------------
@dp.callback_query(F.data == "mgr_attendance")
async def mgr_attendance(call: types.CallbackQuery):
    role, ids = await get_role_and_objects(call.from_user.id)
    if role != "manager":
        return await call.answer("Ruxsat yo'q.", show_alert=True)
    object_id = ids[0]

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, ism FROM workers WHERE object_id = ?", (object_id,)) as cur:
            workers = await cur.fetchall()

    if not workers:
        return await call.answer("Ishchi yo'q. 'add_ishchi ism' deb yozing.", show_alert=True)

    b = InlineKeyboardBuilder()
    for wid, ism in workers:
        b.button(text=ism.upper(), callback_data=f"att_pick_{wid}")
    b.button(text="⬅️ Ortga", callback_data="mgr_back")
    b.adjust(2)
    await call.message.edit_text("Ishchini tanlang:", reply_markup=b.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("att_pick_"))
async def att_pick(call: types.CallbackQuery):
    worker_id = int(call.data.split("_")[-1])
    b = InlineKeyboardBuilder()
    for status, label in STATUS_LABELS.items():
        b.button(text=label, callback_data=f"att_set_{worker_id}_{status}")
    b.button(text="⬅️ Ortga", callback_data="mgr_attendance")
    b.adjust(1)
    await call.message.edit_text("Bugungi holatni tanlang:", reply_markup=b.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("att_set_"))
async def att_set(call: types.CallbackQuery):
    _, _, worker_id, status = call.data.split("_")
    worker_id = int(worker_id)
    bugun = date.today().isoformat()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO attendance (worker_id, sana, status) VALUES (?, ?, ?) "
            "ON CONFLICT(worker_id, sana) DO UPDATE SET status = excluded.status",
            (worker_id, bugun, status),
        )
        await db.commit()

    await call.answer(f"Saqlandi: {STATUS_LABELS[status]}", show_alert=True)
    await mgr_attendance(call)


@dp.callback_query(F.data == "mgr_back")
async def mgr_back(call: types.CallbackQuery):
    await call.message.edit_text("👷 Ish boshqaruvchi paneli:", reply_markup=manager_menu())
    await call.answer()


# ---------------- MANAGER: TO'LOV ----------------
@dp.callback_query(F.data == "mgr_payment")
async def mgr_payment(call: types.CallbackQuery):
    role, ids = await get_role_and_objects(call.from_user.id)
    if role != "manager":
        return await call.answer("Ruxsat yo'q.", show_alert=True)
    object_id = ids[0]

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, ism FROM workers WHERE object_id = ?", (object_id,)) as cur:
            workers = await cur.fetchall()

    if not workers:
        return await call.answer("Ishchi yo'q.", show_alert=True)

    b = InlineKeyboardBuilder()
    for wid, ism in workers:
        b.button(text=ism.upper(), callback_data=f"pay_pick_{wid}")
    b.button(text="⬅️ Ortga", callback_data="mgr_back")
    b.adjust(2)
    await call.message.edit_text("Kimga to'lov yozasiz?", reply_markup=b.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("pay_pick_"))
async def pay_pick(call: types.CallbackQuery, state: FSMContext):
    worker_id = int(call.data.split("_")[-1])
    await state.update_data(worker_id=worker_id)
    await state.set_state(Form.payment_amount)
    await call.message.edit_text("Summani kiriting (faqat raqam, masalan: 300000):")
    await call.answer()


@dp.message(StateFilter(Form.payment_amount))
async def payment_amount_entered(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Faqat raqam kiriting.")

    data = await state.get_data()
    summa = int(message.text)
    vaqt = datetime.now().strftime("%Y-%m-%d %H:%M")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO payments (worker_id, summa, sana_vaqt) VALUES (?, ?, ?)",
            (data["worker_id"], summa, vaqt),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"✅ {summa:,} so'm yozildi ({vaqt}).".replace(",", " "), reply_markup=manager_menu()
    )


# ---------------- MANAGER: HISOBOT ----------------
@dp.callback_query(F.data == "mgr_report")
async def mgr_report(call: types.CallbackQuery):
    role, ids = await get_role_and_objects(call.from_user.id)
    if role != "manager":
        return await call.answer("Ruxsat yo'q.", show_alert=True)
    object_id = ids[0]

    lines = []
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, ism FROM workers WHERE object_id = ?", (object_id,)) as cur:
            workers = await cur.fetchall()

        for wid, ism in workers:
            async with db.execute(
                "SELECT status FROM attendance WHERE worker_id = ?", (wid,)
            ) as cur:
                statuses = await cur.fetchall()
            kunlar = sum(STATUS_VALUE.get(s[0], 0) for s in statuses)

            async with db.execute(
                "SELECT COALESCE(SUM(summa), 0) FROM payments WHERE worker_id = ?", (wid,)
            ) as cur:
                jami = (await cur.fetchone())[0]

            lines.append(f"👤 {ism.upper()}: {kunlar} kun ishlagan, {jami:,} so'm olgan".replace(",", " "))

    text = "📊 Hisobot:\n\n" + ("\n".join(lines) if lines else "Ishchi yo'q.")
    await call.message.edit_text(text, reply_markup=manager_menu())
    await call.answer()


# ---------------- COOK: HISOBOT ----------------
async def show_cook_report(telegram_id: int, message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT group_id FROM cooks WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return await message.answer("Sizga oshpaz roli biriktirilmagan.")
        group_id = row[0]

        async with db.execute(
            "SELECT object_id FROM object_cook_group WHERE group_id = ?", (group_id,)
        ) as cur:
            object_ids = [r[0] for r in await cur.fetchall()]

        if not object_ids:
            return await message.answer("Sizga hali obyekt biriktirilmagan.")

        bugun = date.today().isoformat()
        placeholders = ",".join("?" * len(object_ids))
        query = (
            f"SELECT a.status FROM attendance a JOIN workers w ON a.worker_id = w.id "
            f"WHERE w.object_id IN ({placeholders}) AND a.sana = ?"
        )
        async with db.execute(query, (*object_ids, bugun)) as cur:
            statuses = await cur.fetchall()

        jami = sum(STATUS_VALUE.get(s[0], 0) for s in statuses)

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Yangilash", callback_data="cook_refresh")
    await message.answer(
        f"👨‍🍳 Bugungi umumiy ishchilar soni (barcha bog'langan obyektlar):\n\n"
        f"🍲 {jami} kishi uchun ovqat tayyorlang",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data == "cook_refresh")
async def cook_refresh(call: types.CallbackQuery):
    await show_cook_report(call.from_user.id, call.message)
    await call.answer()


# ---------------- RENDER UCHUN HTTP SERVER ----------------
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
