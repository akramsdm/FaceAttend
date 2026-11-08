"""Delete expired review frames and clear their DB references."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RecognitionAttempt
from app.signin import ROOT

REVIEW_DIR = ROOT / "var" / "review"


def _resolve(path_str: str, review_dir: Path) -> Path | None:
    p = Path(path_str)
    if p.is_absolute():
        try:
            p.relative_to(review_dir.resolve())
            return p
        except ValueError:
            # allow absolute paths inside review_dir by string
            return p if review_dir.resolve() in p.resolve().parents or p.resolve() == review_dir.resolve() else p
    return (ROOT / p).resolve()


def cleanup_review_frames(
    db: Session,
    retention_days: int,
    now: datetime | None = None,
    review_dir: Path | None = None,
) -> int:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=retention_days)
    directory = review_dir or REVIEW_DIR
    directory.mkdir(parents=True, exist_ok=True)

    deleted = 0
    # DB-referenced expired frames
    attempts = db.scalars(
        select(RecognitionAttempt).where(
            RecognitionAttempt.frame_path.is_not(None),
            RecognitionAttempt.timestamp < cutoff,
        )
    ).all()
    for attempt in attempts:
        path = _resolve(attempt.frame_path, directory)
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        attempt.frame_path = None
        deleted += 1

    # orphan files older than retention
    try:
        for child in directory.iterdir():
            if not child.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    child.unlink()
                    deleted += 1
                except OSError:
                    pass
    except OSError:
        pass

    db.commit()
    return deleted
