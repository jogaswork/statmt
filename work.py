import json
import sys
# profits imports this module for accounting even when launched as `python work.py`.
if __name__ == "__main__":
    sys.modules["work"] = sys.modules[__name__]
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {8567015903, 8422968319}

DATA_FILE = Path("/data/top_data.json") if Path("/data").is_dir() else Path("top_data.json")
CARD_FILE = Path("/data/card_data.json") if Path("/data").is_dir() else Path("card_data.json")

UPDATE_INTERVAL_SECONDS = 3600  # 1 раз в час

# Historical standings recovered from the user's screenshot (total: 417,799).
# Keep internal @ keys for profit accounting; display # in /top.
RECOVERY_SNAPSHOT = {
    "@manzi_nx": 333476,
    "@hristi14": 30485,
    "@mr_what_work": 20250,
    "@Шин": 9900,
    "@Flipa222": 7035,
    "@woonsm": 6650,
    "@milanochkaaaaaaas": 5500,
    "@europawin": 2500,
    "@jogas_wor": 1488,
    "@josefphg": 515,
}
RESTORE_MARKER = DATA_FILE.parent / ".snapshot_417799_restored"


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
TOTAL_EMOJI_ID = "5870921681735781843"

# Мануал
MANUAL_EMOJI_ID = "5379573591962563018"
MANUAL_LINK = "https://t.me/+jGW2DBHkqAwwYzgx"

