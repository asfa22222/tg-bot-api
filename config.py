"""Bot configuration."""

import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

DEVIN_API_BASE = "https://api.devin.ai/v1"

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")

KIRO_API_KEY = os.environ.get("KIRO_API_KEY", "")
