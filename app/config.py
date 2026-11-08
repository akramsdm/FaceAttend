import os
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_SECRET_KEY = "dev-insecure-secret-key-change-me"


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    match_threshold: float
    cooldown_seconds: int
    max_failed_attempts: int
    review_retention_days: int
    timezone: str


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=_env("DATABASE_URL", "sqlite:///./attendance.db"),
        secret_key=_env("SECRET_KEY", DEFAULT_SECRET_KEY),
        match_threshold=float(_env("MATCH_THRESHOLD", "0.363")),
        cooldown_seconds=int(_env("COOLDOWN_SECONDS", "60")),
        max_failed_attempts=int(_env("MAX_FAILED_ATTEMPTS", "5")),
        review_retention_days=int(_env("REVIEW_RETENTION_DAYS", "7")),
        timezone=_env("TIMEZONE", "UTC"),
    )
