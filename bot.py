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
_channel = os.getenv("CHANNEL_ID", "@Sz_tradetech").strip()
CHANNEL_ID = int(_channel) if _channel.lstrip("-").isdigit() else _channel
# Публичная ссылка для пользователей. Для Telegram API CHANNEL_ID по-прежнему нужен как @username или числовой ID.
CHANNEL_URL = "https://t.me/Sz_tradetech"

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
    category = State()
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


def category_field_prompt(key: str, category: str) -> str:
    """Возвращает пример, соответствующий выбранной категории."""
    examples = {
        "smartphones": {
            "title": "📱 Какой смартфон продаёте? Укажите бренд, модель и версию.\n\nНапример: <i>Vivo X200s 12/512 ГБ, нежно-зелёный</i>",
            "specs": "⚙️ Характеристики смартфона — одним сообщением. Например:\n\n<i>Память: 512 ГБ\nОЗУ: 12 ГБ\nЭкран: AMOLED, 120 Гц\nПроцессор: Dimensity 9400+\nСостояние: отличное\nКомплект: коробка, кабель</i>",
        },
        "tablets": {
            "title": "📲 Какой планшет продаёте? Укажите бренд, модель и память.\n\nНапример: <i>iPad Air 11 128 ГБ Wi‑Fi, серый</i>",
            "specs": "⚙️ Характеристики планшета. Например:\n\n<i>Диагональ: 11 дюймов\nПамять: 128 ГБ\nПодключение: Wi‑Fi\nЭкран: Liquid Retina\nКомплект: планшет, зарядка, чехол</i>",
        },
        "laptops": {
            "title": "💻 Что продаёте? Укажите бренд и точную модель ноутбука или комплектующих.\n\nНапример: <i>Lenovo IdeaPad 5 15ITL05</i>",
            "specs": "⚙️ Характеристики ноутбука/ПК. Например:\n\n<i>Процессор: Intel Core i5-1135G7\nОЗУ: 16 ГБ DDR4\nНакопитель: SSD 512 ГБ\nВидеокарта: Intel Iris Xe\nЭкран: 15,6 дюйма, Full HD\nОС: Windows 11</i>",
        },
        "wearables": {
            "title": "⌚ Какие смарт-часы или браслет продаёте?\n\nНапример: <i>Huawei Watch GT 5 46 мм, чёрные</i>",
            "specs": "⚙️ Характеристики часов. Например:\n\n<i>Размер корпуса: 46 мм\nМатериал: нержавеющая сталь\nЭкран: AMOLED\nСовместимость: Android/iOS\nАвтономность: до 10 дней\nКомплект: часы, ремешок, зарядка</i>",
        },
        "audio": {
            "title": "🎧 Какие наушники или аудиоустройство продаёте?\n\nНапример: <i>Sony WH-1000XM5, чёрные</i>",
            "specs": "⚙️ Характеристики аудиоустройства. Например:\n\n<i>Тип: полноразмерные, беспроводные\nПодключение: Bluetooth 5.2\nШумоподавление: есть\nАвтономность: до 30 часов\nКомплект: наушники, кейс/кабель</i>",
        },
        "gaming": {
            "title": "🎮 Что из игровой техники продаёте?\n\nНапример: <i>Sony PlayStation 5 Slim 1 ТБ</i>",
            "specs": "⚙️ Характеристики игровой техники. Например:\n\n<i>Модель: PlayStation 5 Slim\nПамять: 1 ТБ SSD\nРевизия: указать при наличии\nГеймпады: 1 DualSense\nКомплект: консоль, кабели, коробка</i>",
        },
        "accessories": {
            "title": "🔌 Какой аксессуар продаёте? Укажите бренд и модель.\n\nНапример: <i>Anker Nano MagGo 10000 мА·ч</i>",
            "specs": "⚙️ Характеристики аксессуара. Например:\n\n<i>Тип: внешний аккумулятор\nЁмкость: 10 000 мА·ч\nМощность: до 30 Вт\nРазъёмы: USB‑C\nСовместимость: указать устройства\nКомплект: устройство, кабель</i>",
        },
        "other": {
            "title": "📦 Что именно продаёте? Укажите название и модель, если есть.\n\nНапример: <i>Умная колонка Яндекс Станция Мини 2</i>",
        },
    }
    fallback = {
        "title": "📦 Что именно продаёте? Укажите название и модель, если есть.",
        "specs": "⚙️ Укажите основные характеристики товара: модель, состояние, комплектацию и важные параметры.",
    }
    return examples.get(category, {}).get(key, fallback.get(key, ""))

