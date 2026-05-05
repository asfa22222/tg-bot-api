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
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import httpx

import database as db
import devin_api
from config import OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, WEBAPP_URL

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15

# ---------------------------------------------------------------------------
# AI Chat — OpenAI-compatible API (only for specific user)
# ---------------------------------------------------------------------------

AI_CHAT_USER_ID = 986832959
AI_CHAT_API_KEY = "sta_eb4a9abaab7cd9aab51bcac7e39773ee381b3aa12ae50a48"
AI_CHAT_API_BASE = "https://api.freetheai.xyz/v1"

AI_MODELS = {
    "gpt5": {"id": "gpt-5.4", "name": "GPT 5.4"},
    "claude": {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6"},
    "glm": {"id": "glm-5.1", "name": "GLM 5.1"},
    "gemini": {"id": "gemini-3.1-pro", "name": "Gemini 3.1 Pro"},
}

# user_id -> {"model": "gpt5", "history": [...]}
_ai_chat_state: dict[int, dict] = {}


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
    "deleted": "🗑 Удалена",
}


def _format_status(status: str) -> str:
    return STATUS_LABELS.get(status, f"❓ {status}")


def _get_webapp_url() -> str:
    """Get Mini App URL from env (supports RAILWAY_PUBLIC_DOMAIN fallback)."""
    url = os.environ.get("WEBAPP_URL", "")
    if not url:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if domain:
            url = f"https://{domain}"
    return url


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
    webapp_url = _get_webapp_url()
    if webapp_url:
        buttons.append(
            [KeyboardButton("📱 Mini App", web_app=WebAppInfo(url=webapp_url))]
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
    webapp_url = _get_webapp_url()
    if webapp_url:
        buttons.append([
            InlineKeyboardButton(
                "📱 Mini App", web_app=WebAppInfo(url=webapp_url)
            ),
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
        except devin_api.DevinAPIError as e:
            if e.status_code == 404:
                logger.info(
                    "Session %s not found (404), stopping polling",
                    sess["devin_session_id"],
                )
                await db.stop_polling_session(sess["id"])
                await db.update_session_status(sess["id"], "deleted")
            else:
                logger.warning(
                    "Poll error for session %s: %s",
                    sess["devin_session_id"], e,
                )
            continue
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

    # Send reply keyboard first
    await update.message.reply_text(
        "🤖 *Devin Telegram Bot*\n\n"
        "Управление сессиями Devin AI прямо из Telegram.\n\n"
        "⬇️ Кнопки внизу — основные действия\n"
        "📋 /menu — подробное меню\n"
        "/ — нажми для списка всех команд",
        parse_mode="Markdown",
        reply_markup=reply_kb,
    )

    # Check if user has API keys set up
    keys = await db.list_api_keys()
    if not keys and is_user_admin:
        await update.message.reply_text(
            "⚠️ *Нет API ключей!*\n\n"
            "Добавьте ключ Devin для начала работы:\n"
            "`/addkey <ваш_api_ключ> Название`\n\n"
            "Ключ можно получить на: https://app.devin.ai → Settings → API Keys",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Как добавить ключ", callback_data="menu_help")]
            ]),
        )


async def cmd_webapp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open Mini App directly."""
    if not await _check_whitelist(update):
        return

    url = _get_webapp_url()

    if not url:
        # Diagnostic: show all RAILWAY_* and WEBAPP_* env vars
        diag = []
        for k, v in sorted(os.environ.items()):
            if k.startswith(("RAILWAY", "WEBAPP", "PORT")):
                diag.append(f"`{k}` = `{v[:40]}`")
        diag_text = "\n".join(diag) if diag else "нет переменных RAILWAY_*/WEBAPP_*"
        await update.message.reply_text(
            f"❌ Mini App не настроен.\n\n"
            f"Диагностика:\n{diag_text}\n\n"
            f"Нужна переменная `WEBAPP_URL`",
        )
        return

    await update.message.reply_text(
        f"📱 Mini App:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📱 Открыть Mini App",
                web_app=WebAppInfo(url=url),
            )]
        ]),
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
        if e.status_code == 429:
            await msg.edit_text(
                "⚠️ Лимит исчерпан!\n\n"
                "Дневная или недельная квота Devin закончилась.\n"
                "Подождите сброса или добавьте другой ключ: /addkey"
            )
        else:
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
        f"Отправляйте сообщения и файлы — они пойдут в эту сессию.",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Статус", callback_data="menu_status"),
                InlineKeyboardButton("📋 Сессии", callback_data="menu_sessions"),
            ],
            [InlineKeyboardButton("🔗 Открыть в Devin", url=session_url)],
        ]),
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


async def cmd_devin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all sessions on the Devin account (admin only)."""
    if not await _check_whitelist(update):
        return

    user = update.effective_user
    if not await db.is_admin(user.id):
        await update.message.reply_text("⛔ Только для владельца бота.")
        return

    await update.message.reply_text("🔄 Загружаю сессии с аккаунта Devin...")

    try:
        sessions = await devin_api.list_account_sessions(limit=20)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка API: {e}")
        return

    if not sessions:
        await update.message.reply_text("Нет сессий на аккаунте Devin.")
        return

    lines = [f"🌐 *Сессии на аккаунте Devin ({len(sessions)}):*\n"]
    for s in sessions:
        title = s.get("title") or s.get("prompt", "")[:40] or "Без названия"
        status = s.get("status_enum") or s.get("status", "unknown")
        session_id = s.get("session_id", s.get("id", "?"))
        url = s.get("url", f"https://app.devin.ai/sessions/{session_id}")
        created = s.get("created_at", "")[:10]
        status_icon = STATUS_LABELS.get(status, f"❓ {status}").split(" ")[0]
        lines.append(
            f"{status_icon} *{title[:35]}*\n"
            f"   ID: `{session_id[:12]}...`\n"
            f"   📅 {created} | 🔗 [Открыть]({url})"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    await db.log_activity(user.id, user.username, "devin_sessions", f"viewed {len(sessions)} sessions")


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


async def cmd_exportkeys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_admin(update):
        return

    keys = await db.list_api_keys()
    if not keys:
        await update.message.reply_text("Нет API ключей для экспорта.")
        return

    lines = ["Экспорт API ключей Devin Telegram Bot", "=" * 40, ""]
    for k in keys:
        status = "Активен" if k["is_active"] else "Отключён"
        label = k["label"] or "Без названия"
        lines.append(f"ID: {k['id']}")
        lines.append(f"Название: {label}")
        lines.append(f"Статус: {status}")
        lines.append(f"Ключ: {k['key']}")
        lines.append(f"Добавлен: {k['added_at']}")
        lines.append("-" * 40)
        lines.append("")

    content = "\n".join(lines)
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = "devin_api_keys.txt"

    await update.message.reply_document(
        document=bio,
        filename="devin_api_keys.txt",
        caption="🔑 Экспорт всех API ключей. Храните в безопасном месте!",
    )

    user = update.effective_user
    await db.log_activity(user.id, "export_keys", f"{len(keys)} keys", user.username)


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


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast message to all users. Supports text, photos, documents, video.

    Usage:
      /broadcast <text>                    — send text
      Reply to a photo/file with /broadcast — forward that media to all
      Send photo with caption /broadcast <text> — send photo+text to all
    """
    if not await _check_admin(update):
        return

    text = " ".join(context.args) if context.args else ""
    reply = update.message.reply_to_message

    # Determine what to broadcast
    has_photo = update.message.photo
    has_document = update.message.document
    has_video = update.message.video
    reply_photo = reply and reply.photo
    reply_document = reply and reply.document
    reply_video = reply and reply.video

    # Get caption text
    if not text and update.message.caption:
        # /broadcast in caption of a photo
        caption_text = update.message.caption
        # Remove the /broadcast command from caption
        caption_text = re.sub(r'^/broadcast\s*', '', caption_text).strip()
        if caption_text:
            text = caption_text
    if not text and reply and reply.caption:
        text = reply.caption

    has_media = has_photo or has_document or has_video or reply_photo or reply_document or reply_video

    if not text and not has_media:
        await update.message.reply_text(
            "📢 *Рассылка — как использовать:*\n\n"
            "1️⃣ Текст: `/broadcast Привет всем!`\n"
            "2️⃣ Фото: отправьте фото с подписью `/broadcast Текст`\n"
            "3️⃣ Ответ: ответьте на фото/файл командой `/broadcast`\n"
            "4️⃣ Файл: ответьте на документ/видео командой `/broadcast`",
            parse_mode="Markdown",
        )
        return

    users = await db.list_whitelist()
    if not users:
        await update.message.reply_text("Вайтлист пуст — некому отправлять.")
        return

    status_msg = await update.message.reply_text(
        f"📤 Рассылка {len(users)} пользователям..."
    )

    broadcast_caption = f"📢 *Рассылка от админа:*\n\n{text}" if text else "📢 *Рассылка от админа*"

    sent = 0
    failed = 0
    for u in users:
        try:
            if has_photo:
                await context.bot.send_photo(
                    chat_id=u["tg_id"],
                    photo=update.message.photo[-1].file_id,
                    caption=broadcast_caption,
                    parse_mode="Markdown",
                )
            elif has_document:
                await context.bot.send_document(
                    chat_id=u["tg_id"],
                    document=update.message.document.file_id,
                    caption=broadcast_caption,
                    parse_mode="Markdown",
                )
            elif has_video:
                await context.bot.send_video(
                    chat_id=u["tg_id"],
                    video=update.message.video.file_id,
                    caption=broadcast_caption,
                    parse_mode="Markdown",
                )
            elif reply_photo:
                await context.bot.send_photo(
                    chat_id=u["tg_id"],
                    photo=reply.photo[-1].file_id,
                    caption=broadcast_caption,
                    parse_mode="Markdown",
                )
            elif reply_document:
                await context.bot.send_document(
                    chat_id=u["tg_id"],
                    document=reply.document.file_id,
                    caption=broadcast_caption,
                    parse_mode="Markdown",
                )
            elif reply_video:
                await context.bot.send_video(
                    chat_id=u["tg_id"],
                    video=reply.video.file_id,
                    caption=broadcast_caption,
                    parse_mode="Markdown",
                )
            else:
                await context.bot.send_message(
                    chat_id=u["tg_id"],
                    text=broadcast_caption,
                    parse_mode="Markdown",
                )
            sent += 1
        except Exception as e:
            logger.warning("Broadcast failed for %s: %s", u["tg_id"], e)
            failed += 1

    user = update.effective_user
    media_type = "photo" if (has_photo or reply_photo) else "document" if (has_document or reply_document) else "video" if (has_video or reply_video) else "text"
    await db.log_activity(user.id, "broadcast", f"{media_type}, {sent} ok, {failed} fail: {text[:40]}", user.username)

    await status_msg.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}\n"
        f"👥 Всего: {len(users)}"
    )


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

    lines = ["📜 Последние действия:\n"]
    for e in entries:
        user_display = f"@{e['tg_username']}" if e["tg_username"] else f"ID {e['tg_user_id']}"
        detail = f" — {e['detail']}" if e["detail"] else ""
        lines.append(f"• {e['created_at']} {user_display}: {e['action']}{detail}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (обрезано)"

    await update.message.reply_text(text)


# --- Inline keyboard callback handler ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = query.from_user
    if not await db.is_whitelisted(user.id):
        await query.edit_message_text("⛔ У вас нет доступа к этому боту.")
        return

    data = query.data

    # AI Chat model selection callbacks
    if data and (data.startswith("ai_model:") or data in ("ai_clear", "ai_stop")):
        await _handle_ai_model_callback(update, context)
        return

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
        lines = ["📜 Последние действия:\n"]
        for e in entries:
            user_display = f"@{e['tg_username']}" if e["tg_username"] else f"ID {e['tg_user_id']}"
            detail = f" — {e['detail']}" if e["detail"] else ""
            lines.append(f"• {e['created_at']} {user_display}: {e['action']}{detail}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (обрезано)"
        await query.edit_message_text(
            text,
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

    elif data.startswith("vnew:"):
        vid = data[len("vnew:"):]
        prompt = _get_voice_text(vid)
        if not prompt:
            await query.edit_message_text("❌ Текст истёк. Отправьте голосовое заново.")
            return
        await query.edit_message_text("⏳ Создаю сессию...")
        try:
            key_id, api_key = await devin_api._get_current_key()
            session_id, session_url = await devin_api.create_session(prompt)
            await db.create_session_record(
                devin_session_id=session_id,
                devin_url=session_url,
                title=prompt[:100],
                tg_user_id=user.id,
                tg_chat_id=query.message.chat_id,
            )
            await db.record_usage(key_id, user.id, "create_session", session_id)
            await db.log_activity(user.id, "voice_create_session", prompt[:80], user.username)
            await query.edit_message_text(
                f"✅ *Сессия создана (голос):*\n\n"
                f"🔗 {session_url}\n"
                f"📝 {prompt[:100]}",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Открыть в Devin", url=session_url)],
                ]),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")

    elif data.startswith("vsend:"):
        vid = data[len("vsend:"):]
        text = _get_voice_text(vid)
        if not text:
            await query.edit_message_text("❌ Текст истёк. Отправьте голосовое заново.")
            return
        session = await db.get_active_session(user.id)
        if not session:
            await query.edit_message_text("❌ Нет активной сессии.")
            return
        await query.edit_message_text("⏳ Отправляю в сессию...")
        try:
            await devin_api.send_message(session["devin_session_id"], text)
            key_id, _ = await devin_api._get_current_key()
            await db.record_usage(key_id, user.id, "send_message", session["devin_session_id"])
            await db.log_activity(user.id, "voice_send_message", text[:80], user.username)
            await query.edit_message_text(
                f"✅ Голосовое сообщение отправлено в сессию.\n🔗 {session['devin_url']}",
                disable_web_page_preview=True,
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")


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

    # AI Chat mode — intercept messages if user has active AI chat
    user = update.effective_user
    if user.id in _ai_chat_state:
        # Still allow keyboard buttons
        if text not in REPLY_KB_ACTIONS:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="typing"
            )
            model_name = AI_MODELS[_ai_chat_state[user.id]["model"]]["name"]
            reply = await _send_ai_message(user.id, text)
            # Split long messages (Telegram 4096 char limit)
            if len(reply) > 4000:
                for i in range(0, len(reply), 4000):
                    await update.message.reply_text(reply[i:i+4000])
            else:
                await update.message.reply_text(reply)
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
        if e.status_code == 404 or "not found" in (e.detail or "").lower():
            await db.stop_polling_session(session["id"])
            await db.update_session_status(session["id"], "deleted")
            await update.message.reply_text(
                "❌ Сессия больше не существует на Devin.\n"
                "Создайте новую: /newsession <задача>"
            )
        else:
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
# Voice message handler — speech-to-text via OpenAI Whisper
# ---------------------------------------------------------------------------

async def _transcribe_voice(file_bytes: bytes) -> str | None:
    """Transcribe audio. Uses OpenAI Whisper if key set, else free Google Speech."""
    import asyncio

    if OPENAI_API_KEY:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": ("voice.ogg", file_bytes, "audio/ogg")},
                data={"model": "whisper-1"},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip()

    # Free: Google Speech Recognition via SpeechRecognition + pydub
    import speech_recognition as sr
    from pydub import AudioSegment

    def _recognize(audio_bytes: bytes) -> str:
        ogg_path = os.path.join(tempfile.gettempdir(), "voice_input.ogg")
        wav_path = os.path.join(tempfile.gettempdir(), "voice_input.wav")
        try:
            with open(ogg_path, "wb") as f:
                f.write(audio_bytes)
            audio = AudioSegment.from_ogg(ogg_path)
            audio.export(wav_path, format="wav")
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
            return recognizer.recognize_google(audio_data, language="ru-RU")
        finally:
            for p in (ogg_path, wav_path):
                try:
                    os.unlink(p)
                except Exception:
                    pass

    return await asyncio.to_thread(_recognize, file_bytes)


# In-memory cache for voice transcription texts (callback_data limited to 64 bytes)
import uuid as _uuid

_voice_cache: dict[str, str] = {}


def _store_voice_text(text: str) -> str:
    """Store text and return a short ID for callback_data."""
    short_id = _uuid.uuid4().hex[:8]
    _voice_cache[short_id] = text
    # Keep cache small — remove oldest if >100 entries
    if len(_voice_cache) > 100:
        oldest = next(iter(_voice_cache))
        del _voice_cache[oldest]
    return short_id


def _get_voice_text(short_id: str) -> str | None:
    return _voice_cache.pop(short_id, None)


VOICE_COMMANDS = {
    # (keywords in recognized text) → (command_func, needs_args, description)
    "новая сессия": ("newsession", True, "📝 Создаю сессию..."),
    "создай сессию": ("newsession", True, "📝 Создаю сессию..."),
    "new session": ("newsession", True, "📝 Creating session..."),
    "сессии": ("sessions", False, "📋 Список сессий..."),
    "список сессий": ("sessions", False, "📋 Список сессий..."),
    "статус": ("status", False, "📊 Проверяю статус..."),
    "текущая": ("session", False, "📌 Текущая сессия..."),
    "ключи": ("keys", False, "🔑 Список ключей..."),
    "расход": ("cost", False, "💰 Расход..."),
    "лог": ("log", False, "📜 Лог действий..."),
    "меню": ("menu", False, "📋 Меню..."),
    "пользователи": ("users", False, "👥 Вайтлист..."),
    "помощь": ("help", False, "❓ Помощь..."),
}

VOICE_CMD_MAP = {
    "newsession": cmd_newsession,
    "sessions": cmd_sessions,
    "status": cmd_status,
    "session": cmd_session,
    "keys": cmd_keys,
    "cost": cmd_cost,
    "log": cmd_log,
    "menu": cmd_menu,
    "users": cmd_users,
    "help": cmd_start,
}


def _match_voice_command(text: str) -> tuple[str | None, str]:
    """Match recognized text to a bot command. Returns (cmd_name, remaining_text)."""
    lower = text.lower()
    for keyword, (cmd_name, needs_args, _) in VOICE_COMMANDS.items():
        if keyword in lower:
            # Extract remaining text after keyword for commands that need args
            idx = lower.index(keyword) + len(keyword)
            remaining = text[idx:].strip().strip(".,!?")
            return cmd_name, remaining
    return None, text


VOICE_DURATION_THRESHOLD = 5  # seconds: ≤5s = command mode, >5s = chat mode


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    voice = update.message.voice
    duration = voice.duration or 0
    is_command_mode = duration <= VOICE_DURATION_THRESHOLD

    mode_label = "🎛 Команда" if is_command_mode else "💬 Чат с Devin"
    msg = await update.message.reply_text(f"🎙 Распознаю речь... ({mode_label})")

    try:
        file_obj = await voice.get_file()
        voice_bytes = await file_obj.download_as_bytearray()

        text = await _transcribe_voice(bytes(voice_bytes))

        if not text:
            await msg.edit_text("❌ Не удалось распознать речь. Попробуйте ещё раз.")
            return

        engine = "Whisper" if OPENAI_API_KEY else "Google"
        user = update.effective_user

        if is_command_mode:
            # --- COMMAND MODE: short voice → match bot commands ---
            cmd_name, remaining = _match_voice_command(text)

            if cmd_name:
                description = ""
                for kw, (cn, _, desc) in VOICE_COMMANDS.items():
                    if cn == cmd_name:
                        description = desc
                        break

                await msg.edit_text(f"🎛 «{text}»\n\n{description}")

                handler_func = VOICE_CMD_MAP.get(cmd_name)
                if handler_func:
                    if cmd_name == "newsession" and remaining:
                        context.args = remaining.split()
                    elif cmd_name == "newsession" and not remaining:
                        context.args = text.split()
                    else:
                        context.args = []
                    await handler_func(update, context)
            else:
                # Short but no command matched — ask what to do
                vid = _store_voice_text(text)
                await msg.edit_text(f"🎛 «{text}»\n\nКоманда не распознана.")
                await update.message.reply_text(
                    "Что сделать?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "📝 Новая сессия",
                            callback_data=f"vnew:{vid}",
                        )],
                        [InlineKeyboardButton(
                            "💬 Отправить в сессию",
                            callback_data=f"vsend:{vid}",
                        )],
                    ]),
                )

            await db.log_activity(user.id, "voice_command", f"[{cmd_name or '?'}] {text[:60]}", user.username)

        else:
            # --- CHAT MODE: long voice → send to active Devin session ---
            session = await db.get_active_session(user.id)

            if not session:
                await msg.edit_text(
                    f"💬 «{text}»\n\n"
                    f"❌ Нет активной сессии. Создайте сначала: /newsession"
                )
                return

            await msg.edit_text(f"💬 «{text}»\n\n⏳ Отправляю в Devin...")

            try:
                await devin_api.send_message(session["devin_session_id"], text)
                key_id, _ = await devin_api._get_current_key()
                await db.record_usage(key_id, user.id, "send_message", session["devin_session_id"])

                await msg.edit_text(
                    f"💬 «{text}»\n\n"
                    f"✅ Отправлено в сессию.\n"
                    f"🔗 {session['devin_url']}",
                    disable_web_page_preview=True,
                )
            except devin_api.DevinAPIError as e:
                if e.status_code == 404 or "not found" in (e.detail or "").lower():
                    await db.stop_polling_session(session["id"])
                    await db.update_session_status(session["id"], "deleted")
                    await msg.edit_text(
                        f"💬 «{text}»\n\n"
                        f"❌ Сессия больше не существует на Devin.\n"
                        f"Создайте новую: /newsession <задача>"
                    )
                else:
                    await msg.edit_text(f"💬 «{text}»\n\n❌ Ошибка: {e.detail}")

            await db.log_activity(user.id, "voice_chat", text[:60], user.username)

    except Exception as e:
        logger.error("Voice transcription error: %s", e, exc_info=True)
        await msg.edit_text(f"❌ Ошибка распознавания: {e}")


# ---------------------------------------------------------------------------
# File/photo/document handler — upload to Devin and send as attachment
# ---------------------------------------------------------------------------

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_whitelist(update):
        return

    # Check if this is a broadcast with media (caption starts with /broadcast)
    caption = update.message.caption or ""
    if caption.startswith("/broadcast"):
        args_text = caption.replace("/broadcast", "", 1).strip()
        context.args = args_text.split() if args_text else []
        await cmd_broadcast(update, context)
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
    file_size = 0
    caption = update.message.caption or ""

    try:
        if update.message.photo:
            photo = update.message.photo[-1]
            file_size = photo.file_size or 0
            file_obj = await photo.get_file()
            filename = f"photo_{file_obj.file_unique_id}.jpg"
        elif update.message.document:
            file_size = update.message.document.file_size or 0
            file_obj = await update.message.document.get_file()
            filename = update.message.document.file_name or f"doc_{file_obj.file_unique_id}"
        elif update.message.video:
            file_size = update.message.video.file_size or 0
            file_obj = await update.message.video.get_file()
            filename = update.message.video.file_name or f"video_{file_obj.file_unique_id}.mp4"
        elif update.message.audio:
            file_size = update.message.audio.file_size or 0
            file_obj = await update.message.audio.get_file()
            filename = update.message.audio.file_name or f"audio_{file_obj.file_unique_id}"
        elif update.message.voice:
            file_size = update.message.voice.file_size or 0
            file_obj = await update.message.voice.get_file()
            filename = f"voice_{file_obj.file_unique_id}.ogg"
    except Exception as e:
        size_mb = file_size / (1024 * 1024) if file_size else 0
        if "too big" in str(e).lower() or "file is too big" in str(e).lower():
            await update.message.reply_text(
                f"❌ Файл слишком большой ({size_mb:.1f} МБ).\n\n"
                f"Telegram Bot API ограничивает скачивание файлов до 20 МБ.\n"
                f"Отправьте файл меньшего размера или загрузите его напрямую в Devin через веб-интерфейс."
            )
        else:
            await update.message.reply_text(f"❌ Ошибка получения файла: {e}")
        return

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

    # Start Mini App web server
    logger.info("WEBAPP_URL = %r", WEBAPP_URL)
    from web_server import start_web_server
    try:
        application.bot_data["web_runner"] = await start_web_server()
    except Exception as e:
        logger.warning("Failed to start web server: %s", e)

    # Register bot commands menu (shows on "/" in chat)
    from telegram import BotCommand, BotCommandScopeAllPrivateChats
    commands = [
        BotCommand("menu", "📋 Главное меню"),
        BotCommand("newsession", "📝 Новая сессия"),
        BotCommand("sessions", "📋 Все сессии"),
        BotCommand("session", "📌 Текущая сессия"),
        BotCommand("status", "📊 Статус сессии"),
        BotCommand("switch", "🔄 Переключить сессию"),
        BotCommand("delsession", "🗑 Удалить сессию"),
        BotCommand("cost", "💰 Расход по ключам"),
        BotCommand("addkey", "🔑 Добавить API ключ"),
        BotCommand("keys", "🔑 Список ключей"),
        BotCommand("exportkeys", "📤 Экспорт ключей в файл"),
        BotCommand("adduser", "👤 Добавить пользователя"),
        BotCommand("users", "👥 Вайтлист"),
        BotCommand("broadcast", "📢 Рассылка всем"),
        BotCommand("log", "📜 Лог действий"),
        BotCommand("chat", "🤖 AI чат (GPT/Claude/Gemini)"),
        BotCommand("stopchat", "🚫 Выйти из AI чата"),
        BotCommand("devin", "🌐 Сессии на аккаунте Devin"),
        BotCommand("webapp", "📱 Mini App"),
        BotCommand("myid", "🆔 Мой Telegram ID"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands menu registered (%d commands)", len(commands))
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)

    # Start background polling job
    application.job_queue.run_repeating(
        _poll_sessions,
        interval=POLL_INTERVAL_SECONDS,
        first=10,
        name="poll_sessions",
    )
    logger.info("Session polling started (every %ds)", POLL_INTERVAL_SECONDS)


async def post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()
        logger.info("Web server stopped")
    await db.close_db()
    logger.info("Database closed")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — log errors and notify user if possible."""
    logger.error("Unhandled exception:", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Произошла внутренняя ошибка. Попробуйте ещё раз.",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# AI Chat commands
# ---------------------------------------------------------------------------

async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start or manage AI chat mode."""
    if not await _check_whitelist(update):
        return

    user = update.effective_user
    if user.id != AI_CHAT_USER_ID:
        await update.message.reply_text("⛔ AI чат доступен только владельцу бота.")
        return

    state = _ai_chat_state.get(user.id)

    # /chat без аргументов — показать меню выбора модели
    buttons = []
    for key, m in AI_MODELS.items():
        current = " ✅" if state and state.get("model") == key else ""
        buttons.append([
            InlineKeyboardButton(
                f"{m['name']}{current}",
                callback_data=f"ai_model:{key}",
            )
        ])
    if state:
        buttons.append([
            InlineKeyboardButton("🗑 Очистить историю", callback_data="ai_clear"),
            InlineKeyboardButton("🚫 Выйти из чата", callback_data="ai_stop"),
        ])

    current_model = AI_MODELS[state["model"]]["name"] if state else "не выбрана"
    history_len = len(state["history"]) // 2 if state else 0

    await update.message.reply_text(
        f"🤖 *AI Chat*\n\n"
        f"Модель: *{current_model}*\n"
        f"История: {history_len} сообщений\n\n"
        f"Выберите модель:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_ai_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle AI model selection callback."""
    query = update.callback_query
    user = query.from_user

    if user.id != AI_CHAT_USER_ID:
        await query.answer("⛔ Нет доступа")
        return

    data = query.data

    if data == "ai_clear":
        state = _ai_chat_state.get(user.id)
        if state:
            state["history"] = []
        await query.answer("🗑 История очищена")
        await query.edit_message_text("🗑 История чата очищена. Напишите сообщение для продолжения.")
        return

    if data == "ai_stop":
        _ai_chat_state.pop(user.id, None)
        await query.answer("🚫 Чат выключен")
        await query.edit_message_text(
            "🚫 AI чат выключен. Сообщения снова идут в Devin.\n"
            "Включить: /chat"
        )
        return

    if data.startswith("ai_model:"):
        model_key = data.split(":", 1)[1]
        if model_key not in AI_MODELS:
            await query.answer("Неизвестная модель")
            return

        if user.id not in _ai_chat_state:
            _ai_chat_state[user.id] = {"model": model_key, "history": []}
        else:
            _ai_chat_state[user.id]["model"] = model_key

        model_name = AI_MODELS[model_key]["name"]
        await query.answer(f"Модель: {model_name}")
        await query.edit_message_text(
            f"🤖 Модель: *{model_name}*\n\n"
            f"Пишите сообщения — отвечу от ИИ.\n"
            f"Команды:\n"
            f"/chat — сменить модель\n"
            f"/stopchat — выйти из AI чата",
            parse_mode="Markdown",
        )
        return


async def cmd_stopchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop AI chat mode."""
    user = update.effective_user
    if _ai_chat_state.pop(user.id, None):
        await update.message.reply_text(
            "🚫 AI чат выключен. Сообщения снова идут в Devin.\n"
            "Включить: /chat"
        )
    else:
        await update.message.reply_text("AI чат не был включён.")


async def _send_ai_message(user_id: int, text: str) -> str:
    """Send message to AI and return response."""
    state = _ai_chat_state.get(user_id)
    if not state:
        return "AI чат не включён. Напишите /chat"

    model_key = state["model"]
    model_id = AI_MODELS[model_key]["id"]
    history = state["history"]

    # Add user message
    history.append({"role": "user", "content": text})

    # Keep last 20 messages to avoid token limits
    if len(history) > 20:
        history[:] = history[-20:]

    messages = [
        {"role": "system", "content": "You are a helpful AI assistant. Respond in the same language as the user."},
        *history,
    ]

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{AI_CHAT_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {AI_CHAT_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_id,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]

            # Add assistant reply to history
            history.append({"role": "assistant", "content": reply})

            return reply
    except httpx.HTTPStatusError as e:
        logger.error("AI API error: %s %s", e.response.status_code, e.response.text[:200])
        return f"❌ Ошибка AI API: {e.response.status_code}"
    except Exception as e:
        logger.error("AI chat error: %s", e)
        return f"❌ Ошибка: {e}"


WEBAPP_CMD_MAP = {
    "newsession": cmd_newsession,
    "sessions": cmd_sessions,
    "status": cmd_status,
    "cost": cmd_cost,
    "keys": cmd_keys,
    "log": cmd_log,
    "menu": cmd_menu,
    "exportkeys": cmd_exportkeys,
}


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle data sent from Telegram Mini App."""
    if not await _check_whitelist(update):
        return

    try:
        data = json.loads(update.effective_message.web_app_data.data)
        cmd = data.get("command", "")
        handler_func = WEBAPP_CMD_MAP.get(cmd)
        if handler_func:
            context.args = []
            await handler_func(update, context)
        else:
            await update.message.reply_text(f"Неизвестная команда: {cmd}")
    except Exception as e:
        logger.error("WebApp data error: %s", e)
        await update.message.reply_text(f"❌ Ошибка: {e}")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Global error handler
    app.add_error_handler(error_handler)

    # Session commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("webapp", cmd_webapp))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("newsession", cmd_newsession))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("delsession", cmd_delsession))
    app.add_handler(CommandHandler("devin", cmd_devin))

    # Key management
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("exportkeys", cmd_exportkeys))
    app.add_handler(CommandHandler("removekey", cmd_removekey))
    app.add_handler(CommandHandler("switchkey", cmd_switchkey))

    # Whitelist management
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Menu, cost, log
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("log", cmd_log))

    # AI Chat
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("stopchat", cmd_stopchat))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Voice message handler (speech-to-text)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # File/photo/document handlers
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO,
            handle_file,
        )
    )

    # WebApp data handler (Mini App actions)
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))

    # Text messages → active session
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
