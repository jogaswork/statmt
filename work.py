import json
import logging
from telegram.ext import MessageHandler, filters
from telegram.request import HTTPXRequest
from pathlib import Path
import os
import psycopg2
import logging

# Читаем DATABASE_URL, которую мы привязали в Railway
DB_URL = os.getenv("DATABASE_URL")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
BOT_TOKEN = "8830622827:AAGr--WXYjGQ0Y-bcfvMYrljFR2ZCPd_N6Y"
ADMIN_IDS = {8567015903, 8422968319}

DATA_FILE = Path("top_data.json")
CARD_FILE = Path("card_data.json")

UPDATE_INTERVAL_SECONDS = 3600  # 1 раз в час

# Placeholder-символ, поверх которого Telegram отрисует премиум-эмодзи.
# Виден клиентам любых пользователей, отправлять премиум-эмодзи может
# только сам бот (это не требует Premium у получателя).
PLACEHOLDER = "⭐"

AMOUNT_EMOJI_ID = "5350726727586840774"
TITLE_EMOJI_ID = "5429194899616474448"
RANK_EMOJI_IDS = {
    1: "5440539497383087970",
    2: "5447203607294265305",
    3: "5453902265922376865",
}
RANK_EMOJI_DEFAULT_ID = "5444986266003194641"  # места с 4-го и далее
CARD_EMOJI_ID = "5472135042044011718"

# Мануал
MANUAL_EMOJI_ID = "5379573591962563018"
MANUAL_LINK = "https://t.me/+jGW2DBHkqAwwYzgx"

# ---------------------------------------------------------------------------
# Работа с данными
# ---------------------------------------------------------------------------
def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_card() -> str:
    if CARD_FILE.exists():
        with open(CARD_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("text", "")
    return ""


def save_card(text: str) -> None:
    with open(CARD_FILE, "w", encoding="utf-8") as f:
        json.dump({"text": text}, f, ensure_ascii=False, indent=2)


def format_amount_number(amount: int) -> str:
    """100000 -> '100 000'"""
    return f"{amount:,}".replace(",", " ")


def utf16_len(s: str) -> int:
    """Длина строки в UTF-16 code units (нужна для смещений MessageEntity)."""
    return len(s.encode("utf-16-le")) // 2


def _append_custom_emoji(text: str, entities: list[MessageEntity], emoji_id: str) -> str:
    """Добавляет в text плейсхолдер под премиум-эмодзи и создаёт под него entity."""
    offset = utf16_len(text)
    text += PLACEHOLDER
    length = utf16_len(PLACEHOLDER)
    entities.append(
        MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=offset,
            length=length,
            custom_emoji_id=emoji_id,
        )
    )
    return text


def build_top_message(data: dict) -> tuple[str, list[MessageEntity]]:
    entities: list[MessageEntity] = []
    text = _append_custom_emoji("", entities, TITLE_EMOJI_ID)
    text += " ТОП ВОРКЕРОВ\n\n"

    if not data:
        text += "Топ пока пуст."
    else:
        sorted_items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
        for i, (username, amount) in enumerate(sorted_items, start=1):
            rank_emoji_id = RANK_EMOJI_IDS.get(i, RANK_EMOJI_DEFAULT_ID)
            text = _append_custom_emoji(text, entities, rank_emoji_id)
            text += f" {username} — {format_amount_number(amount)} "
            text = _append_custom_emoji(text, entities, AMOUNT_EMOJI_ID)
            text += "\n"

    text += "\n⏰ Обновление раз в час!"
    return text, entities


def build_card_message() -> tuple[str, list[MessageEntity]]:
    entities: list[MessageEntity] = []
    text = _append_custom_emoji("", entities, CARD_EMOJI_ID)
    text += " Реквизиты для оплаты:\n\n"

    card_text = load_card()
    text += card_text if card_text else "Реквизиты пока не установлены."
    return text, entities


def build_manual_message() -> tuple[str, list[MessageEntity]]:
    entities: list[MessageEntity] = []
    text = _append_custom_emoji("", entities, MANUAL_EMOJI_ID)
    text += " Мануал"
    return text, entities


def manual_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("Читать", url=MANUAL_LINK)]]
    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# /top
# ---------------------------------------------------------------------------
async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    text, entities = build_top_message(data)
    await update.message.reply_text(text, entities=entities)


async def cmd_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, entities = build_card_message()
    await update.message.reply_text(text, entities=entities)


async def cmd_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, entities = build_manual_message()
    await update.message.reply_text(
        text, entities=entities, reply_markup=manual_keyboard()
    )


