"""Telegram bot for Devin AI session management."""

import io
import json
import logging
import os
import re
import tempfile

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
import devin_api
from config import TELEGRAM_BOT_TOKEN

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _check_whitelist(update: Update) -> bool:
    """Return True if user is whitelisted. Send denial message if not."""
    user = update.effective_user
    if user is None:
        return False

    if await db.get_whitelist_count() == 0:
        await db.add_to_whitelist(user.id, user.username, is_admin=True)
        await update.message.reply_text(
            f"Вы первый пользователь — автоматически стали админом.\n"
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
    if len(key) <= 16:
        return key[:4] + "..." + key[-4:]
    return key[:8] + "..." + key[-4:]


STATUS_LABELS = {
    "running": "🟢 Работает",
    "suspended": "⏸ Ожидает ответа",
    "blocked": "⏸ Заблокирована",
    "stopped": "⏹ Остановлена",
    "finished": "✅ Завершена",
    "error": "❌ Ошибка",
}


def _format_status(status: str) -> str:
    return STATUS_LABELS.get(status, f"❓ {status}")


def _reply_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard with main actions."""
    buttons = [
        [KeyboardButton("📝 Новая сессия"), KeyboardButton("📋 Сессии")],
        [KeyboardButton("📌 Текущая"), KeyboardButton("📊 Статус")],
        [KeyboardButton("💰 Расход"), KeyboardButton("📋 Меню")],
    ]
    if is_admin:
        buttons.append(
            [KeyboardButton("🔑 Ключи"), KeyboardButton("📜 Лог")]
        )
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def _main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Build the main inline keyboard menu."""
    buttons = [
        [
            InlineKeyboardButton("📝 Новая сессия", callback_data="menu_newsession"),
            InlineKeyboardButton("📋 Мои сессии", callback_data="menu_sessions"),
        ],
        [
            InlineKeyboardButton("📌 Текущая сессия", callback_data="menu_session"),
            InlineKeyboardButton("📊 Статус", callback_data="menu_status"),
        ],
    ]
    if is_admin:
        buttons.append([
            InlineKeyboardButton("🔑 Ключи", callback_data="menu_keys"),
            InlineKeyboardButton("👥 Вайтлист", callback_data="menu_users"),
        ])
        buttons.append([
            InlineKeyboardButton("💰 Расход", callback_data="menu_cost"),
            InlineKeyboardButton("📜 Лог действий", callback_data="menu_log"),
        ])
    buttons.append([
        InlineKeyboardButton("🆔 Мой ID", callback_data="menu_myid"),
        InlineKeyboardButton("❓ Помощь", callback_data="menu_help"),
    ])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Polling — check Devin sessions for status updates
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _extract_attachment_urls(text: str) -> list[str]:
    """Extract all attachment URLs from a Devin message.

    Handles multiple formats:
      - ATTACHMENT:{"url":"https://...","fileSize":123}
      - ATTACHMENT:"https://..."
      - ![alt](https://...devin.ai/attachments/...)
      - Plain https://...devin.ai/attachments/... URLs
    """
    urls: list[str] = []

    # Format 1: ATTACHMENT:{JSON} — e.g. ATTACHMENT:{"url":"https://..."}
    for m in re.finditer(r'ATTACHMENT:\s*(\{[^}]+\})', text):
        try:
            data = json.loads(m.group(1))
            if "url" in data:
                urls.append(data["url"])
        except (json.JSONDecodeError, KeyError):
            pass

    # Format 2: ATTACHMENT:"url"
    for m in re.finditer(r'ATTACHMENT:\s*"([^"]+)"', text):
        url = m.group(1)
        if url not in urls:
            urls.append(url)

    # Format 3: Markdown images ![alt](url)
    for m in re.finditer(r'!\[.*?\]\((https?://\S+?)\)', text):
        url = m.group(1)
        if url not in urls:
            urls.append(url)

    # Format 4: Plain devin attachment URLs
    for m in re.finditer(r'https://[^\s"}\]]+devin\.ai/attachments/[^\s"}\]]+', text):
        url = m.group(0)
        if url not in urls:
            urls.append(url)

    return urls


async def _send_url_as_file(bot, chat_id: int, url: str, caption: str = "") -> bool:
    """Download a URL and send it as photo or document to Telegram."""
    try:
        file_data = await devin_api.download_file(url)
    except Exception as e:
        logger.warning("Failed to download %s: %s", url, e)
        return False

    if not file_data:
        return False

    # Determine filename from URL
    filename = url.rsplit("/", 1)[-1].split("?")[0] or "file"
    is_image = any(filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)

    # Also check by content (first bytes magic)
    if not is_image and len(file_data) > 4:
        header = file_data[:4]
        if header[:3] == b'\xff\xd8\xff':  # JPEG
            is_image = True
            if not any(filename.lower().endswith(e) for e in IMAGE_EXTENSIONS):
                filename += ".jpg"
        elif header[:4] == b'\x89PNG':  # PNG
            is_image = True
            if not any(filename.lower().endswith(e) for e in IMAGE_EXTENSIONS):
                filename += ".png"

    try:
        bio = io.BytesIO(file_data)
        bio.name = filename

        if is_image:
            await bot.send_photo(
                chat_id=chat_id,
                photo=bio,
                caption=caption[:1024] if caption else None,
            )
        else:
            await bot.send_document(
                chat_id=chat_id,
                document=bio,
                filename=filename,
                caption=caption[:1024] if caption else None,
            )
        return True
    except Exception as e:
        logger.error("Failed to send file to Telegram: %s", e)
        return False


async def _forward_devin_message(bot, chat_id: int, msg: dict) -> None:
    """Forward a single Devin message — text and any embedded attachments."""
    text = msg.get("message", "").strip()
    if not text:
        return

    # Extract all attachment URLs
    attachment_urls = _extract_attachment_urls(text)

    # Clean text: remove all ATTACHMENT:... patterns for cleaner display
    clean_text = re.sub(r'ATTACHMENT:\s*\{[^}]+\}\s*', '', text)
    clean_text = re.sub(r'ATTACHMENT:\s*"[^"]*"\s*', '', clean_text)
    clean_text = clean_text.strip()

    # Send text message if there's meaningful content
    if clean_text:
        if len(clean_text) > 3500:
            clean_text = clean_text[:3500] + "\n\n... _(сообщение обрезано)_"

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🤖 *Devin:*\n\n{clean_text}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🤖 Devin:\n\n{clean_text}",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error("Failed to send text message: %s", e)

    # Download and send each attachment as photo/file
    for url in attachment_urls:
        logger.info("Downloading attachment: %s", url)
        sent = await _send_url_as_file(bot, chat_id, url, caption="📎 От Devin")
        if not sent:
            logger.warning("Failed to send attachment, sending URL instead: %s", url)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📎 Файл от Devin: {url}",
                    disable_web_page_preview=False,
                )
            except Exception:
                pass


