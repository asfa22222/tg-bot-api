"""Telegram bot for Devin AI session management."""

import io
import json
import logging
import os
import re
import tempfile

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
        sent = await _send_url_as_file(bot, chat_id, url, caption="📎 От Devin")
        if not sent:
            # If download failed, at least send the URL
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

    await update.message.reply_text(
        "🤖 *Devin Telegram Bot*\n\n"
        "Управление сессиями Devin AI прямо из Telegram.\n\n"
        "*Сессии:*\n"
        "/newsession `<задача>` — создать новую сессию\n"
        "/session — текущая активная сессия\n"
        "/status — статус текущей сессии\n"
        "/sessions — все сессии с ID\n"
        "/switch `<id>` — переключиться на сессию\n"
        "/delsession `<id>` — удалить сессию\n"
        "Текстовое сообщение → отправляется в текущую сессию\n"
        "Файлы/картинки → загружаются и отправляются в сессию\n\n"
        "*API ключи (админ):*\n"
        "/addkey `<ключ>` `[название]` — добавить ключ\n"
        "/keys — список ключей\n"
        "/removekey `<id>` — удалить ключ\n"
        "/switchkey — переключить на следующий ключ\n\n"
        "*Вайтлист (админ):*\n"
        "/adduser `<tg_id>` — добавить пользователя\n"
        "/removeuser `<tg_id>` — удалить пользователя\n"
        "/users — список пользователей\n"
        "/myid — показать ваш Telegram ID",
        parse_mode="Markdown",
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

    role = "админ" if is_admin else "пользователь"
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


# ---------------------------------------------------------------------------
# Text message handler (send to active session)
# ---------------------------------------------------------------------------

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
