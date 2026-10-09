"""Telegram-бот: студент шлёт файл -> опрос -> оплата -> админ подтверждает -> печать."""
import asyncio
import logging
import os
import sqlite3
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

import printing

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PRICE_BW = int(os.getenv("PRICE_BW", "300"))
PRICE_COLOR = int(os.getenv("PRICE_COLOR", "1000"))
CURRENCY = os.getenv("CURRENCY", "сум")
CARD_INFO = os.getenv("CARD_INFO", "Реквизиты уточните у оператора")
PRINTER = os.getenv("PRINTER_NAME", "")
SUMATRA = os.getenv("SUMATRA_PATH", "SumatraPDF.exe")
MAX_MB = 20

FILES = Path("files")
FILES.mkdir(exist_ok=True)
db = sqlite3.connect("orders.db")
db.execute("""CREATE TABLE IF NOT EXISTS orders(
    id TEXT PRIMARY KEY, user_id INT, username TEXT, file TEXT, pages INT, copies INT,
    color INT, duplex INT, price INT, method TEXT, status TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")
db.commit()

dp = Dispatcher(storage=MemoryStorage())
print_lock = asyncio.Lock()  # печатаем по одному заданию (очередь)


class Order(StatesGroup):
    copies = State()
    color = State()
    duplex = State()
    method = State()
    receipt = State()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def calc_price(pages: int, copies: int, color: bool, duplex: bool) -> int:
    return pages * copies * (PRICE_COLOR if color else PRICE_BW)


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Отправь файл (PDF, Word, PowerPoint, Excel или фото) — я напечатаю.\n"
        f"Цена: ч/б {PRICE_BW} {CURRENCY}/стр, цвет {PRICE_COLOR} {CURRENCY}/стр.\n"
        "Оплата: переводом на карту или наличными оператору.")


@dp.message(F.document)
async def got_file(m: Message, state: FSMContext, bot: Bot):
    doc = m.document
    ext = Path(doc.file_name or "").suffix.lower()
    if ext not in printing.ALLOWED_EXT:
        return await m.answer("Формат не поддерживается. Пришли PDF, DOCX, PPTX, XLSX или картинку.")
    if doc.file_size > MAX_MB * 1024 * 1024:
        return await m.answer(f"Файл больше {MAX_MB} МБ.")
    msg = await m.answer("Обрабатываю файл…")
    oid = uuid.uuid4().hex[:8]
    src = FILES / f"{oid}{ext}"
    await bot.download(doc, destination=src)
    try:
        pdf = await printing.to_pdf(src)
        pages = printing.count_pages(pdf)
    except Exception:
        logging.exception("convert failed")
        return await msg.edit_text("Не удалось обработать файл. Попробуй отправить PDF.")
    await state.update_data(oid=oid, pdf=str(pdf), pages=pages, name=doc.file_name)
    await state.set_state(Order.copies)
    await msg.edit_text(f"Файл: {doc.file_name}\nСтраниц: {pages}\n\nСколько копий?",
                        reply_markup=kb([[(str(n), f"c{n}") for n in (1, 2, 3, 5, 10)]]))


@dp.message(F.photo)
async def got_photo_hint(m: Message, state: FSMContext):
    if await state.get_state() == Order.receipt.state:
        return await got_receipt(m, state, m.bot)
    await m.answer("Отправь фото как «Файл» (скрепка → Файл), чтобы не потерять качество.")


@dp.callback_query(Order.copies, F.data.startswith("c"))
async def set_copies(cb: CallbackQuery, state: FSMContext):
    await state.update_data(copies=int(cb.data[1:]))
    await state.set_state(Order.color)
    await cb.message.edit_text("Печать:", reply_markup=kb([[("⚫ Чёрно-белая", "col0"), ("🎨 Цветная", "col1")]]))


@dp.callback_query(Order.color, F.data.startswith("col"))
async def set_color(cb: CallbackQuery, state: FSMContext):
    await state.update_data(color=cb.data == "col1")
    await state.set_state(Order.duplex)
    await cb.message.edit_text("Стороны:", reply_markup=kb([[("1 сторона", "d0"), ("2 стороны", "d1")]]))


@dp.callback_query(Order.duplex, F.data.startswith("d"))
async def set_duplex(cb: CallbackQuery, state: FSMContext):
    d = await state.update_data(duplex=cb.data == "d1")
    price = calc_price(d["pages"], d["copies"], d["color"], d["duplex"])
    await state.update_data(price=price)
    await state.set_state(Order.method)
    await cb.message.edit_text(
        f"{d['name']}\n{d['pages']} стр × {d['copies']} коп., {'цвет' if d['color'] else 'ч/б'}, "
        f"{'2 стороны' if d['duplex'] else '1 сторона'}\n\n💰 К оплате: {price} {CURRENCY}\nКак платишь?",
        reply_markup=kb([[("💳 Переводом", "m_card"), ("💵 Наличными", "m_cash")]]))


async def save_order(cb_or_m, d, method, status):
    u = cb_or_m.from_user
    db.execute("INSERT INTO orders(id,user_id,username,file,pages,copies,color,duplex,price,method,status)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (d["oid"], u.id, u.username, d["pdf"], d["pages"], d["copies"],
                int(d["color"]), int(d["duplex"]), d["price"], method, status))
    db.commit()


def admin_text(d, u, method):
    return (f"🖨 Заказ #{d['oid']} от @{u.username or u.id}\n{d['name']}\n"
            f"{d['pages']} стр × {d['copies']}, {'цвет' if d['color'] else 'ч/б'}, "
            f"{'2 стор.' if d['duplex'] else '1 стор.'}\n"
            f"💰 {d['price']} {CURRENCY} — {'перевод (чек ниже)' if method == 'card' else 'НАЛИЧНЫЕ: получи деньги и подтверди'}")


def admin_kb(oid):
    return kb([[("✅ Оплачено → печатать", f"ok_{oid}"), ("❌ Отклонить", f"no_{oid}")]])


@dp.callback_query(Order.method, F.data == "m_cash")
async def pay_cash(cb: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await save_order(cb, d, "cash", "wait_admin")
    await state.clear()
    await cb.message.edit_text(f"Заказ #{d['oid']} принят. Подойди к оператору и оплати {d['price']} {CURRENCY} — "
                               "после оплаты документ сразу напечатается.")
    for a in ADMINS:
        await bot.send_message(a, admin_text(d, cb.from_user, "cash"), reply_markup=admin_kb(d["oid"]))


@dp.callback_query(Order.method, F.data == "m_card")
async def pay_card(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.set_state(Order.receipt)
    await cb.message.edit_text(f"Переведи {d['price']} {CURRENCY}\n{CARD_INFO}\n\n"
                               "Затем отправь сюда скриншот чека.")


async def got_receipt(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await save_order(m, d, "card", "wait_admin")
    await state.clear()
    await m.answer(f"Чек получен. Заказ #{d['oid']} ждёт подтверждения оператора.")
    for a in ADMINS:
        await bot.send_photo(a, m.photo[-1].file_id, caption=admin_text(d, m.from_user, "card"),
                             reply_markup=admin_kb(d["oid"]))


@dp.callback_query(F.data.regexp(r"^(ok|no)_"))
async def admin_decision(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Нет доступа", show_alert=True)
    action, oid = cb.data.split("_", 1)
    row = db.execute("SELECT user_id,file,copies,color,duplex,status FROM orders WHERE id=?", (oid,)).fetchone()
    if not row or row[5] != "wait_admin":
        return await cb.answer("Заказ уже обработан", show_alert=True)
    uid, pdf, copies, color, duplex, _ = row
    # сначала меняем статус, чтобы повторное нажатие не напечатало дважды
    db.execute("UPDATE orders SET status=? WHERE id=?", ("printing" if action == "ok" else "rejected", oid))
    db.commit()
    mark = "✅ печатается" if action == "ok" else "❌ отклонён"
    await (cb.message.edit_caption if cb.message.photo else cb.message.edit_text)(
        (cb.message.caption or cb.message.text) + f"\n\n{mark} ({cb.from_user.first_name})")
    if action == "no":
        return await bot.send_message(uid, f"Заказ #{oid} отклонён. Свяжись с оператором.")
    try:
        async with print_lock:
            await printing.print_pdf(Path(pdf), copies, bool(color), bool(duplex), PRINTER, SUMATRA)
        db.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
        await bot.send_message(uid, f"✅ Заказ #{oid} напечатан, забирай!")
    except Exception as e:
        logging.exception("print failed")
        db.execute("UPDATE orders SET status='error' WHERE id=?", (oid,))
        await bot.send_message(cb.from_user.id, f"⚠️ Ошибка печати #{oid}: {e}")
        await bot.send_message(uid, f"Заказ #{oid}: ошибка печати, оператор разберётся.")
    db.commit()


@dp.message(Command("stats"), F.from_user.id.in_(ADMINS))
async def stats(m: Message):
    n, pages, money = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(pages*copies),0), COALESCE(SUM(price),0) FROM orders "
        "WHERE status='done' AND date(created)=date('now')").fetchone()
    await m.answer(f"Сегодня: {n} заказов, {pages} листов, {money} {CURRENCY}")


async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(Bot(BOT_TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
