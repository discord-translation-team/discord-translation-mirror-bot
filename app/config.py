from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    database_url: str = "sqlite+aiosqlite:///./bot.db"
    translation_provider: str = "mock"
    gemini_api_key: str = ""
    gemini_translation_model: str = "gemini-2.5-flash-lite"
    openai_api_key: str = ""
    openai_translation_model: str = "gpt-5.4-mini"
    openai_translation_quality_model: str = "gpt-5.4-mini"
    default_monthly_char_limit: int = 500_000
    max_message_chars: int = 1_500
    skip_messages_over_limit: bool = True
    log_level: str = "INFO"


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")

    def parse_bool(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}

    return Settings(
        discord_bot_token=token,
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db").strip(),
        translation_provider=os.getenv("TRANSLATION_PROVIDER", "mock").strip().lower(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_translation_model=os.getenv("GEMINI_TRANSLATION_MODEL", "gemini-2.5-flash-lite").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_translation_model=os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-5.4-mini").strip(),
        openai_translation_quality_model=os.getenv("OPENAI_TRANSLATION_QUALITY_MODEL", "gpt-5.4-mini").strip(),
        default_monthly_char_limit=int(os.getenv("DEFAULT_MONTHLY_CHAR_LIMIT", "500000")),
        max_message_chars=int(os.getenv("MAX_MESSAGE_CHARS", "1500")),
        skip_messages_over_limit=parse_bool(os.getenv("SKIP_MESSAGES_OVER_LIMIT", "true")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, self.datefmt),
        }

        for key, value in record.__dict__.items():
            if key.startswith("_") or key in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
