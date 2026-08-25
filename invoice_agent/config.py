import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_chat_ids: tuple[int, ...]
    telegram_webhook_secret: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    database_path: Path
    output_dir: Path
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        required = (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_CHAT_IDS",
            "TELEGRAM_WEBHOOK_SECRET",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
        )
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise ValueError(f"Missing environment variable: {missing[0]}")
        try:
            allowed_chat_ids = tuple(
                dict.fromkeys(
                    int(value.strip())
                    for value in values["TELEGRAM_ALLOWED_CHAT_IDS"].split(",")
                    if value.strip()
                )
            )
        except ValueError as error:
            raise ValueError("Invalid TELEGRAM_ALLOWED_CHAT_IDS") from error
        if not allowed_chat_ids:
            raise ValueError("Invalid TELEGRAM_ALLOWED_CHAT_IDS")
        return cls(
            telegram_bot_token=values["TELEGRAM_BOT_TOKEN"],
            telegram_allowed_chat_ids=allowed_chat_ids,
            telegram_webhook_secret=values["TELEGRAM_WEBHOOK_SECRET"],
            openai_api_key=values["OPENAI_API_KEY"],
            openai_base_url=values["OPENAI_BASE_URL"],
            openai_model=values["OPENAI_MODEL"],
            database_path=Path(values.get("DATABASE_PATH", "invoice_agent.db")),
            output_dir=Path(values.get("OUTPUT_DIR", "generated")),
            log_level=values.get("LOG_LEVEL", "INFO").upper(),
        )
