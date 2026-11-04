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
    InlineKeyboardMarkup,
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
        "Например: <i>iPhone 13 128 ГБ, чёрный</i>",
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
    keyboard=[
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text=BTN_NEW)],
        [KeyboardButton(text=BTN_MY), KeyboardButton(text="⭐ Избранное")],
    ],
    resize_keyboard=True,
)
done_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_DONE)]], resize_keyboard=True
)

STATUS_TEXT = {
    "pending": "⏳ на проверке",
    "approved": "✅ опубликовано",
    "rejected": "❌ отклонено",
    "sold": "🏁 продано",
    "removed": "🗑 снято с публикации",
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
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Безопасные миграции: существующие объявления и база сохраняются.
        cur = await db.execute("PRAGMA table_info(ads)")
        columns = {row[1] for row in await cur.fetchall()}
        for name, sql_type in (("channel_message_id", "INTEGER"), ("views", "INTEGER NOT NULL DEFAULT 0"), ("favorites_count", "INTEGER NOT NULL DEFAULT 0"), ("updated_at", "TEXT")):
            if name not in columns:
                await db.execute(f"ALTER TABLE ads ADD COLUMN {name} {sql_type}")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL, ad_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, ad_id)
            );
            CREATE TABLE IF NOT EXISTS ad_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id INTEGER NOT NULL,
                user_id INTEGER, event TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ads_status_created ON ads(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ads_user_created ON ads(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_ad ON ad_events(ad_id, event);
        """)
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


async def toggle_favorite(user_id: int, ad_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND ad_id=?", (user_id, ad_id))
        exists = await cur.fetchone()
        if exists:
            await db.execute("DELETE FROM favorites WHERE user_id=? AND ad_id=?", (user_id, ad_id))
            added = False
        else:
            await db.execute("INSERT OR IGNORE INTO favorites(user_id, ad_id) VALUES(?,?)", (user_id, ad_id))
            added = True
        await db.execute("UPDATE ads SET favorites_count=(SELECT COUNT(*) FROM favorites WHERE ad_id=?) WHERE id=?", (ad_id, ad_id))
        await db.commit()
        return added


async def get_favorites(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT a.* FROM ads a JOIN favorites f ON f.ad_id=a.id WHERE f.user_id=? AND a.status='approved' ORDER BY f.created_at DESC", (user_id,))
        return [_row_to_ad(r) for r in await cur.fetchall()]


async def record_event(ad_id: int, user_id: int | None, event: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO ad_events(ad_id,user_id,event) VALUES(?,?,?)", (ad_id,user_id,event))
        if event == "view":
            await db.execute("UPDATE ads SET views=views+1 WHERE id=?", (ad_id,))
        await db.commit()


async def update_ad_status(ad_id: int, user_id: int, status: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE ads SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status IN ('approved','sold','removed')", (status,ad_id,user_id))
        await db.commit()
        return cur.rowcount == 1


async def get_public_ads(limit: int = 10, offset: int = 0) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ads WHERE status='approved' ORDER BY id DESC LIMIT ? OFFSET ?", (limit,offset))
        return [_row_to_ad(r) for r in await cur.fetchall()]


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
    contact = "Связь с продавцом — через Trade Bish"
    return (
        f"<b>{e(ad['title'])}</b>\n\n"
        f"💰 <b>Цена:</b> {e(ad['price'])}\n"
        f"📍 <b>Город:</b> {e(ad['city'])}\n\n"
        f"⚙️ <b>Характеристики:</b>\n{e(ad['specs'])}\n\n"
        f"📝 <b>Описание:</b>\n{e(ad['description'])}\n\n"
        f"⚠️ <b>Недочёты:</b>\n{e(ad['defects'])}\n\n"
        f"💬 <b>Связь:</b> {contact}"
    )


async def send_ad(bot: Bot, chat_id: int | str, ad: dict) -> None:
    """Отправляет объявление (фото + текст) в чат или канал.
    Если текст не влезает в подпись — отправляет его отдельным сообщением."""
    text = build_post(ad)
    photos = ad["photos"]
    fits = len(text) <= CAPTION_LIMIT

    kb = InlineKeyboardBuilder()
    if isinstance(ad.get("id"), int):
        kb.button(text="👁 Открыть / перейти", callback_data=f"visit:{ad['id']}")
        kb.button(text="💬 Написать продавцу", callback_data=f"contact:{ad['id']}")
        kb.button(text="♡ В избранное", callback_data=f"fav:{ad['id']}")
        kb.adjust(1, 1, 1)
    markup = kb.as_markup() if isinstance(ad.get("id"), int) else None
    if len(photos) == 1:
        if fits:
            msg = await bot.send_photo(chat_id, photos[0], caption=text, reply_markup=markup)
        else:
            msg = await bot.send_photo(chat_id, photos[0], reply_markup=markup)
            await bot.send_message(chat_id, text)
        return msg

    media = [
        InputMediaPhoto(
            media=file_id,
            caption=text if (i == 0 and fits) else None,
            parse_mode=ParseMode.HTML,
        )
        for i, file_id in enumerate(photos)
    ]
    sent = await bot.send_media_group(chat_id, media)
    if not fits:
        await bot.send_message(chat_id, text)
    # Кнопки публикуем отдельным сообщением, так как у media-group нет reply_markup.
    if markup:
        await bot.send_message(chat_id, "Действия с объявлением:", reply_markup=markup)
    return sent[0] if sent else None


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
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Привет! Я помогу оформить объявление о продаже.\n\n"
        "Заполните короткую анкету, проверьте предпросмотр — и я отправлю "
        "объявление на модерацию. После одобрения оно появится в канале.",
        reply_markup=menu_kb,
    )


@router.message(Command("catalog"))
@router.message(F.text == "🛍 Каталог")
async def cmd_catalog(message: Message) -> None:
    ads = await get_public_ads()
    if not ads:
        await message.answer("Пока нет опубликованных объявлений. Загляните позже!")
        return
    await message.answer("🛍 <b>Свежие объявления</b>")
    for ad in ads:
        await send_ad(message.bot, message.chat.id, ad)


@router.message(Command("favorites"))
@router.message(F.text == "⭐ Избранное")
async def cmd_favorites(message: Message) -> None:
    ads = await get_favorites(message.from_user.id)
    if not ads:
        await message.answer("В избранном пока пусто. Добавляйте объявления кнопкой ♡.")
        return
    await message.answer("⭐ <b>Ваше избранное</b>")
    for ad in ads:
        await send_ad(message.bot, message.chat.id, ad)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    ads = await get_user_ads(message.from_user.id, 100)
    if not ads:
        await message.answer("У вас пока нет объявлений.")
        return
    total_views = sum(int(a.get("views") or 0) for a in ads)
    total_favs = sum(int(a.get("favorites_count") or 0) for a in ads)
    lines = [f"#{a['id']} · {e(a['title'])}\n{STATUS_TEXT.get(a['status'], a['status'])} · 👁 переходы: {a.get('views',0)} · ⭐ {a.get('favorites_count',0)}" for a in ads]
    await message.answer("📊 <b>Статистика продавца</b>\n\n" + "\n\n".join(lines) + f"\n\nВсего просмотров: <b>{total_views}</b>\nДобавлений в избранное: <b>{total_favs}</b>")


@router.callback_query(F.data.startswith("visit:"))
async def visit_callback(cb: CallbackQuery) -> None:
    ad_id = int(cb.data.split(":", 1)[1])
    ad = await get_ad(ad_id)
    if not ad or ad["status"] != "approved":
        await cb.answer("Объявление уже недоступно", show_alert=True)
        return
    await record_event(ad_id, cb.from_user.id, "view")
    await cb.answer("Переход учтён 👁")


@router.callback_query(F.data.startswith("fav:"))
async def favorite_callback(cb: CallbackQuery) -> None:
    ad_id = int(cb.data.split(":",1)[1])
    ad = await get_ad(ad_id)
    if not ad or ad["status"] != "approved":
        await cb.answer("Объявление уже недоступно", show_alert=True); return
    added = await toggle_favorite(cb.from_user.id, ad_id)
    await cb.answer("Добавлено в избранное ⭐" if added else "Удалено из избранного", show_alert=False)


@router.callback_query(F.data.startswith("contact:"))
async def contact_callback(cb: CallbackQuery, bot: Bot) -> None:
    ad_id = int(cb.data.split(":",1)[1])
    ad = await get_ad(ad_id)
    if not ad or ad["status"] != "approved":
        await cb.answer("Объявление уже недоступно", show_alert=True); return
    if cb.from_user.id == ad["user_id"]:
        await cb.answer("Это ваше объявление", show_alert=True); return
    await record_event(ad_id, cb.from_user.id, "contact")
    try:
        await bot.send_message(ad["user_id"], f"💬 Покупатель заинтересовался объявлением <b>#{ad_id} {e(ad['title'])}</b>.\n\nПока доступно только уведомление о запросе контакта; встроенный чат будет отдельным следующим этапом.")
        await cb.answer("Продавец уведомлён. Напишите ему в личном чате с ботом: /reply " + str(ad_id), show_alert=True)
    except TelegramAPIError:
        await cb.answer("Продавец ещё не запускал бота. Попробуйте позже.", show_alert=True)


@router.message(Command("reply"))
async def reply_help(message: Message) -> None:
    await message.answer("💬 Диалоги внутри бота будут доступны после следующего этапа обновления. Сейчас продавец получает уведомление о запросе, но прямой чат ещё не подключён.")


@router.callback_query(F.data.startswith("seller:"))
async def seller_actions(cb: CallbackQuery, bot: Bot) -> None:
    _, action, raw_id = cb.data.split(":")
    ad_id = int(raw_id); ad = await get_ad(ad_id)
    if not ad or ad["user_id"] != cb.from_user.id:
        await cb.answer("Объявление не найдено или нет доступа", show_alert=True); return
    if action not in ("sold", "remove", "republish"):
        await cb.answer("Неизвестное действие", show_alert=True); return
    status = {"sold":"sold", "remove":"removed", "republish":"approved"}[action]
    if not await update_ad_status(ad_id, cb.from_user.id, status):
        await cb.answer("Действие доступно только для опубликованных объявлений", show_alert=True); return
    try:
        if ad.get("channel_message_id"):
            notice = "🏁 ПРОДАНО" if status == "sold" else "🗑 ОБЪЯВЛЕНИЕ СНЯТО С ПУБЛИКАЦИИ"
            await bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=ad["channel_message_id"], caption=f"{notice}\n\n{e(ad['title'])}")
    except TelegramAPIError:
        log.warning("Не удалось обновить пост #%s в канале", ad_id)
    await cb.message.edit_text(f"Объявление #{ad_id}: {STATUS_TEXT[status]}")
    await cb.answer("Статус обновлён")


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
    ads = await get_user_ads(message.from_user.id)
    if not ads:
        await message.answer("У вас пока нет объявлений.")
        return
    lines = []
    for a in ads:
        line = f"#{a['id']} {e(a['title'])} — {STATUS_TEXT[a['status']]}"
        if a["status"] == "rejected" and a["reject_reason"]:
            line += f"\n   Причина: {e(a['reject_reason'])}"
        lines.append(line)
        if a["status"] == "approved":
            kb = InlineKeyboardBuilder()
            kb.button(text="🏁 Продано", callback_data=f"seller:sold:{a['id']}")
            kb.button(text="🗑 Снять", callback_data=f"seller:remove:{a['id']}")
            kb.adjust(2)
            await message.answer(f"📦 <b>#{a['id']} {e(a['title'])}</b>\n{STATUS_TEXT[a['status']]}\n👁 переходы: {a.get('views',0)} · ⭐ {a.get('favorites_count',0)}", reply_markup=kb.as_markup())
    await message.answer("\n".join(lines))


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
        sent = await send_ad(bot, CHANNEL_ID, ad)
        if sent:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE ads SET channel_message_id=? WHERE id=?", (sent.message_id, ad_id))
                await db.commit()
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
        bot, ad["user_id"], f"🎉 Ваше объявление «{e(ad['title'])}» опубликовано! Теперь им можно управлять через /my, статистика — /stats."
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
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
