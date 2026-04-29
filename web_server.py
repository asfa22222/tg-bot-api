"""Lightweight web server for Telegram Mini App."""

import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qs, unquote

from aiohttp import web

import database as db
from config import TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

WEBAPP_DIR = os.path.join(os.path.dirname(__file__), "webapp")
PORT = int(os.environ.get("PORT", 8080))


def _mask_key(key: str) -> str:
    if len(key) <= 12:
        return key[:4] + "****"
    return key[:6] + "..." + key[-4:]


def _validate_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData and return parsed data."""
    if not init_data:
        return None

    try:
        parsed = parse_qs(init_data)
        check_hash = parsed.get("hash", [None])[0]
        if not check_hash:
            return None

        # Build data-check-string
        items = []
        for key, val in sorted(parsed.items()):
            if key != "hash":
                items.append(f"{key}={val[0]}")
        data_check_string = "\n".join(items)

        # Compute HMAC
        secret_key = hmac.new(
            b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        computed = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if computed != check_hash:
            return None

        user_str = parsed.get("user", [None])[0]
        if user_str:
            return json.loads(unquote(user_str))
        return {}
    except Exception as e:
        logger.warning("initData validation failed: %s", e)
        return None


def _get_user(request: web.Request) -> dict | None:
    """Extract and validate Telegram user from request."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return _validate_init_data(init_data)


async def handle_stats(request: web.Request):
    user = _get_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    user_id = user.get("id")
    sessions = await db.list_user_sessions(user_id) if user_id else []
    user_stats = await db.get_usage_by_user()

    total_messages = 0
    for s in user_stats:
        if s["tg_user_id"] == user_id:
            total_messages = s["messages_sent"]
            break

    active = sum(1 for s in sessions if s.get("status") in ("running", "suspended"))

    return web.json_response({
        "total_sessions": len(sessions),
        "active_sessions": active,
        "total_messages": total_messages,
    })


async def handle_sessions(request: web.Request):
    user = _get_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    user_id = user.get("id")
    sessions = await db.list_user_sessions(user_id) if user_id else []

    return web.json_response({
        "sessions": [
            {
                "id": s["id"],
                "title": s["title"],
                "status": s["status"],
                "devin_url": s["devin_url"],
                "created_at": s["created_at"],
            }
            for s in sessions
        ]
    })


async def handle_keys(request: web.Request):
    user = _get_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    user_id = user.get("id")
    is_admin = await db.is_admin(user_id) if user_id else False
    if not is_admin:
        return web.json_response({"keys": []})

    keys = await db.list_api_keys()
    return web.json_response({
        "keys": [
            {
                "id": k["id"],
                "label": k["label"] or "Без названия",
                "is_active": k["is_active"],
                "masked_key": _mask_key(k["key"]),
                "added_at": k["added_at"],
            }
            for k in keys
        ]
    })


async def handle_log(request: web.Request):
    user = _get_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    user_id = user.get("id")
    is_admin = await db.is_admin(user_id) if user_id else False
    if not is_admin:
        return web.json_response({"entries": []})

    entries = await db.get_activity_log(limit=30)
    return web.json_response({
        "entries": [
            {
                "action": e["action"],
                "detail": e["detail"],
                "username": e["tg_username"],
                "created_at": e["created_at"],
            }
            for e in entries
        ]
    })


def create_app() -> web.Application:
    app = web.Application()

    # API routes
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/api/sessions", handle_sessions)
    app.router.add_get("/api/keys", handle_keys)
    app.router.add_get("/api/log", handle_log)

    # Serve index.html at root
    async def handle_index(request: web.Request):
        return web.FileResponse(os.path.join(WEBAPP_DIR, "index.html"))

    app.router.add_get("/", handle_index)

    # Serve Mini App static files
    app.router.add_static("/static/", WEBAPP_DIR)

    return app


async def start_web_server():
    """Start the web server (call from bot's main)."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Mini App web server started on port %d", PORT)
    return runner