# ---------------------------------------------------------------------------
# Работа с данными
# ---------------------------------------------------------------------------
def _read_top_file(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        if isinstance(value, dict):
            return {str(k): int(v) for k, v in value.items()}
        raise ValueError("Top data must be an object")
    except (OSError, ValueError, TypeError):
        logger.exception("Unable to read top file: %s", path)
        if path.exists():
            raise ValueError("Existing top data is unreadable; refusing to overwrite it")
    return {}


def load_data() -> dict:
    recover_profit_transaction()
    # Never overwrite existing volume data during migration.
    if DATA_FILE.exists():
        return _read_top_file(DATA_FILE)
    legacy = Path("top_data.json")
    if legacy != DATA_FILE and legacy.exists():
        return _read_top_file(legacy)
    return {}


def save_data(data: dict, *, _recover=True) -> None:
    if _recover:
        recover_profit_transaction()
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep a backup of existing data before any change.
    if DATA_FILE.exists():
        import shutil
        shutil.copy2(DATA_FILE, DATA_FILE.with_suffix(".json.bak"))
    import tempfile
    fd, temp = tempfile.mkstemp(prefix="top-", suffix=".tmp", dir=str(DATA_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp, DATA_FILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def recover_profit_transaction():
    """Replay absolute snapshots, so a crash between file writes cannot accrue twice."""
    journal = DATA_FILE.parent / "profit_transaction.json"
    if not journal.exists():
        return
    transaction = json.loads(journal.read_text(encoding="utf8"))
    from profits import save
    save_data(transaction["top"], _recover=False)
    save(transaction["profits"])
    journal.unlink()


def commit_profit_data(profits, top):
    recover_profit_transaction()
    journal = DATA_FILE.parent / "profit_transaction.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    temp = journal.with_suffix(".tmp")
    temp.write_text(json.dumps({"profits": profits, "top": top}, ensure_ascii=False), encoding="utf8")
    os.replace(temp, journal)
    recover_profit_transaction()


def restore_historical_snapshot_once() -> None:
    """Merge older screenshot standings, preserve new amounts, and never add twice."""
    if RESTORE_MARKER.exists():
        return
    assert sum(RECOVERY_SNAPSHOT.values()) == 417799
    existing = load_data()
    # Do not replace a populated modern top that already exceeds the historic snapshot.
    if sum(existing.values()) >= 417799:
        RESTORE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        RESTORE_MARKER.write_text("Existing historical total preserved.\n", encoding="utf8")
        logger.info("Historical restore skipped: current total already >= 417799.")
        return
    merged = dict(existing)
    added = 0
    updated = 0
    for name, old_amount in RECOVERY_SNAPSHOT.items():
        # Matching ignores @ vs # and case to prevent duplicate workers.
        matching = next((key for key in merged if key.lstrip("@#").casefold() == name.lstrip("@#").casefold()), None)
        if matching is None:
            merged[name] = old_amount
            added += 1
        elif merged[matching] < old_amount:
            merged[matching] = old_amount
            updated += 1
    if merged != existing:
        save_data(merged)
    RESTORE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    RESTORE_MARKER.write_text("Restored snapshot from user screenshot; original existing sums preserved.\n", encoding="utf-8")
    logger.warning("Historical snapshot restored: %d added, %d raised, new total %d.", added, updated, sum(merged.values()))


async def cmd_top_recovery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    candidates = [DATA_FILE, Path("top_data.json"), Path("/data/top_data.json.bak")]
    seen = set()
    lines = ["🔎 Проверка файлов топа:"]
    for path in candidates:
        if str(path) in seen:
            continue
        seen.add(str(path))
        exists = path.exists()
        values = _read_top_file(path) if exists else {}
        lines.append(f"{path}: {'есть' if exists else 'нет'}, записей: {len(values)}, сумма: {sum(values.values()):,}")
    lines.append("Никакие данные не изменены.")
    await update.message.reply_text("\n".join(lines))


async def cmd_top_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    # Restore only into an empty destination, never overwrite current standings.
    if load_data():
        await update.message.reply_text("⚠️ Текущий топ не пустой. Автоматическое восстановление заблокировано.")
        return
    for source in (Path("top_data.json"), Path("/data/top_data.json.bak")):
        if source == DATA_FILE:
            continue
        values = _read_top_file(source) if source.exists() else {}
        if values:
            save_data(values)
            await update.message.reply_text(f"✅ Восстановлено {len(values)} записей. Общая сумма: {sum(values.values()):,}")
            return
    await update.message.reply_text("❌ Старый файл или резервная копия не найдены в доступных путях. Ничего не перезаписано.")


def load_card() -> str:
    if CARD_FILE.exists():
        with open(CARD_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("text", "")
    return ""


def save_card(text: str) -> None:
    CARD_FILE.parent.mkdir(parents=True, exist_ok=True)
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
            text += f" #{username.lstrip('@#')} — {format_amount_number(amount)} "
            text = _append_custom_emoji(text, entities, AMOUNT_EMOJI_ID)
            text += "\n"

        total = sum(data.values())
        text += "\n"
        text = _append_custom_emoji(text, entities, TOTAL_EMOJI_ID)
        text += f" Общая сумма: {format_amount_number(total)} "
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
        [InlineKeyboardButton("🚀 Управление профитами", callback_data="admin_profits")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return

    context.user_data.pop("awaiting", None)
    context.user_data.pop("profit_flow", None)
    await update.message.reply_text("⚙️ Админ-панель", reply_markup=admin_main_keyboard())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("Доступ запрещён", show_alert=True)
        return

    await query.answer()
    data = query.data
    context.user_data.pop("profit_flow", None)

    if data == "admin_profits":
        context.user_data.pop("awaiting", None)
        from profits import menu
        await query.edit_message_text("📊 MTR STATS · Профиты", reply_markup=menu())
        return

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
    if context.user_data.get("profit_flow"):
        return
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
    restore_historical_snapshot_once()
    from profit_admin import sync_history
    application = Application.builder().token(BOT_TOKEN).request(request).post_init(sync_history).build()

    from profits import register as register_profits
    register_profits(application, ADMIN_IDS)
    application.add_handler(CommandHandler("top_recovery", cmd_top_recovery))
    application.add_handler(CommandHandler("top_restore", cmd_top_restore))
    application.add_handler(CommandHandler("top", cmd_top))
    application.add_handler(CommandHandler("card", cmd_card))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(admin_|remove_|reset_).*"))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^/?(топ|top)$"), cmd_top))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^/?(реквизиты( карты)?|карты|карта|рекв(ы|и)?)$"), cmd_card))
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


if __name__ == "__main__":
    main()


