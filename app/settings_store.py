"""Live settings stored in the Setting table, overlaid on env defaults."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Setting

# key -> (type, default_getter, min, max_or_none)
DEFINITIONS: dict[str, dict[str, Any]] = {
    "match_threshold": {"type": float, "min": 0.0, "max": 1.0, "default": lambda s: s.match_threshold},
    "ambiguity_margin": {"type": float, "min": 0.0, "max": 1.0, "default": lambda s: s.ambiguity_margin},
    "duplicate_threshold": {"type": float, "min": 0.0, "max": 1.0, "default": lambda s: s.match_threshold},
    "cooldown_seconds": {"type": int, "min": 0, "max": 3600, "default": lambda s: s.cooldown_seconds},
    "work_start": {"type": "time", "default": lambda s: s.work_start},
    "grace_minutes": {"type": int, "min": 0, "max": 720, "default": lambda s: s.grace_minutes},
    "review_retention_days": {"type": int, "min": 1, "max": 365, "default": lambda s: s.review_retention_days},
    "spoof_challenge": {"type": bool, "default": lambda s: False},
    "missing_close_hours": {"type": int, "min": 1, "max": 48, "default": lambda s: 4},
    "followup_late_count": {"type": int, "min": 1, "max": 365, "default": lambda s: 3},
    "followup_period_days": {"type": int, "min": 1, "max": 366, "default": lambda s: 30},
}


def _coerce(key: str, raw: str):
    spec = DEFINITIONS[key]
    t = spec["type"]
    if t is float:
        v = float(raw)
    elif t is int:
        v = int(float(raw))
    elif t is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    elif t == "time":
        parts = str(raw).strip().split(":")
        if len(parts) != 2:
            raise ValueError("expected HH:MM")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("expected HH:MM")
        return f"{h:02d}:{m:02d}"
    else:
        return raw
    if "min" in spec and v < spec["min"]:
        raise ValueError(f"must be >= {spec['min']}")
    if spec.get("max") is not None and v > spec["max"]:
        raise ValueError(f"must be <= {spec['max']}")
    return v


def get_value(db: Session, key: str):
    spec = DEFINITIONS[key]
    defaults = get_settings()
    row = db.get(Setting, key)
    raw = row.value if row is not None else spec["default"](defaults)
    return _coerce(key, str(raw))


def get_all(db: Session) -> dict[str, Any]:
    return {key: get_value(db, key) for key in DEFINITIONS}


def set_values(db: Session, updates: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    changes = []
    for key, value in updates.items():
        if key not in DEFINITIONS:
            raise KeyError(key)
        coerced = _coerce(key, str(value))
        old = get_value(db, key)
        row = db.get(Setting, key)
        stored = str(coerced)
        if row is None:
            db.add(Setting(key=key, value=stored))
        else:
            row.value = stored
        if old != coerced:
            changes.append((key, old, coerced))
    db.flush()
    return changes
