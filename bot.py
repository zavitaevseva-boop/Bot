"""
Telegram-бот магазин цифровых товаров.
Оплата: Telegram Stars (основная) + опционально ручной перевод (карта/СБП) с подтверждением админом.
Стек: Python 3.10+, aiogram 3, SQLite.
"""
import asyncio
import html
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    LabeledPrice, Message, PreCheckoutQuery, ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

import db

# ================== НАСТРОЙКИ ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SHOP_NAME = os.getenv("SHOP_NAME", "Мой магазин")
SUPPORT = os.getenv("SUPPORT", "@your_username")
MANUAL_PAYMENTS = os.getenv("MANUAL_PAYMENTS", "0") == "1"
MANUAL_DETAILS = os.getenv("MANUAL_DETAILS", "Реквизиты не указаны").replace("\\n", "\n")
DB_PATH = os.getenv("DB_PATH", "shop.db")

logging.basicConfig(level=logging.INFO)
router = Router()
e = html.escape  # экранирование текста для HTML


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ================== СОСТОЯНИЯ ==================
class AddProduct(StatesGroup):
    title = State()
    description = State()
    price_stars = State()
    price_rub = State()
    photo = State()
    content = State()


class EditProduct(StatesGroup):
    value = State()


class Broadcast(StatesGroup):
    message = State()


class ManualPay(StatesGroup):
    receipt = State()


