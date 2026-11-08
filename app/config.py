import os
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_SECRET_KEY = "dev-insecure-secret-key-change-me"


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    match_threshold: float
    ambiguity_margin: float
    cooldown_seconds: int
    max_failed_attempts: int
    review_retention_days: int
    timezone: str
    min_face_width: int
    blur_threshold: float
    session_timeout_minutes: int
    lockout_minutes: int
    session_cookie_secure: bool


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=_env("DATABASE_URL", "sqlite:///./attendance.db"),
        secret_key=_env("SECRET_KEY", DEFAULT_SECRET_KEY),
        match_threshold=float(_env("MATCH_THRESHOLD", "0.363")),
        ambiguity_margin=float(_env("AMBIGUITY_MARGIN", "0.05")),
        cooldown_seconds=int(_env("COOLDOWN_SECONDS", "60")),
        max_failed_attempts=int(_env("MAX_FAILED_ATTEMPTS", "5")),
        review_retention_days=int(_env("REVIEW_RETENTION_DAYS", "7")),
        timezone=_env("TIMEZONE", "UTC"),
        min_face_width=int(_env("MIN_FACE_WIDTH", "120")),
        blur_threshold=float(_env("BLUR_THRESHOLD", "100.0")),
        session_timeout_minutes=int(_env("SESSION_TIMEOUT_MINUTES", "30")),
        lockout_minutes=int(_env("LOCKOUT_MINUTES", "5")),
        session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE"),
    )
