"""
Multi-provider AI client with automatic fallback.

Order: Anthropic → OpenAI → Gemini → Groq
Each provider is skipped if its API key is not configured.
"""

import logging
from config import (
    ANTHROPIC_API_KEY,
    OPENAI_API_KEY,
    GEMINI_API_KEY,
    GROQ_API_KEY,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — аналитик команды разработки. "
    "Отвечай ТОЛЬКО на русском языке. "
    "Никаких других языков — ни английского, ни корейского, ни любого другого. "
    "Никакого markdown-форматирования: никаких **, *, #, __, ~~ и подобных символов. "
    "Пиши обычным текстом. Заголовки разделов пиши заглавными буквами на отдельной строке. "
    "Опирайся строго на предоставленные данные — не придумывай факты."
)


# ── Provider implementations ─────────────────────────────────────────────────

def _call_anthropic(prompt: str, max_tokens: int) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _call_openai(prompt: str, max_tokens: int) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content


def _call_gemini(prompt: str, max_tokens: int) -> str:
    if not GEMINI_API_KEY.startswith("AIza"):
        raise ValueError("Gemini key invalid — must start with 'AIza'. Get it at aistudio.google.com/app/apikey")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            system_instruction=SYSTEM_PROMPT,
        ),
    )
    return response.text


def _call_groq(prompt: str, max_tokens: int) -> str:
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content


# ── Provider registry ────────────────────────────────────────────────────────

_PROVIDERS = [
    ("Anthropic", ANTHROPIC_API_KEY, _call_anthropic),
    ("OpenAI",    OPENAI_API_KEY,    _call_openai),
    ("Gemini",    GEMINI_API_KEY,    _call_gemini),
    ("Groq",      GROQ_API_KEY,      _call_groq),
]


def call_ai(prompt: str, max_tokens: int = 1000) -> str:
    """Try each configured provider in order, fallback on any error."""
    available = [(name, fn) for name, key, fn in _PROVIDERS if key]
    if not available:
        raise RuntimeError("Нет настроенных AI-провайдеров. Добавьте ключ в config.py.")

    last_error: Exception | None = None
    for name, fn in available:
        try:
            logger.debug("Trying AI provider: %s", name)
            result = fn(prompt, max_tokens)
            if name != available[0][0]:
                logger.info("AI fallback: used %s", name)
            return result
        except Exception as exc:
            logger.warning("AI provider %s failed: %s", name, exc)
            last_error = exc

    raise RuntimeError(f"Все AI-провайдеры недоступны. Последняя ошибка: {last_error}")