# ================== КЛАВИАТУРЫ ==================
def ikb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def main_kb(uid):
    rows = [
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="📦 Мои покупки")],
        [KeyboardButton(text="💬 Поддержка")],
    ]
    if is_admin(uid):
        rows.append([KeyboardButton(text="⚙️ Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


ADMIN_MENU = ikb([
    [("➕ Добавить товар", "adm:add")],
    [("📋 Товары", "adm:list"), ("🧾 Заказы", "adm:orders")],
    [("📊 Статистика", "adm:stats"), ("📢 Рассылка", "adm:bc")],
])

CANCEL_KB = ikb([[("✖️ Отмена", "cancel")]])


def price_text(p):
    s = f"⭐ {p['price_stars']} Stars"
    if MANUAL_PAYMENTS and p["price_rub"]:
        s += f"  ·  💳 {p['price_rub']} ₽"
    return s


def extract_content(msg: Message):
    """Достаёт из сообщения админа содержимое товара: текст/ссылку или файл."""
    if msg.document:
        return "document", msg.document.file_id
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.audio:
        return "audio", msg.audio.file_id
    if msg.text:
        return "text", msg.html_text
    return None, None


async def deliver(bot: Bot, user_id: int, product: dict):
    """Выдаёт покупателю купленный товар."""
    await bot.send_message(user_id, f"✅ Ваш товар: <b>{e(product['title'])}</b>")
    kind, c = product["content_kind"], product["content"]
    if kind == "text":
        await bot.send_message(user_id, c, disable_web_page_preview=False)
    elif kind == "document":
        await bot.send_document(user_id, c)
    elif kind == "photo":
        await bot.send_photo(user_id, c)
    elif kind == "video":
        await bot.send_video(user_id, c)
    elif kind == "audio":
        await bot.send_audio(user_id, c)


async def notify_admins(bot: Bot, text: str, **kw):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, **kw)
        except Exception as ex:  # админ мог не запустить бота
            logging.warning("Не удалось уведомить админа %s: %s", aid, ex)


# ================== ОБЩИЕ КОМАНДЫ ==================
@router.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    u = msg.from_user
    await db.add_user(u.id, u.username, u.first_name)
    await msg.answer(
        f"👋 Добро пожаловать в <b>{e(SHOP_NAME)}</b>!\n\n"
        "Здесь можно купить цифровые товары — товар приходит сюда же сразу после оплаты.\n\n"
        "Откройте 🛍 <b>Каталог</b>, чтобы выбрать.",
        reply_markup=main_kb(u.id),
    )


@router.callback_query(F.data == "cancel")
async def cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.answer()


@router.message(F.text == "💬 Поддержка")
@router.message(Command("support", "paysupport"))
async def support(msg: Message):
    await msg.answer(
        f"💬 По любым вопросам, в том числе по оплате и возвратам, пишите: {e(SUPPORT)}\n"
        "Укажите номер заказа — так мы поможем быстрее."
    )


@router.message(Command("terms"))
async def terms(msg: Message):
    await msg.answer(
        "📄 <b>Условия</b>\n"
        "• Товары цифровые и выдаются автоматически сразу после оплаты.\n"
        "• Если товар не пришёл или не соответствует описанию — напишите в поддержку, "
        "мы выдадим его повторно или вернём оплату.\n"
        f"• Поддержка: {e(SUPPORT)}"
    )


# ================== КАТАЛОГ ==================
async def show_catalog(target: Message, edit=False):
    products = await db.get_products()
    if not products:
        text, kb = "Каталог пока пуст. Загляните позже 🙂", None
    else:
        text = "🛍 <b>Каталог</b>\nВыберите товар:"
        kb = ikb([[(f"{p['title']} — ⭐{p['price_stars']}", f"prod:{p['id']}")] for p in products])
    if edit:
        try:
            await target.delete()
        except Exception:
            pass
    await target.answer(text, reply_markup=kb)


@router.message(F.text == "🛍 Каталог")
@router.message(Command("catalog"))
async def catalog(msg: Message):
    await show_catalog(msg)


@router.callback_query(F.data == "cat")
async def catalog_cb(cb: CallbackQuery):
    await show_catalog(cb.message, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("prod:"))
async def product_card(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = await db.get_product(pid)
    if not p or not p["active"]:
        return await cb.answer("Товар недоступен", show_alert=True)

    rows = []
    if await db.has_paid(cb.from_user.id, pid):
        rows.append([("📥 Получить снова", f"get:{pid}")])
    rows.append([(f"⭐ Купить за {p['price_stars']} Stars", f"buys:{pid}")])
    if MANUAL_PAYMENTS and p["price_rub"]:
        rows.append([(f"💳 Карта / СБП — {p['price_rub']} ₽", f"buym:{pid}")])
    rows.append([("« Назад в каталог", "cat")])

    caption = f"<b>{e(p['title'])}</b>\n\n{e(p['description'] or '')}\n\n💰 {price_text(p)}"
    try:
        await cb.message.delete()
    except Exception:
        pass
    if p["photo_id"]:
        await cb.message.answer_photo(p["photo_id"], caption=caption[:1024], reply_markup=ikb(rows))
    else:
        await cb.message.answer(caption, reply_markup=ikb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("get:"))
async def get_again(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[1])
    p = await db.get_product(pid)
    if p and await db.has_paid(cb.from_user.id, pid):
        await deliver(bot, cb.from_user.id, p)
        await cb.answer()
    else:
        await cb.answer("Покупка не найдена", show_alert=True)


@router.message(F.text == "📦 Мои покупки")
async def my_purchases(msg: Message):
    items = await db.user_purchases(msg.from_user.id)
    if not items:
        return await msg.answer("У вас пока нет покупок.")
    seen, rows = set(), []
    for it in items:
        if it["product_id"] in seen:
            continue
        seen.add(it["product_id"])
        rows.append([(f"📥 {it['title']}", f"get:{it['product_id']}")])
    await msg.answer("📦 <b>Ваши покупки</b> — нажмите, чтобы получить снова:", reply_markup=ikb(rows))


# ================== ОПЛАТА STARS ==================
@router.callback_query(F.data.startswith("buys:"))
async def buy_stars(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[1])
    p = await db.get_product(pid)
    if not p or not p["active"]:
        return await cb.answer("Товар недоступен", show_alert=True)
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title=p["title"][:32],
        description=(p["description"] or p["title"])[:255],
        payload=f"p:{pid}",
        currency="XTR",            # XTR = Telegram Stars
        provider_token="",         # для Stars токен провайдера не нужен
        prices=[LabeledPrice(label=p["title"][:32], amount=p["price_stars"])],
    )
    await cb.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try:
        pid = int(q.invoice_payload.split(":")[1])
        p = await db.get_product(pid)
        ok = bool(p and p["active"] and p["price_stars"] == q.total_amount)
    except Exception:
        ok = False
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Товар недоступен или цена изменилась. Откройте каталог заново.")


@router.message(F.successful_payment)
async def paid_stars(msg: Message, bot: Bot):
    sp = msg.successful_payment
    pid = int(sp.invoice_payload.split(":")[1])
    p = await db.get_product(pid)
    u = msg.from_user
    oid = await db.create_order(
        u.id, u.username, pid, "stars", sp.total_amount, "paid", sp.telegram_payment_charge_id
    )
    await msg.answer(f"🎉 Оплата прошла! Заказ №{oid}.")
    if p:
        await deliver(bot, u.id, p)
    await notify_admins(
        bot,
        f"💰 <b>Новая продажа</b> №{oid}\n"
        f"Товар: {e(p['title'] if p else str(pid))}\n"
        f"Сумма: ⭐ {sp.total_amount}\n"
        f"Покупатель: {e(u.full_name)} (@{e(u.username or '—')}, id {u.id})",
    )


# ================== РУЧНАЯ ОПЛАТА (карта / СБП) ==================
@router.callback_query(F.data.startswith("buym:"))
async def buy_manual(cb: CallbackQuery, state: FSMContext):
    if not MANUAL_PAYMENTS:
        return await cb.answer("Способ недоступен", show_alert=True)
    pid = int(cb.data.split(":")[1])
    p = await db.get_product(pid)
    if not p or not p["active"] or not p["price_rub"]:
        return await cb.answer("Товар недоступен", show_alert=True)
    u = cb.from_user
    oid = await db.create_order(u.id, u.username, pid, "manual", p["price_rub"], "pending")
    await state.set_state(ManualPay.receipt)
    await state.update_data(oid=oid)
    await cb.message.answer(
        f"💳 <b>Заказ №{oid}</b> — {e(p['title'])}\n"
        f"Сумма к оплате: <b>{p['price_rub']} ₽</b>\n\n"
        f"{e(MANUAL_DETAILS)}\n\n"
        f"В комментарии к переводу укажите: <code>{oid}</code>\n\n"
        "После оплаты пришлите сюда <b>скриншот или файл чека</b>. "
        "Товар придёт после проверки оплаты.",
        reply_markup=CANCEL_KB,
    )
    await cb.answer()


@router.message(ManualPay.receipt, F.photo | F.document)
async def manual_receipt(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    oid = data.get("oid")
    await state.clear()
    order = await db.get_order(oid)
    p = await db.get_product(order["product_id"]) if order else None
    if not order or not p:
        return await msg.answer("Заказ не найден. Начните заново из каталога.")
    await msg.answer(f"✅ Чек получен. Заказ №{oid} на проверке — обычно это занимает немного времени.")
    u = msg.from_user
    kb = ikb([[("✅ Подтвердить", f"mok:{oid}"), ("❌ Отклонить", f"mno:{oid}")]])
    for aid in ADMIN_IDS:
        try:
            await msg.copy_to(aid)
            await bot.send_message(
                aid,
                f"🧾 <b>Чек по заказу №{oid}</b>\n"
                f"Товар: {e(p['title'])}\nСумма: {order['amount']} ₽\n"
                f"Покупатель: {e(u.full_name)} (@{e(u.username or '—')}, id {u.id})",
                reply_markup=kb,
            )
        except Exception as ex:
            logging.warning("Не удалось отправить чек админу %s: %s", aid, ex)


@router.message(ManualPay.receipt)
async def manual_receipt_wrong(msg: Message):
    await msg.answer("Пришлите, пожалуйста, скриншот или файл чека (или нажмите «Отмена»).")


@router.callback_query(F.data.startswith(("mok:", "mno:")))
async def manual_decide(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer()
    action, oid = cb.data.split(":")
    oid = int(oid)
    order = await db.get_order(oid)
    if not order or order["status"] != "pending":
        return await cb.answer("Заказ уже обработан", show_alert=True)
    p = await db.get_product(order["product_id"])
    if action == "mok":
        await db.set_order_status(oid, "paid")
        await bot.send_message(order["user_id"], f"🎉 Оплата по заказу №{oid} подтверждена!")
        if p:
            await deliver(bot, order["user_id"], p)
        await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Подтверждено, товар выдан</b>")
    else:
        await db.set_order_status(oid, "rejected")
        await bot.send_message(
            order["user_id"],
            f"❌ Оплата по заказу №{oid} не подтверждена. Если это ошибка — напишите в поддержку: {e(SUPPORT)}",
        )
        await cb.message.edit_text(cb.message.html_text + "\n\n❌ <b>Отклонено</b>")
    await cb.answer()


# ================== АДМИНКА ==================
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


@admin.message(F.text == "⚙️ Админка")
@admin.message(Command("admin"))
async def admin_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("⚙️ <b>Панель администратора</b>", reply_markup=ADMIN_MENU)


@admin.callback_query(F.data == "adm:menu")
async def admin_menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("⚙️ <b>Панель администратора</b>", reply_markup=ADMIN_MENU)
    await cb.answer()


# ---- добавление товара (пошагово) ----
@admin.callback_query(F.data == "adm:add")
async def add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddProduct.title)
    await cb.message.answer("1/6. Введите <b>название</b> товара (до 32 символов):", reply_markup=CANCEL_KB)
    await cb.answer()


@admin.message(AddProduct.title, F.text)
async def add_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip()[:32])
    await state.set_state(AddProduct.description)
    await msg.answer("2/6. Введите <b>описание</b> товара (до 255 символов):", reply_markup=CANCEL_KB)


@admin.message(AddProduct.description, F.text)
async def add_desc(msg: Message, state: FSMContext):
    await state.update_data(description=msg.text.strip()[:255])
    await state.set_state(AddProduct.price_stars)
    await msg.answer("3/6. Цена в <b>Telegram Stars</b> (целое число, например 150):", reply_markup=CANCEL_KB)


@admin.message(AddProduct.price_stars, F.text)
async def add_stars(msg: Message, state: FSMContext):
    if not msg.text.isdigit() or int(msg.text) < 1:
        return await msg.answer("Нужно целое число больше 0.")
    await state.update_data(price_stars=int(msg.text))
    await state.set_state(AddProduct.price_rub)
    await msg.answer(
        "4/6. Цена в <b>рублях</b> для оплаты картой/СБП.\nОтправьте 0, если этот способ не нужен:",
        reply_markup=CANCEL_KB,
    )


@admin.message(AddProduct.price_rub, F.text)
async def add_rub(msg: Message, state: FSMContext):
    if not msg.text.isdigit():
        return await msg.answer("Нужно целое число (или 0).")
    await state.update_data(price_rub=int(msg.text))
    await state.set_state(AddProduct.photo)
    await msg.answer(
        "5/6. Пришлите <b>картинку-обложку</b> товара или нажмите «Без фото»:",
        reply_markup=ikb([[("Без фото", "nophoto")], [("✖️ Отмена", "cancel")]]),
    )


@admin.message(AddProduct.photo, F.photo)
async def add_photo(msg: Message, state: FSMContext):
    await state.update_data(photo_id=msg.photo[-1].file_id)
    await ask_content(msg, state)


@admin.callback_query(AddProduct.photo, F.data == "nophoto")
async def add_nophoto(cb: CallbackQuery, state: FSMContext):
    await state.update_data(photo_id=None)
    await ask_content(cb.message, state)
    await cb.answer()


async def ask_content(msg: Message, state: FSMContext):
    await state.set_state(AddProduct.content)
    await msg.answer(
        "6/6. Пришлите <b>то, что получит покупатель</b>:\n"
        "• файл (PDF, архив и т.п.), фото, видео или аудио\n"
        "• или текст: ссылку, ключ, промпт, инструкцию",
        reply_markup=CANCEL_KB,
    )


@admin.message(AddProduct.content)
async def add_content(msg: Message, state: FSMContext):
    kind, content = extract_content(msg)
    if not kind:
        return await msg.answer("Не понял формат. Пришлите файл, фото, видео, аудио или текст.")
    d = await state.get_data()
    await state.clear()
    pid = await db.add_product(
        d["title"], d["description"], d["price_stars"], d["price_rub"], d.get("photo_id"), kind, content
    )
    await msg.answer(
        f"✅ Товар №{pid} «{e(d['title'])}» добавлен и уже виден в каталоге.",
        reply_markup=ikb([[("Открыть товар", f"aprod:{pid}")], [("« В админку", "adm:menu")]]),
    )


# ---- список и редактирование товаров ----
@admin.callback_query(F.data == "adm:list")
async def admin_list(cb: CallbackQuery):
    products = await db.get_products(only_active=False)
    if not products:
        await cb.message.edit_text("Товаров пока нет.", reply_markup=ikb([[("« Назад", "adm:menu")]]))
        return await cb.answer()
    rows = [[(("🟢 " if p["active"] else "⚪️ ") + f"{p['title']} — ⭐{p['price_stars']}", f"aprod:{p['id']}")]
            for p in products]
    rows.append([("« Назад", "adm:menu")])
    await cb.message.edit_text("📋 <b>Товары</b> (🟢 в продаже, ⚪️ скрыт):", reply_markup=ikb(rows))
    await cb.answer()


def admin_product_kb(p):
    pid = p["id"]
    return ikb([
        [("🙈 Скрыть" if p["active"] else "👁 Показать", f"atog:{pid}")],
        [("✏️ Название", f"aedit:{pid}:title"), ("✏️ Описание", f"aedit:{pid}:description")],
        [("⭐ Цена Stars", f"aedit:{pid}:price_stars"), ("💳 Цена ₽", f"aedit:{pid}:price_rub")],
        [("🖼 Обложка", f"aedit:{pid}:photo_id"), ("📦 Содержимое", f"aedit:{pid}:content")],
        [("👀 Проверить выдачу", f"atest:{pid}")],
        [("🗑 Удалить", f"adel:{pid}")],
        [("« К списку", "adm:list")],
    ])


async def admin_product_text(p):
    return (
        f"<b>№{p['id']} {e(p['title'])}</b> {'🟢 в продаже' if p['active'] else '⚪️ скрыт'}\n\n"
        f"{e(p['description'] or '')}\n\n"
        f"⭐ {p['price_stars']} Stars · 💳 {p['price_rub']} ₽\n"
        f"Обложка: {'есть' if p['photo_id'] else 'нет'} · Выдача: {p['content_kind']}"
    )


@admin.callback_query(F.data.startswith("aprod:"))
async def admin_product(cb: CallbackQuery):
    p = await db.get_product(int(cb.data.split(":")[1]))
    if not p:
        return await cb.answer("Не найден", show_alert=True)
    try:
        await cb.message.edit_text(await admin_product_text(p), reply_markup=admin_product_kb(p))
    except Exception:
        await cb.message.answer(await admin_product_text(p), reply_markup=admin_product_kb(p))
    await cb.answer()


@admin.callback_query(F.data.startswith("atog:"))
async def admin_toggle(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = await db.get_product(pid)
    await db.update_product(pid, "active", 0 if p["active"] else 1)
    p = await db.get_product(pid)
    await cb.message.edit_text(await admin_product_text(p), reply_markup=admin_product_kb(p))
    await cb.answer("Готово")


@admin.callback_query(F.data.startswith("atest:"))
async def admin_test(cb: CallbackQuery, bot: Bot):
    p = await db.get_product(int(cb.data.split(":")[1]))
    await deliver(bot, cb.from_user.id, p)
    await cb.answer("Так покупатель получит товар 👆")


EDIT_PROMPTS = {
    "title": "Новое название (до 32 символов):",
    "description": "Новое описание (до 255 символов):",
    "price_stars": "Новая цена в Stars (целое число):",
    "price_rub": "Новая цена в рублях (0 — отключить оплату картой/СБП):",
    "photo_id": "Пришлите новую картинку-обложку (или «-», чтобы убрать):",
    "content": "Пришлите новое содержимое (файл, фото, видео, аудио или текст):",
}


@admin.callback_query(F.data.startswith("aedit:"))
async def admin_edit(cb: CallbackQuery, state: FSMContext):
    _, pid, field = cb.data.split(":")
    await state.set_state(EditProduct.value)
    await state.update_data(pid=int(pid), field=field)
    await cb.message.answer(EDIT_PROMPTS[field], reply_markup=CANCEL_KB)
    await cb.answer()


@admin.message(EditProduct.value)
async def admin_edit_value(msg: Message, state: FSMContext):
    d = await state.get_data()
    pid, field = d["pid"], d["field"]
    if field in ("price_stars", "price_rub"):
        if not (msg.text or "").isdigit() or (field == "price_stars" and int(msg.text) < 1):
            return await msg.answer("Нужно целое число" + (" больше 0." if field == "price_stars" else "."))
        await db.update_product(pid, field, int(msg.text))
    elif field == "photo_id":
        if msg.photo:
            await db.update_product(pid, field, msg.photo[-1].file_id)
        elif msg.text == "-":
            await db.update_product(pid, field, None)
        else:
            return await msg.answer("Пришлите картинку или «-».")
    elif field == "content":
        kind, content = extract_content(msg)
        if not kind:
            return await msg.answer("Не понял формат. Пришлите файл, фото, видео, аудио или текст.")
        await db.update_product(pid, "content_kind", kind)
        await db.update_product(pid, "content", content)
    else:
        if not msg.text:
            return await msg.answer("Нужен текст.")
        limit = 32 if field == "title" else 255
        await db.update_product(pid, field, msg.text.strip()[:limit])
    await state.clear()
    p = await db.get_product(pid)
    await msg.answer("✅ Сохранено.\n\n" + await admin_product_text(p), reply_markup=admin_product_kb(p))


@admin.callback_query(F.data.startswith("adel:"))
async def admin_delete(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    await cb.message.edit_text(
        "Удалить товар навсегда? Покупатели потеряют возможность получить его повторно.\n"
        "Если хотите просто убрать из продажи — нажмите «Скрыть».",
        reply_markup=ikb([[("🗑 Да, удалить", f"adelok:{pid}"), ("Нет", f"aprod:{pid}")]]),
    )
    await cb.answer()


@admin.callback_query(F.data.startswith("adelok:"))
async def admin_delete_ok(cb: CallbackQuery):
    await db.delete_product(int(cb.data.split(":")[1]))
    await cb.answer("Удалено")
    await admin_list(cb)


# ---- заказы, статистика, рассылка, возврат ----
STATUS_ICON = {"paid": "✅", "pending": "⏳", "rejected": "❌", "refunded": "↩️"}


@admin.callback_query(F.data == "adm:orders")
async def admin_orders(cb: CallbackQuery):
    orders = await db.recent_orders()
    if not orders:
        text = "Заказов пока нет."
    else:
        lines = []
        for o in orders:
            cur = "⭐" if o["method"] == "stars" else "₽"
            lines.append(
                f"{STATUS_ICON.get(o['status'], '')} №{o['id']} · {e(o['title'] or '?')} · "
                f"{o['amount']}{cur} · @{e(o['username'] or str(o['user_id']))} · {o['created_at']}"
            )
        text = "🧾 <b>Последние заказы</b>\n\n" + "\n".join(lines)
    await cb.message.edit_text(text, reply_markup=ikb([[("« Назад", "adm:menu")]]))
    await cb.answer()


@admin.callback_query(F.data == "adm:stats")
async def admin_stats(cb: CallbackQuery):
    s = await db.stats()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {s['users']}\n"
        f"🛒 Продаж: {s['sales']}\n"
        f"⭐ Получено Stars: {s['stars']}\n"
        f"💳 Получено ₽ (вручную): {s['rub']}"
    )
    await cb.message.edit_text(text, reply_markup=ikb([[("« Назад", "adm:menu")]]))
    await cb.answer()


@admin.callback_query(F.data == "adm:bc")
async def admin_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.message)
    await cb.message.answer(
        "📢 Пришлите сообщение для рассылки всем пользователям (текст, фото, видео — что угодно):",
        reply_markup=CANCEL_KB,
    )
    await cb.answer()


@admin.message(Broadcast.message)
async def admin_broadcast_send(msg: Message, state: FSMContext):
    await state.clear()
    ids = await db.all_user_ids()
    ok = 0
    await msg.answer(f"Отправляю {len(ids)} пользователям…")
    for uid in ids:
        try:
            await msg.copy_to(uid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # чтобы не упереться в лимиты Telegram
    await msg.answer(f"✅ Доставлено: {ok} из {len(ids)}")


@admin.message(Command("refund"))
async def admin_refund(msg: Message, command: CommandObject, bot: Bot):
    """Возврат Stars: /refund <номер заказа>"""
    if not command.args or not command.args.strip().isdigit():
        return await msg.answer("Использование: <code>/refund 12</code> — где 12 номер заказа.")
    o = await db.get_order(int(command.args.strip()))
    if not o or o["status"] != "paid":
        return await msg.answer("Оплаченный заказ с таким номером не найден.")
    if o["method"] != "stars":
        await db.set_order_status(o["id"], "refunded")
        return await msg.answer("Заказ оплачен вручную — верните деньги переводом. Статус заказа изменён на «возврат».")
    try:
        await bot.refund_star_payment(user_id=o["user_id"], telegram_payment_charge_id=o["charge_id"])
        await db.set_order_status(o["id"], "refunded")
        await msg.answer(f"↩️ Stars по заказу №{o['id']} возвращены.")
        await bot.send_message(o["user_id"], f"↩️ Оплата по заказу №{o['id']} возвращена.")
    except Exception as ex:
        await msg.answer(f"Ошибка возврата: {e(str(ex))}")


# ================== ЗАПУСК ==================
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. файл .env)")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS не задан — админка будет недоступна")
    await db.init_db(DB_PATH)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)   # админка первой, чтобы её состояния обрабатывались раньше
    dp.include_router(router)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
