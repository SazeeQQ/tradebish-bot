"""
Telegram-бот для приёма объявлений о продаже товаров (смартфоны и т.п.)
с модерацией и публикацией в канал.

Поток:
  продавец заполняет анкету -> предпросмотр -> "Отправить на проверку"
  -> модераторы получают заявку с кнопками -> одобрили = пост в канале,
  отклонили = продавец получает причину.

Стек: Python 3.10+, aiogram 3.7+, aiosqlite.
"""
import asyncio
import html
import json
import logging
import os

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ──────────────────────────── Настройки ────────────────────────────

load_dotenv()

# Токен НЕ хранится в коде (репозиторий публичный): он берётся из переменных
# окружения хостинга. Поддержаны популярные названия переменной.
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TOKEN")
    or ""
).strip()
if not BOT_TOKEN:
    raise SystemExit(
        "Не задан токен бота. Добавьте переменную BOT_TOKEN в настройках хостинга."
    )

# Канал для публикаций (можно переопределить переменной CHANNEL_ID)
_channel = os.getenv("CHANNEL_ID", "@trade_bish").strip()
CHANNEL_ID = int(_channel) if _channel.lstrip("-").isdigit() else _channel

# Telegram ID модераторов через запятую (можно переопределить переменной ADMIN_IDS)
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "6408237678").split(",") if x.strip()
}

# В облаке Amvera постоянное хранилище смонтировано в /data — кладём базу туда
DB_PATH = os.getenv("DB_PATH", "/data/bot.db" if os.path.isdir("/data") else "bot.db")
MAX_PHOTOS = 10
CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото

log = logging.getLogger("ads_bot")
router = Router()
BOT_USERNAME: str | None = None
e = html.escape  # экранирование пользовательского текста для HTML

# ──────────────────────────── Анкета ────────────────────────────


class Form(StatesGroup):
    title = State()
    specs = State()
    description = State()
    defects = State()
    city = State()
    price = State()
    photos = State()
    confirm = State()


class Reject(StatesGroup):
    reason = State()


# (ключ, состояние, вопрос, макс. длина)
FIELDS = [
    (
        "title",
        Form.title,
        "📱 Что продаёте? Напишите название и модель.\n\n"
        "Например: <i>iPhone 13, чёрный</i>",
        100,
    ),
    (
        "specs",
        Form.specs,
        "⚙️ Характеристики — одним сообщением. Например:\n\n"
        "<i>Память: 128 ГБ\nОЗУ: 4 ГБ\nЭкран: 6.1\" OLED\n"
        "Состояние АКБ: 87%\nКомплект: коробка, кабель\nГарантия: нет</i>",
        700,
    ),
    (
        "description",
        Form.description,
        "📝 Описание от продавца: как пользовались, почему продаёте, "
        "возможен ли торг, обмен и т.д.",
        700,
    ),
    (
        "defects",
        Form.defects,
        "⚠️ Недочёты: царапины, сколы, неисправности, заменённые детали.\n"
        "Если их нет — напишите «Нет».",
        400,
    ),
    ("city", Form.city, "📍 В каком городе находится товар?", 50),
    (
        "price",
        Form.price,
        "💰 Цена. Укажите с валютой, например: <i>45 000 ₽</i>",
        30,
    ),
]
FIELD_STATES = [f[1] for f in FIELDS]

PHOTOS_PROMPT = (
    f"📸 Пришлите фото товара (до {MAX_PHOTOS} шт.).\n"
    "Когда закончите — нажмите «✅ Готово»."
)

BTN_NEW = "➕ Новое объявление"
BTN_MY = "📋 Мои объявления"
BTN_DONE = "✅ Готово"

menu_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_MY)]],
    resize_keyboard=True,
)
done_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_DONE)]], resize_keyboard=True
)

STATUS_TEXT = {
    "pending": "⏳ на проверке",
    "approved": "✅ опубликовано",
    "rejected": "❌ отклонено",
    "withdrawn": "🗑 снято с публикации",
}

