from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    anthropic_api_key: str | None
    request_delay_seconds: float = 1.25
    request_timeout_seconds: int = 30
    user_agent: str = "mma-ingesta/1.0 (+https://ufcstats.com)"
    promotion_id_ufc: int = 1
    source_name: str = "ufcstats"


def get_settings() -> Settings:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in the environment or .env file.")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    return Settings(database_url=database_url, anthropic_api_key=anthropic_api_key)