"""Bot configuration."""

import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

DEVIN_API_BASE = "https://api.devin.ai/v1"

DB_PATH = os.environ.get("DB_PATH", "/data/bot_data.db")

# OpenAI Whisper API for voice recognition (optional)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