async def _poll_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: poll sessions for new messages and status changes."""
    sessions = await db.get_polling_sessions()

    for sess in sessions:
        try:
            info = await devin_api.get_session(sess["devin_session_id"])
        except Exception as e:
            logger.warning("Poll error for session %s: %s", sess["devin_session_id"], e)
            continue

        # --- Forward new Devin messages ---
        messages = info.get("messages", [])
        last_event_id = sess["last_event_id"] or ""
        new_msgs = []
        found_last = not last_event_id

        for msg in messages:
            if found_last:
                msg_type = msg.get("type", "")
                if msg_type != "user_message":
                    new_msgs.append(msg)
            elif msg.get("event_id") == last_event_id:
                found_last = True

        if new_msgs:
            latest_event_id = messages[-1].get("event_id", last_event_id)
            await db.update_session_last_event_id(sess["id"], latest_event_id)

            for msg in new_msgs:
                await _forward_devin_message(
                    context.bot, sess["tg_chat_id"], msg
                )
        elif not last_event_id and messages:
            await db.update_session_last_event_id(
                sess["id"], messages[-1].get("event_id", "")
            )

        # --- Status change notifications ---
        current_status = info.get("status_enum", info.get("status", "unknown"))
        last_status = sess["last_status"] or ""

        if current_status and current_status != last_status:
            await db.update_session_last_status(sess["id"], current_status)
            await db.update_session_status(sess["id"], current_status)

            title = sess["title"] or "Без названия"
            text = (
                f"🔔 *Статус сессии изменился*\n\n"
                f"📝 {title}\n"
                f"📌 {_format_status(last_status)} → {_format_status(current_status)}\n"
                f"🔗 {sess['devin_url']}"
            )

            try:
                await context.bot.send_message(
                    chat_id=sess["tg_chat_id"],
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error("Failed to send status update: %s", e)

            if current_status in ("finished", "stopped", "error", "expired"):
                await db.stop_polling_session(sess["id"])


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    user = update.effective_user
    is_user_admin = await db.is_admin(user.id)
    reply_kb = _reply_keyboard(is_user_admin)

    await update.message.reply_text(
        "🤖 *Devin Telegram Bot*\n\n"
        "Управление сессиями Devin AI прямо из Telegram.\n"
        "Используйте кнопки внизу экрана!",
        parse_mode="Markdown",
        reply_markup=reply_kb,
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Ваш Telegram ID: `{user.id}`", parse_mode="Markdown"
    )


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
    msg = await update.message.reply_text("⏳ Создаю сессию Devin...")

    try:
        result, key_id = await devin_api.create_session(prompt)
    except devin_api.NoAPIKeysError as e:
        await msg.edit_text(f"❌ {e}")
        return
    except devin_api.DevinAPIError as e:
        await msg.edit_text(f"❌ Ошибка API: {e.detail}")
        return

    session_id = result["session_id"]
    session_url = result["url"]

    await db.create_session_record(
        devin_session_id=session_id,
        devin_url=session_url,
        tg_user_id=update.effective_user.id,
        tg_chat_id=update.effective_chat.id,
        title=prompt[:100],
        api_key_id=key_id,
    )

    user = update.effective_user
    await db.record_usage(key_id, user.id, "create_session", session_id)
    await db.log_activity(user.id, "create_session", prompt[:80], user.username)

    await msg.edit_text(
        f"✅ *Сессия создана!*\n\n"
        f"🔗 {session_url}\n"
        f"📝 {prompt[:100]}\n\n"
        f"Теперь можете отправлять сообщения и файлы — они пойдут в эту сессию.\n"
        f"Бот уведомит вас об изменениях статуса.",
        parse_mode="Markdown",
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
        f"📌 Статус: {_format_status(session['status'])}\n"
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
        f"📌 Статус: {_format_status(status)}",
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

    active = await db.get_active_session(update.effective_user.id)
    active_id = active["id"] if active else None

    lines = ["📋 *Все сессии:*\n"]
    for s in sessions:
        title = s["title"] or "Без названия"
        status_icon = _format_status(s["status"]).split(" ")[0]
        marker = "➡️ " if s["id"] == active_id else ""
        lines.append(
            f"{marker}{status_icon} `{s['id']}` | {title}\n"
            f"   🔗 {s['devin_url']}"
        )

    lines.append(
        "\n➡️ = активная сессия\n"
        "Команды: /switch <id> | /delsession <id>"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /switch <id сессии>\n"
            "Посмотреть ID: /sessions"
        )
        return

    try:
        session_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Посмотреть: /sessions")
        return

    session = await db.get_session_by_id(session_id, update.effective_user.id)
    if not session:
        await update.message.reply_text(f"❌ Сессия ID {session_id} не найдена.")
        return

    await db.set_active_session(update.effective_user.id, session_id)
    user = update.effective_user
    await db.log_activity(user.id, "switch_session", f"ID {session_id}", user.username)
    title = session["title"] or "Без названия"
    await update.message.reply_text(
        f"✅ Переключено на сессию `{session_id}`\n"
        f"📝 {title}\n"
        f"🔗 {session['devin_url']}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_delsession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /delsession <id сессии>\n"
            "Посмотреть ID: /sessions"
        )
        return

    try:
        session_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Посмотреть: /sessions")
        return

    if await db.delete_session(session_id, update.effective_user.id):
        user = update.effective_user
        await db.log_activity(user.id, "delete_session", f"ID {session_id}", user.username)
        await update.message.reply_text(f"✅ Сессия `{session_id}` удалена.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Сессия ID {session_id} не найдена.")


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

    user = update.effective_user
    await db.log_activity(user.id, "add_key", f"ID {key_id}, {label or '—'}", user.username)

    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Ключ добавлен (ID: {key_id}, `{_mask_key(key)}`)\n"
        f"Название: {label or '—'}\n\n"
        f"💡 Сообщение с ключом удалено для безопасности.",
        parse_mode="Markdown",
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
        await update.message.reply_text(
            "Нужно минимум 2 активных ключа для переключения."
        )
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

    user = update.effective_user
    role = "админ" if is_admin else "пользователь"
    await db.log_activity(user.id, "add_user", f"{tg_id} ({role})", user.username)

    await update.message.reply_text(
        f"✅ Добавлен {role}: `{tg_id}`", parse_mode="Markdown"
    )


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
        await update.message.reply_text(
            f"✅ Пользователь `{tg_id}` удалён.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Пользователь `{tg_id}` не найден.", parse_mode="Markdown"
        )


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


# --- Menu, Cost, Log commands ---

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return
    user = update.effective_user
    is_user_admin = await db.is_admin(user.id)
    keyboard = _main_menu_keyboard(is_user_admin)
    await update.message.reply_text(
        "📋 *Меню:*", parse_mode="Markdown", reply_markup=keyboard
    )


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    key_stats = await db.get_usage_by_key()
    user_stats = await db.get_usage_by_user()

    if not key_stats and not user_stats:
        await update.message.reply_text("📊 Пока нет статистики использования.")
        return

    lines = ["💰 *Расход по ключам:*\n"]

    if key_stats:
        for s in key_stats:
            key_display = _mask_key(s["key"]) if s["key"] else f"ID {s['api_key_id']}"
            label = s["label"] or "—"
            lines.append(
                f"🔑 `{key_display}` ({label})\n"
                f"   📝 Сессий: {s['sessions_created']} | 💬 Сообщений: {s['messages_sent']}\n"
                f"   📊 Всего действий: {s['total_actions']}\n"
                f"   🕐 {s['first_used'] or '—'} — {s['last_used'] or '—'}\n"
            )
    else:
        lines.append("Нет данных по ключам.\n")

    lines.append("\n👥 *Расход по пользователям:*\n")
    if user_stats:
        for s in user_stats:
            username = f"@{s['username']}" if s["username"] else f"ID {s['tg_user_id']}"
            lines.append(
                f"• {username}: 📝 {s['sessions_created']} сессий, "
                f"💬 {s['messages_sent']} сообщений"
            )
    else:
        lines.append("Нет данных по пользователям.")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    entries = await db.get_activity_log(limit=30)
    if not entries:
        await update.message.reply_text("📜 Лог пуст.")
        return

    lines = ["📜 *Последние действия:*\n"]
    for e in entries:
        user_display = f"@{e['tg_username']}" if e["tg_username"] else f"ID {e['tg_user_id']}"
        detail = f" — {e['detail']}" if e["detail"] else ""
        lines.append(f"• `{e['created_at']}` {user_display}: {e['action']}{detail}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... _(обрезано)_"

    await update.message.reply_text(text, parse_mode="Markdown")


# --- Inline keyboard callback handler ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = query.from_user
    if not await db.is_whitelisted(user.id):
        await query.edit_message_text("⛔ У вас нет доступа к этому боту.")
        return

    data = query.data

    if data == "menu_newsession":
        await query.edit_message_text(
            "📝 Отправьте команду:\n`/newsession <описание задачи>`\n\n"
            "Пример: `/newsession Исправь баг в main.py`",
            parse_mode="Markdown",
        )

    elif data == "menu_sessions":
        sessions = await db.list_user_sessions(user.id)
        if not sessions:
            await query.edit_message_text("Нет сессий. Создайте: /newsession <задача>")
            return

        active = await db.get_active_session(user.id)
        active_id = active["id"] if active else None

        lines = ["📋 *Все сессии:*\n"]
        for s in sessions[:15]:
            title = s["title"] or "Без названия"
            status_icon = _format_status(s["status"]).split(" ")[0]
            marker = "➡️ " if s["id"] == active_id else ""
            lines.append(f"{marker}{status_icon} `{s['id']}` | {title}")

        if len(sessions) > 15:
            lines.append(f"\n... и ещё {len(sessions) - 15} сессий")
        lines.append("\nКоманды: /switch <id> | /delsession <id>")

        # Add buttons for quick session switching
        session_buttons = []
        for s in sessions[:6]:
            title_short = (s["title"] or "—")[:20]
            session_buttons.append(
                InlineKeyboardButton(
                    f"{'➡️' if s['id'] == active_id else '📝'} {title_short}",
                    callback_data=f"switch_{s['id']}",
                )
            )
        keyboard_rows = [session_buttons[i:i+2] for i in range(0, len(session_buttons), 2)]
        keyboard_rows.append([InlineKeyboardButton("🔙 Меню", callback_data="menu_back")])

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )

    elif data == "menu_session":
        session = await db.get_active_session(user.id)
        if not session:
            await query.edit_message_text("Нет активной сессии. Создайте: /newsession <задача>")
            return
        await query.edit_message_text(
            f"📌 *Активная сессия*\n\n"
            f"📝 {session['title'] or 'Без названия'}\n"
            f"📌 Статус: {_format_status(session['status'])}\n"
            f"🔗 {session['devin_url']}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_status":
        session = await db.get_active_session(user.id)
        if not session:
            await query.edit_message_text("Нет активной сессии.")
            return
        try:
            info = await devin_api.get_session(session["devin_session_id"])
        except devin_api.DevinAPIError as e:
            await query.edit_message_text(f"❌ Ошибка: {e.detail}")
            return
        status = info.get("status_enum", info.get("status", "unknown"))
        await query.edit_message_text(
            f"📊 *Статус сессии*\n\n"
            f"📝 {session['title'] or 'Без названия'}\n"
            f"🔗 {session['devin_url']}\n"
            f"📌 Статус: {_format_status(status)}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="menu_status"),
                 InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_keys":
        if not await db.is_admin(user.id):
            await query.edit_message_text("⛔ Только для админов.")
            return
        keys = await db.list_api_keys()
        if not keys:
            await query.edit_message_text("Нет API ключей. Добавьте: /addkey <ключ> [название]")
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
            lines.append(f"{marker}ID `{k['id']}` | {status} | `{_mask_key(k['key'])}` | {label}")
        lines.append("\n➡️ = текущий активный ключ")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_users":
        if not await db.is_admin(user.id):
            await query.edit_message_text("⛔ Только для админов.")
            return
        users = await db.list_whitelist()
        if not users:
            await query.edit_message_text("Вайтлист пуст.")
            return
        lines = ["👥 *Вайтлист:*\n"]
        for u in users:
            role = "👑 админ" if u["is_admin"] else "👤 юзер"
            username = f"@{u['username']}" if u["username"] else "—"
            lines.append(f"• `{u['tg_id']}` | {username} | {role}")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_cost":
        key_stats = await db.get_usage_by_key()
        user_stats = await db.get_usage_by_user()
        if not key_stats and not user_stats:
            await query.edit_message_text(
                "📊 Пока нет статистики.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
                ]),
            )
            return
        lines = ["💰 *Расход по ключам:*\n"]
        for s in key_stats:
            key_display = _mask_key(s["key"]) if s["key"] else f"ID {s['api_key_id']}"
            label = s["label"] or "—"
            lines.append(
                f"🔑 `{key_display}` ({label})\n"
                f"   📝 Сессий: {s['sessions_created']} | 💬 Сообщений: {s['messages_sent']}"
            )
        lines.append("\n👥 *По пользователям:*")
        for s in user_stats:
            username = f"@{s['username']}" if s["username"] else f"ID {s['tg_user_id']}"
            lines.append(f"• {username}: 📝 {s['sessions_created']} | 💬 {s['messages_sent']}")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="menu_cost"),
                 InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_log":
        if not await db.is_admin(user.id):
            await query.edit_message_text("⛔ Только для админов.")
            return
        entries = await db.get_activity_log(limit=20)
        if not entries:
            await query.edit_message_text(
                "📜 Лог пуст.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
                ]),
            )
            return
        lines = ["📜 *Последние действия:*\n"]
        for e in entries:
            user_display = f"@{e['tg_username']}" if e["tg_username"] else f"ID {e['tg_user_id']}"
            detail = f" — {e['detail']}" if e["detail"] else ""
            lines.append(f"• `{e['created_at']}` {user_display}: {e['action']}{detail}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n... _(обрезано)_"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="menu_log"),
                 InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_myid":
        await query.edit_message_text(
            f"🆔 Ваш Telegram ID: `{user.id}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_help":
        await query.edit_message_text(
            "❓ *Команды:*\n\n"
            "*Сессии:*\n"
            "/newsession `<задача>` — создать\n"
            "/sessions — список\n"
            "/switch `<id>` — переключить\n"
            "/delsession `<id>` — удалить\n\n"
            "*Админ:*\n"
            "/addkey `<ключ>` `[имя]`\n"
            "/adduser `<tg_id>` `[admin]`\n"
            "/cost — расход\n"
            "/log — действия\n"
            "/menu — меню",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )

    elif data == "menu_back":
        is_user_admin = await db.is_admin(user.id)
        keyboard = _main_menu_keyboard(is_user_admin)
        await query.edit_message_text(
            "📋 *Меню:*", parse_mode="Markdown", reply_markup=keyboard
        )

    elif data.startswith("switch_"):
        session_id = int(data.split("_")[1])
        session = await db.get_session_by_id(session_id, user.id)
        if not session:
            await query.edit_message_text(f"❌ Сессия ID {session_id} не найдена.")
            return
        await db.set_active_session(user.id, session_id)
        await db.log_activity(user.id, "switch_session", f"ID {session_id}", user.username)
        title = session["title"] or "Без названия"
        await query.edit_message_text(
            f"✅ Переключено на сессию `{session_id}`\n📝 {title}\n🔗 {session['devin_url']}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Меню", callback_data="menu_back")]
            ]),
        )


# ---------------------------------------------------------------------------
# Text message handler (send to active session)
# ---------------------------------------------------------------------------

REPLY_KB_ACTIONS = {
    "📝 Новая сессия": "kb_newsession",
    "📋 Сессии": "kb_sessions",
    "📌 Текущая": "kb_session",
    "📊 Статус": "kb_status",
    "💰 Расход": "kb_cost",
    "📋 Меню": "kb_menu",
    "🔑 Ключи": "kb_keys",
    "📜 Лог": "kb_log",
}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    text = update.message.text
    if not text:
        return

    # Handle reply keyboard button presses
    kb_action = REPLY_KB_ACTIONS.get(text)
    if kb_action:
        user = update.effective_user
        if kb_action == "kb_newsession":
            await update.message.reply_text(
                "📝 Отправьте команду:\n`/newsession <описание задачи>`\n\n"
                "Пример: `/newsession Исправь баг в main.py`",
                parse_mode="Markdown",
            )
        elif kb_action == "kb_sessions":
            context.args = []
            await cmd_sessions(update, context)
        elif kb_action == "kb_session":
            await cmd_session(update, context)
        elif kb_action == "kb_status":
            await cmd_status(update, context)
        elif kb_action == "kb_cost":
            await cmd_cost(update, context)
        elif kb_action == "kb_menu":
            await cmd_menu(update, context)
        elif kb_action == "kb_keys":
            await cmd_keys(update, context)
        elif kb_action == "kb_log":
            await cmd_log(update, context)
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

    user = update.effective_user
    key_id, _ = await devin_api._get_current_key()
    await db.record_usage(key_id, user.id, "send_message", session["devin_session_id"])
    await db.log_activity(user.id, "send_message", text[:60], user.username)

    await update.message.reply_text(
        f"📨 Сообщение отправлено в сессию.\n🔗 {session['devin_url']}",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# File/photo/document handler — upload to Devin and send as attachment
# ---------------------------------------------------------------------------

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    session = await db.get_active_session(update.effective_user.id)
    if not session:
        await update.message.reply_text(
            "Нет активной сессии. Создайте: /newsession <задача>"
        )
        return

    # Get the file object from the message
    file_obj = None
    filename = "file"
    caption = update.message.caption or ""

    if update.message.photo:
        # Photos come as a list of sizes, take the largest
        file_obj = await update.message.photo[-1].get_file()
        filename = f"photo_{file_obj.file_unique_id}.jpg"
    elif update.message.document:
        file_obj = await update.message.document.get_file()
        filename = update.message.document.file_name or f"doc_{file_obj.file_unique_id}"
    elif update.message.video:
        file_obj = await update.message.video.get_file()
        filename = update.message.video.file_name or f"video_{file_obj.file_unique_id}.mp4"
    elif update.message.audio:
        file_obj = await update.message.audio.get_file()
        filename = update.message.audio.file_name or f"audio_{file_obj.file_unique_id}"
    elif update.message.voice:
        file_obj = await update.message.voice.get_file()
        filename = f"voice_{file_obj.file_unique_id}.ogg"

    if file_obj is None:
        await update.message.reply_text("❌ Не удалось получить файл.")
        return

    msg = await update.message.reply_text(f"⏳ Загружаю файл `{filename}` в Devin...", parse_mode="Markdown")

    try:
        # Download from Telegram
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
            tmp_path = tmp.name
            await file_obj.download_to_drive(tmp_path)

        # Upload to Devin
        attachment_url = await devin_api.upload_file(tmp_path, filename)

        # Send message to session with attachment
        message_text = caption if caption else f"Файл: {filename}"
        message_text += f'\n\nATTACHMENT:"{attachment_url}"'

        await devin_api.send_message(session["devin_session_id"], message_text)

        user = update.effective_user
        key_id, _ = await devin_api._get_current_key()
        await db.record_usage(key_id, user.id, "upload_file", session["devin_session_id"])
        await db.log_activity(user.id, "upload_file", filename, user.username)

        await msg.edit_text(
            f"✅ Файл `{filename}` отправлен в сессию.\n🔗 {session['devin_url']}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except devin_api.DevinAPIError as e:
        await msg.edit_text(f"❌ Ошибка: {e.detail}")
    except Exception as e:
        logger.error("File upload error: %s", e, exc_info=True)
        await msg.edit_text(f"❌ Ошибка загрузки файла: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    await db.get_db()
    logger.info("Database initialized")

    # Start background polling job
    application.job_queue.run_repeating(
        _poll_sessions,
        interval=POLL_INTERVAL_SECONDS,
        first=10,
        name="poll_sessions",
    )
    logger.info("Session polling started (every %ds)", POLL_INTERVAL_SECONDS)


async def post_shutdown(application: Application) -> None:
    await db.close_db()
    logger.info("Database closed")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Session commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("newsession", cmd_newsession))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("delsession", cmd_delsession))

    # Key management
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("removekey", cmd_removekey))
    app.add_handler(CommandHandler("switchkey", cmd_switchkey))

    # Whitelist management
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))

    # Menu, cost, log
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("log", cmd_log))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # File/photo/document handlers
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE,
            handle_file,
        )
    )

    # Text messages → active session
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