CATEGORIES = {
    "smartphones": "📱 Смартфоны",
    "tablets": "📲 Планшеты",
    "laptops": "💻 Ноутбуки и ПК",
    "wearables": "⌚ Смарт-часы",
    "audio": "🎧 Наушники и аудио",
    "gaming": "🎮 Игровая техника",
    "accessories": "🔌 Аксессуары",
    "other": "📦 Другое",
}

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
        for name, sql_type in (("channel_message_id", "INTEGER"), ("views", "INTEGER NOT NULL DEFAULT 0"), ("favorites_count", "INTEGER NOT NULL DEFAULT 0"), ("updated_at", "TEXT"), ("category", "TEXT NOT NULL DEFAULT 'other'")) :
            if name not in columns:
                await db.execute(f"ALTER TABLE ads ADD COLUMN {name} {sql_type}")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS safety_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ad_id, reporter_id)
            );
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
                             defects, city, price, photos, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ad.get("category", "other"),
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


async def create_safety_report(ad_id: int, reporter_id: int, reason: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "INSERT INTO safety_reports(ad_id, reporter_id, reason) VALUES(?,?,?)",
                (ad_id, reporter_id, reason[:500]),
            )
            await db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None


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


async def get_public_ads(limit: int = 10, offset: int = 0, category: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if category and category in CATEGORIES:
            cur = await db.execute("SELECT * FROM ads WHERE status='approved' AND category=? ORDER BY id DESC LIMIT ? OFFSET ?", (category, limit, offset))
        else:
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
        f"<b>{e(ad['title'])}</b>\n"
        f"🏷 <b>Категория:</b> {e(CATEGORIES.get(ad.get('category', 'other'), CATEGORIES['other']))}\n\n"
        f"💰 <b>Цена:</b> {e(ad['price'])}\n"
        f"📍 <b>Город:</b> {e(ad['city'])}\n\n"
        f"⚙️ <b>Характеристики:</b>\n{e(ad['specs'])}\n\n"
        f"📝 <b>Описание:</b>\n{e(ad['description'])}\n\n"
        f"⚠️ <b>Недочёты:</b>\n{e(ad['defects'])}\n\n"
        f"💬 <b>Связь:</b> {contact}\n\n"
        "🛡 <i>Проверяйте товар лично. Не переводите предоплату незнакомцам и не сообщайте коды из SMS.</i>\n\n"
        f"📢 <a href=\"{CHANNEL_URL}\">Канал Trade Bish — все объявления</a>"
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
        kb.button(text="🚨 Пожаловаться", callback_data=f"report:{ad['id']}")
        kb.adjust(1, 1, 1, 1)
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
        "category": data.get("category", "other"),
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
        "👋 Добро пожаловать в Trade Bish — площадку для покупки, продажи и обмена техники.\n\n"
        "Здесь можно найти смартфоны, ноутбуки, планшеты, аксессуары и другую электронику, сохранить объявления и безопаснее связаться с продавцом.\n\n"
        "Публикации проходят модерацию. Trade Bish не принимает оплату и не выступает гарантом сделки — внимательно проверяйте товар и не переводите предоплату незнакомцам.\n\n"
        f"📢 Наш канал: <a href=\"{CHANNEL_URL}\">Trade Bish</a>",
        reply_markup=menu_kb,
    )


def category_keyboard(prefix: str = "catalog"):
    kb = InlineKeyboardBuilder()
    kb.button(text="Все категории", callback_data=f"{prefix}:all")
    for key, label in CATEGORIES.items():
        kb.button(text=label, callback_data=f"{prefix}:{key}")
    kb.adjust(2)
    return kb.as_markup()


async def show_catalog(target, category: str | None = None) -> None:
    ads = await get_public_ads(category=category)
    title = "🛍 <b>Каталог Trade Bish</b>"
    if category:
        title += f"\nКатегория: {e(CATEGORIES.get(category, 'Другое'))}"
    await target.answer(title)
    if not ads:
        await target.answer("В этой категории пока нет опубликованных объявлений.")
        return
    for ad in ads:
        await send_ad(target.bot, target.chat.id, ad)


@router.message(Command("catalog"))
@router.message(F.text == "🛍 Каталог")
async def cmd_catalog(message: Message) -> None:
    await message.answer("Выберите категорию техники для просмотра:", reply_markup=category_keyboard())


@router.callback_query(F.data.startswith("catalog:"))
async def catalog_category(cb: CallbackQuery) -> None:
    value = cb.data.split(":", 1)[1]
    await cb.answer()
    await show_catalog(cb.message, None if value == "all" else value)


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


@router.message(Command("sta