# ---------------------------------------------------------------------------
# /admin — inline-панель администрирования
# ---------------------------------------------------------------------------
def admin_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Добавить в топ", callback_data="admin_add")],
        [InlineKeyboardButton("🗑 Удалить из топа", callback_data="admin_remove")],
        [InlineKeyboardButton("♻️ Сбросить топ", callback_data="admin_reset")],
        [InlineKeyboardButton("📊 Показать топ", callback_data="admin_show")],
        [InlineKeyboardButton("💳 Изменить реквизиты", callback_data="admin_card")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return

    context.user_data.pop("awaiting", None)
    await update.message.reply_text("⚙️ Админ-панель", reply_markup=admin_main_keyboard())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("Доступ запрещён", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "admin_add":
        context.user_data["awaiting"] = "add"
        await query.edit_message_text(
            "✏️ Отправьте сообщением никнейм и сумму через пробел.\n\n"
            "Пример:\n@username 50000"
        )
        return

    if data == "admin_remove":
        top_data = load_data()
        if not top_data:
            await query.edit_message_text(
                "Топ пуст, удалять нечего.", reply_markup=admin_main_keyboard()
            )
            return

        keyboard = [
            [InlineKeyboardButton(f"🗑 {username}", callback_data=f"remove_{username}")]
            for username in top_data
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")])
        await query.edit_message_text(
            "Кого удалить?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("remove_"):
        username = data[len("remove_") :]
        top_data = load_data()
        if username in top_data:
            del top_data[username]
            save_data(top_data)
            await query.edit_message_text(
                f"✅ {username} удалён из топа.", reply_markup=admin_main_keyboard()
            )
        else:
            await query.edit_message_text(
                "Не найдено.", reply_markup=admin_main_keyboard()
            )
        return

    if data == "admin_reset":
        keyboard = [
            [InlineKeyboardButton("✅ Да, сбросить", callback_data="reset_yes")],
            [InlineKeyboardButton("❌ Отмена", callback_data="admin_back")],
        ]
        await query.edit_message_text(
            "Точно сбросить весь топ?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "reset_yes":
        save_data({})
        await query.edit_message_text(
            "♻️ Топ полностью сброшен.", reply_markup=admin_main_keyboard()
        )
        return

    if data == "admin_show":
        top_data = load_data()
        text, entities = build_top_message(top_data)
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")]]
        await query.edit_message_text(
            text, entities=entities, reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "admin_card":
        context.user_data["awaiting"] = "card"
        current = load_card()
        current_block = f"\n\nТекущие реквизиты:\n{current}" if current else ""
        await query.edit_message_text(
            "✏️ Отправьте сообщением новый текст реквизитов "
            "(можно с переносами строк)." + current_block
        )
        return

    if data == "admin_back":
        context.user_data.pop("awaiting", None)
        await query.edit_message_text("⚙️ Админ-панель", reply_markup=admin_main_keyboard())
        return


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит текстовый ввод после нажатия кнопок в админ-панели."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return

    awaiting = context.user_data.get("awaiting")
    if awaiting is None:
        return

    if awaiting == "card":
        card_text = update.message.text.strip()
        save_card(card_text)
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            "✅ Реквизиты обновлены.", reply_markup=admin_main_keyboard()
        )
        return

    if awaiting != "add":
        return

    text = update.message.text.strip()
    parts = text.split()
    if len(parts) != 2:
        await update.message.reply_text(
            "Неверный формат. Пример:\n@username 50000"
        )
        return

    username, amount_raw = parts
    amount_raw = amount_raw.replace(" ", "")
    if not amount_raw.lstrip("-").isdigit():
        await update.message.reply_text(
            "Сумма должна быть числом. Пример:\n@username 50000"
        )
        return

    amount = int(amount_raw)
    if not username.startswith("@"):
        username = "@" + username

    top_data = load_data()
    top_data[username] = amount
    save_data(top_data)

    context.user_data.pop("awaiting", None)
    await update.message.reply_text(
        f"✅ Данные для {username} обновлены.", reply_markup=admin_main_keyboard()
    )


# ---------------------------------------------------------------------------
# Периодическое "перепроведение" кэша топа
# ---------------------------------------------------------------------------
async def refresh_top_cache(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    logger.info("Автообновление топа выполнено. Записей: %d", len(data))

async def save_user_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Убеждаемся, что сообщение пришло от пользователя
    if not update.effective_user or update.effective_user.is_bot:
        return

    user = update.effective_user

    if DB_URL:
        try:
            conn = psycopg2.connect(DB_URL)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO users (user_id, username) 
                VALUES (%s, %s) 
                ON CONFLICT (user_id) DO NOTHING;
                """,
                (user.id, user.username)
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"DB Error: {e}")

# ---------------------------------------------------------------------------
# Запуск бота
# ---------------------------------------------------------------------------
def main() -> None:
    # Таймауты для проксі (например PythonAnywhere)
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=10.0,
        write_timeout=10.0,
        pool_timeout=10.0,
    )

def main() -> None:
    # Таймауты для прокси (например PythonAnywhere)
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=10.0,
        write_timeout=10.0,
        pool_timeout=10.0,
    )
    application = Application.builder().token(BOT_TOKEN).request(request).build()

    application.add_handler(CommandHandler("top", cmd_top))
    application.add_handler(CommandHandler("card", cmd_card))
    application.add_handler(CommandHandler("manual", cmd_manual))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(admin_|remove_|reset_).*"))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^/?мануал$"), cmd_manual))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input))

    job_queue = application.job_queue
    if job_queue is not None:
        job_queue.run_repeating(
            refresh_top_cache,
            interval=UPDATE_INTERVAL_SECONDS,
            first=UPDATE_INTERVAL_SECONDS,
        )
    else:
        logger.warning(
            "JobQueue недоступен. Установите: "
            'pip install "python-telegram-bot[job-queue]" --upgrade'
        )

    logger.info("Бот запущен.")
    application.add_handler(MessageHandler(filters.ALL, save_user_on_message), group=1)
    application.run_polling()
    application.run_polling()


if __name__ == "__main__":
    main()