# ──────────────────────────── База данных ────────────────────────────


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                username      TEXT,
                title         TEXT NOT NULL,
                specs         TEXT NOT NULL,
                description   TEXT NOT NULL,
                defects       TEXT NOT NULL,
                city          TEXT NOT NULL,
                price         TEXT NOT NULL,
                photos        TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                channel_chat_id TEXT,
                channel_message_id INTEGER,
                channel_message_ids TEXT,
                views_clicks INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Миграция существующей базы без потери старых объявлений.
        cur = await db.execute("PRAGMA table_info(ads)")
        columns = {row[1] for row in await cur.fetchall()}
        migrations = {
            "channel_chat_id": "ALTER TABLE ads ADD COLUMN channel_chat_id TEXT",
            "channel_message_id": "ALTER TABLE ads ADD COLUMN channel_message_id INTEGER",
            "channel_message_ids": "ALTER TABLE ads ADD COLUMN channel_message_ids TEXT",
            "views_clicks": "ALTER TABLE ads ADD COLUMN views_clicks INTEGER NOT NULL DEFAULT 0",
        }
        for column, sql in migrations.items():
            if column not in columns:
                await db.execute(sql)
        await db.commit()


def _row_to_ad(row: aiosqlite.Row) -> dict:
    ad = dict(row)
    ad["photos"] = json.loads(ad["photos"])
    return ad


