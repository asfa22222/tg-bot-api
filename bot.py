"""Telegram bot for Devin AI session management."""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
import devin_api
import kiro_api
from config import TELEGRAM_BOT_TOKEN, KIRO_API_KEY

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _check_whitelist(update: Update) -> bool:
    """Return True if user is whitelisted. Send denial message if not."""
    user = update.effective_user
    if user is None:
        return False

    # First user ever becomes admin automatically
    if await db.get_whitelist_count() == 0:
        await db.add_to_whitelist(user.id, user.username, is_admin=True)
        await update.message.send_message(
            chat_id=update.effective_chat.id,
            text=f"Вы первый пользователь — автоматически стали админом.\n"
            f"Ваш Telegram ID: `{user.id}`",
            parse_mode="Markdown",
        )
        return True

    if not await db.is_whitelisted(user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return False
    return True


async def _check_admin(update: Update) -> bool:
    """Return True if user is admin."""
    if not await _check_whitelist(update):
        return False
    user = update.effective_user
    if not await db.is_admin(user.id):
        await update.message.reply_text("⛔ Эта команда только для админов.")
        return False
    return True


def _mask_key(key: str) -> str:
    """Show first 8 and last 4 characters of an API key."""
    if len(key) <= 16:
        return key[:4] + "..." + key[-4:]
    return key[:8] + "..." + key[-4:]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    await update.message.reply_text(
        "🤖 *Devin Telegram Bot*\n\n"
        "Управление сессиями Devin AI прямо из Telegram.\n\n"
        "*Сессии:*\n"
        "/newsession `<задача>` — создать новую сессию\n"
        "/session — текущая активная сессия\n"
        "/status — статус текущей сессии\n"
        "/sessions — список последних сессий\n"
        "Любое текстовое сообщение → отправляется в текущую сессию\n\n"
        "*API ключи (админ):*\n"
        "/addkey `<ключ>` `[название]` — добавить ключ\n"
        "/keys — список ключей\n"
        "/removekey `<id>` — удалить ключ\n"
        "/switchkey — переключить на следующий ключ\n\n"
        "*Вайтлист (админ):*\n"
        "/adduser `<tg_id>` — добавить пользователя\n"
        "/removeuser `<tg_id>` — удалить пользователя\n"
        "/users — список пользователей\n"
        "/myid — показать ваш Telegram ID\n\n"
        "*Kiro (админ):*\n"
        "/kiro `<запрос>` — отправить запрос в Kiro AI",
        parse_mode="Markdown",
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(f"Ваш Telegram ID: `{user.id}`", parse_mode="Markdown")


# --- Session commands ---

async def cmd_newsession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /newsession <описание задачи>\n"
            "Пример: /newsession Исправь баг в файле main.py"
        )
        return

    prompt = " ".join(context.args)
    await update.message.reply_text("⏳ Создаю сессию Devin...")

    try:
        result, key_id = await devin_api.create_session(prompt)
    except devin_api.NoAPIKeysError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except devin_api.DevinAPIError as e:
        await update.message.reply_text(f"❌ Ошибка API: {e.detail}")
        return

    session_id = result["session_id"]
    session_url = result["url"]

    await db.create_session_record(
        devin_session_id=session_id,
        devin_url=session_url,
        tg_user_id=update.effective_user.id,
        title=prompt[:100],
        api_key_id=key_id,
    )

    await update.message.reply_text(
        f"✅ Сессия создана!\n\n"
        f"🔗 {session_url}\n"
        f"📝 {prompt[:100]}\n\n"
        f"Теперь можете отправлять сообщения — они пойдут в эту сессию.",
        disable_web_page_preview=True,
    )


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    session = await db.get_active_session(update.effective_user.id)
    if not session:
        await update.message.reply_text(
            "Нет активной сессии. Создайте: /newsession <задача>"
        )
        return

    await update.message.reply_text(
        f"📌 *Активная сессия*\n\n"
        f"📝 {session['title'] or 'Без названия'}\n"
        f"🔗 {session['devin_url']}\n"
        f"🕐 Создана: {session['created_at']}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    session = await db.get_active_session(update.effective_user.id)
    if not session:
        await update.message.reply_text("Нет активной сессии.")
        return

    try:
        info = await devin_api.get_session(session["devin_session_id"])
    except devin_api.DevinAPIError as e:
        await update.message.reply_text(f"❌ Ошибка: {e.detail}")
        return

    status = info.get("status_enum", info.get("status", "unknown"))
    await update.message.reply_text(
        f"📊 *Статус сессии*\n\n"
        f"📝 {session['title'] or 'Без названия'}\n"
        f"🔗 {session['devin_url']}\n"
        f"📌 Статус: `{status}`",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    sessions = await db.list_user_sessions(update.effective_user.id)
    if not sessions:
        await update.message.reply_text("Нет сессий. Создайте: /newsession <задача>")
        return

    lines = ["📋 *Последние сессии:*\n"]
    for s in sessions:
        title = s["title"] or "Без названия"
        lines.append(
            f"• [{title}]({s['devin_url']}) — {s['created_at']}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# --- Key management commands ---

async def cmd_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /addkey <api_ключ> [название]\n"
            "Пример: /addkey apk_user_abc123 Аккаунт1"
        )
        return

    key = context.args[0]
    label = " ".join(context.args[1:]) if len(context.args) > 1 else None

    try:
        key_id = await db.add_api_key(key, label)
    except Exception:
        await update.message.reply_text("❌ Этот ключ уже добавлен.")
        return

    # Delete the message with the key for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Ключ добавлен (ID: {key_id}, {_mask_key(key)})\n"
        f"Название: {label or '—'}\n\n"
        f"💡 Сообщение с ключом удалено для безопасности.",
    )


async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    keys = await db.list_api_keys()
    if not keys:
        await update.message.reply_text(
            "Нет API ключей. Добавьте: /addkey <ключ> [название]"
        )
        return

    current_idx = await db.get_current_key_index()
    active_keys = [k for k in keys if k["is_active"]]
    current_key_id = None
    if active_keys:
        real_idx = current_idx % len(active_keys)
        current_key_id = active_keys[real_idx]["id"]

    lines = ["🔑 *API ключи:*\n"]
    for k in keys:
        marker = "➡️ " if k["id"] == current_key_id else ""
        status = "✅" if k["is_active"] else "❌"
        label = k["label"] or "—"
        lines.append(
            f"{marker}ID `{k['id']}` | {status} | `{_mask_key(k['key'])}` | {label}"
        )

    lines.append("\n➡️ = текущий активный ключ")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_removekey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /removekey <id>")
        return

    try:
        key_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    if await db.remove_api_key(key_id):
        await update.message.reply_text(f"✅ Ключ ID {key_id} удалён.")
    else:
        await update.message.reply_text(f"❌ Ключ ID {key_id} не найден.")


async def cmd_switchkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    keys = await db.get_active_keys()
    if len(keys) < 2:
        await update.message.reply_text("Нужно минимум 2 активных ключа для переключения.")
        return

    idx = await db.get_current_key_index()
    new_idx = (idx + 1) % len(keys)
    await db.set_current_key_index(new_idx)

    new_key = keys[new_idx]
    await update.message.reply_text(
        f"🔄 Переключено на ключ ID {new_key['id']} (`{_mask_key(new_key['key'])}`)",
        parse_mode="Markdown",
    )


# --- Whitelist commands ---

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /adduser <telegram_id> [admin]\n"
            "Пример: /adduser 123456789\n"
            "Пример: /adduser 123456789 admin"
        )
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID должен быть числом.")
        return

    is_admin = len(context.args) > 1 and context.args[1].lower() == "admin"
    await db.add_to_whitelist(tg_id, is_admin=is_admin)

    role = "админ" if is_admin else "пользователь"
    await update.message.reply_text(f"✅ Добавлен {role}: `{tg_id}`", parse_mode="Markdown")


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /removeuser <telegram_id>")
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID должен быть числом.")
        return

    if tg_id == update.effective_user.id:
        await update.message.reply_text("❌ Нельзя удалить самого себя.")
        return

    if await db.remove_from_whitelist(tg_id):
        await update.message.reply_text(f"✅ Пользователь `{tg_id}` удалён.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Пользователь `{tg_id}` не найден.", parse_mode="Markdown")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    users = await db.list_whitelist()
    if not users:
        await update.message.reply_text("Вайтлист пуст.")
        return

    lines = ["👥 *Вайтлист:*\n"]
    for u in users:
        role = "👑 админ" if u["is_admin"] else "👤 юзер"
        username = f"@{u['username']}" if u["username"] else "—"
        lines.append(f"• `{u['tg_id']}` | {username} | {role}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Kiro command (admin only) ---

async def cmd_kiro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /kiro <запрос>\n"
            "Пример: /kiro Напиши функцию сортировки на Python"
        )
        return

    if not KIRO_API_KEY:
        await update.message.reply_text(
            "KIRO_API_KEY не настроен. Добавьте переменную окружения."
        )
        return

    prompt = " ".join(context.args)
    await update.message.reply_text("Kiro думает...")

    try:
        result = await kiro_api.send_prompt(prompt)
    except kiro_api.KiroNotConfiguredError as e:
        await update.message.reply_text(f"KIRO_API_KEY not configured: {e}")
        return
    except kiro_api.KiroError as e:
        await update.message.reply_text(f"Kiro error: {e}")
        return

    # Telegram limits messages to 4096 chars
    if len(result) > 4096:
        for i in range(0, len(result), 4096):
            await update.message.reply_text(result[i:i + 4096])
    else:
        await update.message.reply_text(result)


# --- Text message handler (send to active session) ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    text = update.message.text
    if not text:
        return

    session = await db.get_active_session(update.effective_user.id)
    if not session:
        await update.message.reply_text(
            "Нет активной сессии. Создайте: /newsession <задача>"
        )
        return

    try:
        await devin_api.send_message(session["devin_session_id"], text)
    except devin_api.DevinAPIError as e:
        await update.message.reply_text(f"❌ Ошибка: {e.detail}")
        return

    await update.message.reply_text(
        f"📨 Сообщение отправлено в сессию.\n🔗 {session['devin_url']}",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    await db.get_db()
    logger.info("Database initialized")


async def post_shutdown(application: Application) -> None:
    await db.close_db()
    logger.info("Database closed")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # Session commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("newsession", cmd_newsession))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sessions", cmd_sessions))

    # Key management
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("removekey", cmd_removekey))
    app.add_handler(CommandHandler("switchkey", cmd_switchkey))

    # Whitelist management
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))

    # Kiro integration (admin only)
    app.add_handler(CommandHandler("kiro", cmd_kiro))

    # Text messages → active session
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
