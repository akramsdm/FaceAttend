"""Sign-in rules, attempt logging and the temporary review-frame capture."""

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Attendance, RecognitionAttempt

ROOT = Path(__file__).resolve().parents[1]
REVIEW_DIR = ROOT / "var" / "review"
REVIEW_WINDOW = timedelta(minutes=2)
REVIEW_THRESHOLD = 3
REVIEW_OUTCOMES = ("rejected", "ambiguous")


def local_day_bounds(
    now_utc: datetime, timezone_name: str
) -> tuple[datetime, datetime]:
    """Return the naive-UTC start and end of the local day containing ``now_utc``."""

    zone = ZoneInfo(timezone_name)
    local_now = now_utc.replace(tzinfo=timezone.utc).astimezone(zone)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def to_local_time(naive_utc: datetime, timezone_name: str) -> datetime:
    """Attach the configured local offset so kiosk clients can format the time."""

    return naive_utc.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo(timezone_name)
    )


def attendance_within_cooldown(
    session: Session,
    staff_id: int,
    now: datetime,
    cooldown_seconds: int,
) -> Attendance | None:
    if cooldown_seconds <= 0:
        return None
    cutoff = now - timedelta(seconds=cooldown_seconds)
    return session.scalar(
        select(Attendance)
        .where(
            Attendance.staff_id == staff_id,
            Attendance.timestamp > cutoff,
        )
        .order_by(Attendance.timestamp.desc(), Attendance.id.desc())
        .limit(1)
    )


def next_action(
    session: Session,
    staff_id: int,
    now: datetime,
    timezone_name: str,
) -> str:
    """First record of the local day is 'in', the next is 'out', and so on."""

    start, end = local_day_bounds(now, timezone_name)
    count = (
        session.scalar(
            select(func.count())
            .select_from(Attendance)
            .where(
                Attendance.staff_id == staff_id,
                Attendance.timestamp >= start,
                Attendance.timestamp < end,
            )
        )
        or 0
    )
    return "in" if count % 2 == 0 else "out"


def recent_review_attempts(
    session: Session, kiosk_id: str, now: datetime
) -> int:
    cutoff = now - REVIEW_WINDOW
    return (
        session.scalar(
            select(func.count())
            .select_from(RecognitionAttempt)
            .where(
                RecognitionAttempt.kiosk_id == kiosk_id,
                RecognitionAttempt.outcome.in_(REVIEW_OUTCOMES),
                RecognitionAttempt.timestamp >= cutoff,
            )
        )
        or 0
    )


def save_review_frame(
    frame: np.ndarray,
    now: datetime,
    review_dir: Path | None = None,
) -> str | None:
    """Write one rejected frame as JPEG and return its stored path.

    Production paths are stored relative to the project root. Tests can point
    ``review_dir`` at a temporary directory, in which case an absolute path is
    returned.
    """

    directory = REVIEW_DIR if review_dir is None else review_dir
    try:
        encoded_ok, encoded = cv2.imencode(".jpg", frame)
    except cv2.error:
        return None
    if not encoded_ok:
        return None

    try:
        directory.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{now.strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(4)}.jpg"
        )
        path = directory / filename
        path.write_bytes(encoded.tobytes())
    except OSError:
        return None

    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def record_attempt(
    session: Session,
    *,
    kiosk_id: str,
    outcome: str,
    now: datetime,
    best_staff_id: int | None = None,
    score: float | None = None,
    frame: np.ndarray | None = None,
) -> RecognitionAttempt:
    """Log an attempt and queue its frame after the review threshold is reached."""

    attempt = RecognitionAttempt(
        timestamp=now,
        kiosk_id=kiosk_id,
        outcome=outcome,
        best_staff_id=best_staff_id,
        score=score,
        resolved=False,
    )
    session.add(attempt)
    session.flush()

    if outcome in REVIEW_OUTCOMES and frame is not None:
        if recent_review_attempts(session, kiosk_id, now) >= REVIEW_THRESHOLD:
            attempt.frame_path = save_review_frame(frame, now)
            attempt.resolved = False

    return attempt