async def create_ad(ad: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO ads (user_id, username, title, specs, description,
                             defects, city, price, photos)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ad["user_id"],
                ad["username"],
                ad["title"],
                ad["specs"],
                ad["description"],
                ad["defects"],
                ad["city"],
                ad["price"],
                json.dumps(ad["photos"]),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_ad(ad_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ads WHERE id = ?", (ad_id,))
        row = await cur.fetchone()
        return _row_to_ad(row) if row else None


async def get_user_ads(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM ads WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [_row_to_ad(r) for r in await cur.fetchall()]


async def save_channel_post(ad_id: int, chat_id: int | str, message_ids: list[int], primary_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ads SET channel_chat_id = ?, channel_message_id = ?, channel_message_ids = ? WHERE id = ?",
            (str(chat_id), primary_id, json.dumps(message_ids), ad_id),
        )
        await db.commit()


async def increment_ad_views(ad_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE ads SET views_clicks = views_clicks + 1 WHERE id = ? AND status = 'approved'",
            (ad_id,),
        )
        await db.commit()
        return cur.rowcount == 1


async def set_status(
    ad_id: int,
    status: str,
    reason: str | None = None,
    expected: str | None = None,
) -> bool:
    """Меняет статус. Если задан expected — только если текущий статус совпадает
    (защита от того, что два модератора нажмут кнопки одновременно)."""
    sql = "UPDATE ads SET status = ?, reject_reason = ? WHERE id = ?"
    params: list = [status, reason, ad_id]
    if expected:
        sql += " AND status = ?"
        params.append(expected)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount == 1


# ──────────────────────────── Оформление поста ────────────────────────────


def build_post(ad: dict) -> str:
    if ad["username"]:
        contact = f"@{e(ad['username'])}"
    else:
        contact = f'<a href="tg://user?id={ad["user_id"]}">написать продавцу</a>'
    return (
        f"<b>{e(ad['title'])}</b>\n\n"
        f"💰 <b>Цена:</b> {e(ad['price'])}\n"
        f"📍 <b>Город:</b> {e(ad['city'])}\n\n"
        f"⚙️ <b>Характеристики:</b>\n{e(ad['specs'])}\n\n"
        f"📝 <b>Описание:</b>\n{e(ad['description'])}\n\n"
        f"⚠️ <b>Недочёты:</b>\n{e(ad['defects'])}\n\n"
        f"👤 <b>Продавец:</b> {contact}"
    )


async def send_ad(
    bot: Bot,
    chat_id: int | str,
    ad: dict,
    track_ad_id: int | None = None,
) -> dict:
    """Отправляет объявление. Для публикации в канале добавляет ссылку
    на бота для подсчёта переходов и возвращает ID отправленных сообщений."""
    text = build_post(ad)
    photos = ad["photos"]
    fits = len(text) <= CAPTION_LIMIT
    tracked = track_ad_id is not None and bool(BOT_USERNAME)

    tracking_kb = None
    if tracked:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="👁 Открыть карточку / статистика",
            url=f"https://t.me/{BOT_USERNAME}?start=view_{track_ad_id}",
        )
        tracking_kb = kb.as_markup()

    message_ids: list[int] = []
    primary_id: int | None = None

    if len(photos) == 1:
        if fits:
            msg = await bot.send_photo(
                chat_id, photos[0], caption=text, reply_markup=tracking_kb
            )
            message_ids.append(msg.message_id)
            primary_id = msg.message_id
        else:
            photo_msg = await bot.send_photo(chat_id, photos[0])
            message_ids.append(photo_msg.message_id)
            text_msg = await bot.send_message(
                chat_id, text, reply_markup=tracking_kb
            )
            message_ids.append(text_msg.message_id)
            primary_id = text_msg.message_id
        return {"message_ids": message_ids, "primary_id": primary_id}

    media = [
        InputMediaPhoto(
            media=file_id,
            caption=text if (i == 0 and fits) else None,
            parse_mode=ParseMode.HTML,
        )
        for i, file_id in enumerate(photos)
    ]
    sent = await bot.send_media_group(chat_id, media)
    message_ids.extend(msg.message_id for msg in sent)

    if tracked:
        # У альбома Telegram не поддерживает inline-клавиатуру, поэтому
        # добавляем отдельное сообщение со ссылкой на отслеживаемую карточку.
        cta_text = "👁 Статистика и карточка этого объявления"
        if not fits:
            cta_text = text + "\n\n👁 Переходы по объявлению учитываются по кнопке ниже."
        cta = await bot.send_message(chat_id, cta_text, reply_markup=tracking_kb)
        message_ids.append(cta.message_id)
        primary_id = cta.message_id
    elif not fits:
        text_msg = await bot.send_message(chat_id, text)
        message_ids.append(text_msg.message_id)
        primary_id = text_msg.message_id
    else:
        primary_id = sent[0].message_id

    return {"message_ids": message_ids, "primary_id": primary_id}


async def withdraw_channel_post(bot: Bot, ad: dict) -> bool:
    """Заменяет публикацию уведомлением и удаляет остальные сообщения альбома."""
    chat_id = ad.get("channel_chat_id")
    primary_id = ad.get("channel_message_id")
    raw_ids = ad.get("channel_message_ids")
    if not chat_id or not primary_id:
        return False

    try:
        ids = json.loads(raw_ids) if raw_ids else [primary_id]
    except (TypeError, json.JSONDecodeError):
        ids = [primary_id]

    notice = (
        "🗑 <b>Объявление снято с публикации</b>\n\n"
        "Продавец больше не предлагает этот товар.\n"
        "Публикация неактуальна."
    )

    edited = False
    try:
        await bot.edit_message_text(
            notice, chat_id=chat_id, message_id=primary_id, reply_markup=None
        )
        edited = True
    except TelegramAPIError:
        try:
            await bot.edit_message_caption(
                notice, chat_id=chat_id, message_id=primary_id, reply_markup=None
            )
            edited = True
        except TelegramAPIError:
            log.exception("Не удалось изменить публикацию объявления #%s", ad["id"])
            return False

    # Удаляем фото/части альбома, оставляя только изменённое сообщение-уведомление.
    for message_id in ids:
        if message_id == primary_id:
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            log.warning("Не удалось удалить сообщение %s объявления #%s", message_id, ad["id"])
    return edited


def ad_from_state(data: dict, user: User) -> dict:
    return {
        "user_id": user.id,
        "username": user.username,
        **{k: data[k] for k in (f[0] for f in FIELDS)},
        "photos": data["photos"],
    }


async def safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        log.warning("Не удалось отправить сообщение пользователю %s", chat_id)


# ──────────────────────────── Общие команды ────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    if payload.startswith("view_"):
        try:
            ad_id = int(payload.removeprefix("view_"))
        except ValueError:
            ad_id = 0
        ad = await get_ad(ad_id) if ad_id else None
        if ad and ad["status"] == "approved":
            await increment_ad_views(ad_id)
            channel_url = make_channel_post_url(ad)
            kb = InlineKeyboardBuilder()
            if channel_url:
                kb.button(text="📢 Перейти к объявлению в канале", url=channel_url)
            await message.answer(
                f"👁 Переход учтён.\n\n<b>{e(ad['title'])}</b>\n"
                f"💰 Цена: {e(ad['price'])}\n"
                f"📍 Город: {e(ad['city'])}",
                reply_markup=kb.as_markup() if channel_url else None,
            )
            await message.answer("Вы также можете разместить собственное объявление.", reply_markup=menu_kb)
            return
        await message.answer("Это объявление больше не доступно.", reply_markup=menu_kb)
        return

    await message.answer(
        "👋 Привет! Я помогу оформить объявление о продаже.\n\n"
        "Заполните короткую анкету, проверьте предпросмотр — и я отправлю "
        "объявление на модерацию. После одобрения оно появится в канале.",
        reply_markup=menu_kb,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=menu_kb)


async def begin_form(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FIELDS[0][1])
    await message.answer(
        FIELDS[0][2] + "\n\n<i>/cancel — отмена</i>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("new"))
@router.message(F.text == BTN_NEW)
async def cmd_new(message: Message, state: FSMContext) -> None:
    await begin_form(message, state)


@router.message(Command("my"))
@router.message(F.text == BTN_MY)
async def cmd_my(message: Message) -> None:
    ads = await get_user_ads(message.from_user.id, limit=20)
    if not ads:
        await message.answer("У вас пока нет объявлений.")
        return

    kb = InlineKeyboardBuilder()
    for ad in ads:
        status = STATUS_TEXT.get(ad["status"], ad["status"])
        kb.button(
            text=f"#{ad['id']} · {ad['title'][:35]} · {status}",
            callback_data=f"myad:view:{ad['id']}",
        )
    kb.adjust(1)
    await message.answer("📋 <b>Ваши объявления</b>\nВыберите объявление для просмотра и управления:", reply_markup=kb.as_markup())


def make_channel_post_url(ad: dict) -> str | None:
    """Формирует ссылку на пост публичного или приватного канала."""
    message_id = ad.get("channel_message_id")
    if not message_id:
        return None
    channel = str(ad.get("channel_chat_id") or CHANNEL_ID)
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    if channel.startswith("-100"):
        return f"https://t.me/c/{channel[4:]}/{message_id}"
    return None


async def show_user_ad(callback: CallbackQuery, ad_id: int) -> None:
    ad = await get_ad(ad_id)
    if not ad or ad["user_id"] != callback.from_user.id:
        await callback.answer("Объявление не найдено или у вас нет доступа.", show_alert=True)
        return

    if len(ad["photos"]) == 1:
        await callback.message.answer_photo(ad["photos"][0])
    elif ad["photos"]:
        media = [InputMediaPhoto(media=file_id) for file_id in ad["photos"]]
        await callback.message.answer_media_group(media)

    details = (
        f"📱 <b>{e(ad['title'])}</b>\n\n"
        f"💰 <b>Цена:</b> {e(ad['price'])}\n"
        f"📍 <b>Город:</b> {e(ad['city'])}\n\n"
        f"⚙️ <b>Характеристики:</b>\n{e(ad['specs'])}\n\n"
        f"📝 <b>Описание:</b>\n{e(ad['description'])}\n\n"
        f"⚠️ <b>Недочёты:</b>\n{e(ad['defects'])}\n\n"
        f"📊 <b>Статус:</b> {STATUS_TEXT.get(ad['status'], ad['status'])}\n"
        f"👁 <b>Переходы по ссылке:</b> {ad.get('views_clicks', 0)}\n"
        f"📈 <b>Просмотры Telegram:</b> недоступны через стандартный Bot API"
    )
    kb = InlineKeyboardBuilder()
    if ad["status"] == "approved":
        if ad.get("channel_message_id"):
            kb.button(text="🗑 Снять с публикации", callback_data=f"myad:remove:{ad_id}")
        else:
            details += "\n\nℹ️ Это объявление опубликовано до обновления бота. Управление старой публикацией пока недоступно."
        post_url = make_channel_post_url(ad)
        if post_url:
            kb.button(text="📢 Открыть пост в канале", url=post_url)
    kb.button(text="◀️ К списку объявлений", callback_data="myad:list")
    kb.adjust(1)
    await callback.message.answer(details, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "myad:list")
async def myad_list_callback(cb: CallbackQuery) -> None:
    ads = await get_user_ads(cb.from_user.id, limit=20)
    if not ads:
        await cb.message.answer("У вас пока нет объявлений.")
        await cb.answer()
        return
    kb = InlineKeyboardBuilder()
    for ad in ads:
        kb.button(
            text=f"#{ad['id']} · {ad['title'][:35]} · {STATUS_TEXT.get(ad['status'], ad['status'])}",
            callback_data=f"myad:view:{ad['id']}",
        )
    kb.adjust(1)
    await cb.message.answer("📋 <b>Ваши объявления</b>", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("myad:view:"))
async def myad_view(cb: CallbackQuery) -> None:
    try:
        ad_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный номер объявления.", show_alert=True)
        return
    await show_user_ad(cb, ad_id)


@router.callback_query(F.data.startswith("myad:remove:"))
async def myad_remove_confirm(cb: CallbackQuery) -> None:
    try:
        ad_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный номер объявления.", show_alert=True)
        return
    ad = await get_ad(ad_id)
    if not ad or ad["user_id"] != cb.from_user.id:
        await cb.answer("Нет доступа к этому объявлению.", show_alert=True)
        return
    if ad["status"] != "approved":
        await cb.answer("Это объявление уже нельзя снять с публикации.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, снять", callback_data=f"myad:confirm:{ad_id}")
    kb.button(text="Отмена", callback_data=f"myad:view:{ad_id}")
    kb.adjust(1)
    await cb.message.answer(
        f"⚠️ Снять объявление «{e(ad['title'])}» с публикации?\n"
        "Пост в канале будет заменён уведомлением о снятии.",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("myad:confirm:"))
async def myad_remove_execute(cb: CallbackQuery, bot: Bot) -> None:
    try:
        ad_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный номер объявления.", show_alert=True)
        return
    ad = await get_ad(ad_id)
    if not ad or ad["user_id"] != cb.from_user.id:
        await cb.answer("Нет доступа к этому объявлению.", show_alert=True)
        return
    if ad["status"] != "approved":
        await cb.answer("Объявление уже обработано.", show_alert=True)
        return
    if not ad.get("channel_message_id"):
        await cb.answer(
            "У этой старой публикации нет сохранённого ID сообщения. Обратитесь к администратору.",
            show_alert=True,
        )
        return

    if not await set_status(ad_id, "withdrawn", expected="approved"):
        await cb.answer("Объявление уже обрабатывается.", show_alert=True)
        return

    if not await withdraw_channel_post(bot, ad):
        await set_status(ad_id, "approved", expected="withdrawn")
        await cb.answer("Не удалось изменить пост в канале. Статус восстановлен.", show_alert=True)
        return

    await cb.message.edit_text(
        f"✅ Объявление «{e(ad['title'])}» снято с публикации.\n"
        f"Переходов по ссылке: {ad.get('views_clicks', 0)}"
    )
    await cb.answer("Публикация снята.")


# ──────────────────────────── Заполнение анкеты ────────────────────────────


@router.message(StateFilter(*FIELD_STATES), F.text)
async def collect_field(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    idx = next(i for i, f in enumerate(FIELDS) if f[1].state == current)
    key, _, _, limit = FIELDS[idx]

    value = message.text.strip()
    if len(value) > limit:
        await message.answer(f"Слишком длинно — максимум {limit} символов. Сократите, пожалуйста.")
        return
    if key == "price" and not any(ch.isdigit() for ch in value):
        await message.answer("Укажите цену цифрами, например: <i>45 000 ₽</i>")
        return

    await state.update_data(**{key: value})

    if idx + 1 < len(FIELDS):
        _, next_state, prompt, _ = FIELDS[idx + 1]
        await state.set_state(next_state)
        await message.answer(prompt)
    else:
        await state.set_state(Form.photos)
        await state.update_data(photos=[])
        await message.answer(PHOTOS_PROMPT, reply_markup=done_kb)


@router.message(StateFilter(*FIELD_STATES))
async def field_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, ответьте текстом.")


@router.message(Form.photos, F.photo)
async def add_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos: list[str] = data.get("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await message.answer(f"Максимум {MAX_PHOTOS} фото. Нажмите «{BTN_DONE}».")
        return
    photos.append(message.photo[-1].file_id)  # самое большое разрешение
    await state.update_data(photos=photos)
    await message.answer(f"📸 Фото {len(photos)}/{MAX_PHOTOS} принято.")


@router.message(Form.photos, F.text == BTN_DONE)
async def photos_done(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if not data.get("photos"):
        await message.answer("Нужно хотя бы одно фото товара.")
        return

    await state.set_state(Form.confirm)
    ad = ad_from_state(data, message.from_user)

    await message.answer(
        "👀 Так объявление увидят подписчики канала:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_ad(bot, message.chat.id, ad)

    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Отправить на проверку", callback_data="ad:submit")
    kb.button(text="🔄 Заполнить заново", callback_data="ad:restart")
    kb.button(text="🚫 Отмена", callback_data="ad:cancel")
    kb.adjust(1)
    await message.answer("Всё верно?", reply_markup=kb.as_markup())


@router.message(Form.photos)
async def photos_fallback(message: Message) -> None:
    await message.answer(f"Пришлите фото или нажмите «{BTN_DONE}».")


@router.message(Form.confirm)
async def confirm_fallback(message: Message) -> None:
    await message.answer("Используйте кнопки под предпросмотром или /cancel.")


# ──────────────────────────── Подтверждение ────────────────────────────


async def notify_admins(bot: Bot, ad_id: int, ad: dict) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать", callback_data=f"mod:approve:{ad_id}")
    kb.button(text="❌ Отклонить", callback_data=f"mod:reject:{ad_id}")
    kb.adjust(2)
    for admin_id in ADMIN_IDS:
        try:
            await send_ad(bot, admin_id, ad)
            await bot.send_message(
                admin_id,
                f"🆕 Заявка #{ad_id} на модерации",
                reply_markup=kb.as_markup(),
            )
        except TelegramAPIError:
            log.exception("Не удалось отправить заявку модератору %s", admin_id)


@router.callback_query(Form.confirm, F.data == "ad:submit")
async def ad_submit(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    ad = ad_from_state(data, cb.from_user)
    ad_id = await create_ad(ad)
    await state.clear()

    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        f"✅ Заявка #{ad_id} отправлена на проверку. Я сообщу о решении.",
        reply_markup=menu_kb,
    )
    await cb.answer()
    await notify_admins(bot, ad_id, ad)


@router.callback_query(Form.confirm, F.data == "ad:restart")
async def ad_restart(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await begin_form(cb.message, state)


@router.callback_query(Form.confirm, F.data == "ad:cancel")
async def ad_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Отменено.", reply_markup=menu_kb)
    await cb.answer()


@router.callback_query(F.data.startswith("ad:"))
async def ad_stale(cb: CallbackQuery) -> None:
    await cb.answer("Эта анкета уже неактуальна. Начните заново: /new", show_alert=True)


# ──────────────────────────── Модерация ────────────────────────────


@router.callback_query(F.data.startswith("mod:approve:"))
async def mod_approve(cb: CallbackQuery, bot: Bot) -> None:
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return

    ad_id = int(cb.data.rsplit(":", 1)[1])
    # атомарно "занимаем" заявку, чтобы не опубликовать дважды
    if not await set_status(ad_id, "approved", expected="pending"):
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    ad = await get_ad(ad_id)
    try:
        publication = await send_ad(bot, CHANNEL_ID, ad, track_ad_id=ad_id)
        await save_channel_post(
            ad_id, CHANNEL_ID, publication["message_ids"], publication["primary_id"]
        )
    except TelegramAPIError as exc:
        await set_status(ad_id, "pending")  # откатываем
        log.exception("Не удалось опубликовать заявку #%s", ad_id)
        await cb.answer(f"Ошибка публикации: {exc}"[:190], show_alert=True)
        return

    await cb.message.edit_text(
        f"✅ Заявка #{ad_id} опубликована\nМодератор: {e(cb.from_user.full_name)}"
    )
    await cb.answer()
    await safe_send(
        bot, ad["user_id"], f"🎉 Ваше объявление «{e(ad['title'])}» опубликовано в канале!"
    )


@router.callback_query(F.data.startswith("mod:reject:"))
async def mod_reject_start(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return

    ad_id = int(cb.data.rsplit(":", 1)[1])
    ad = await get_ad(ad_id)
    if not ad or ad["status"] != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    await state.set_state(Reject.reason)
    await state.update_data(
        ad_id=ad_id,
        chat_id=cb.message.chat.id,
        message_id=cb.message.message_id,
    )
    await cb.message.answer(
        f"Напишите причину отклонения заявки #{ad_id} — её получит продавец.\n"
        "/skip — без причины, /cancel — отмена."
    )
    await cb.answer()


@router.message(Reject.reason, F.text)
async def mod_reject_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()

    ad_id = data["ad_id"]
    text = message.text.strip()
    reason = None if text == "/skip" else text[:500]

    if not await set_status(ad_id, "rejected", reason, expected="pending"):
        await message.answer("Заявка уже обработана другим модератором.")
        return

    ad = await get_ad(ad_id)
    notice = f"❌ Ваше объявление «{e(ad['title'])}» не прошло проверку."
    if reason:
        notice += f"\nПричина: {e(reason)}"
    notice += "\n\nИсправьте и отправьте заново: /new"
    await safe_send(bot, ad["user_id"], notice)

    try:
        await bot.edit_message_text(
            f"❌ Заявка #{ad_id} отклонена\nМодератор: {e(message.from_user.full_name)}",
            chat_id=data["chat_id"],
            message_id=data["message_id"],
        )
    except TelegramAPIError:
        pass
    await message.answer(f"Заявка #{ad_id} отклонена, продавец уведомлён.")


# ──────────────────────────── Запуск ────────────────────────────


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
