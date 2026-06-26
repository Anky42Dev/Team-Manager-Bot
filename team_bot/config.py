# ══════════════════════════════════════════════════
#   НАСТРОЙКИ БОТА — заполните файл .env в корне
# ══════════════════════════════════════════════════

import os
from dotenv import load_dotenv

load_dotenv()

def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Переменная {name} не задана в .env")
    return val

BOT_TOKEN         = _require("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")

GROUP_ID          = int(_require("GROUP_ID"))
TOPIC_WORK_TIME   = int(_require("TOPIC_WORK_TIME"))
TOPIC_GENERAL     = int(_require("TOPIC_GENERAL"))
TOPIC_REPORTS     = int(_require("TOPIC_REPORTS"))
ADMIN_IDS         = [int(x) for x in _require("ADMIN_IDS").split(",")]
TIMEZONE          = os.getenv("TIMEZONE", "Asia/Bishkek")
