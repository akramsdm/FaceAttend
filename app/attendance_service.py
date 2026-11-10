"""Authoritative attendance-session lifecycle.

Every kiosk, legacy sign-in, review and approval path must go through these
helpers. The rules are intentionally server-side only:

* At most one open session per employee (``open_key`` + unique index).
* A clock-out always closes the *actual* open clock-in row and inherits its
  ``shift_id`` / ``shift_date``. It never re-resolves "today's" shift.
* A completed shift cannot be clocked out again, nor clocked in again without
  the configured permission + manager approval.
* Idempotency is provided by ``Attendance.token_jti``.

Timestamps in the database remain naive UTC (the project convention); all
helpers accept/return naive UTC and localise only for display/rule evaluation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from threading import RLock
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Attendance, Staff
from app.settings_store import get_value
from app.shifts import (
    ResolvedShift,
    evaluate,
    resolve_pattern,
    resolve_shift,
    shift_date_for,
)

# Serialises attendance mutations inside one process. SQLite + the unique
# indexes below provide the cross-process/defence-in-depth guarantee.
ATTENDANCE_LOCK = RLock()


def _shifts_data(db: Session):
    # Imported lazily to keep the pure shift module free of DB imports.
    from app.routers.shifts import _shifts_data as load

    return load(db)


def utc_to_local(naive_utc: datetime, timezone_name: str) -> datetime:
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo(timezone_name)
    ).replace(tzinfo=None)


def local_to_utc(naive_local: datetime, timezone_name: str) -> datetime:
    return naive_local.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(
        timezone.utc
    ).replace(tzinfo=None)


def get_local_now(now_utc: datetime, timezone_name: str | None = None) -> datetime:
    return utc_to_local(now_utc, timezone_name or get_settings().timezone)


def _effective_shift_date(row: Attendance, timezone_name: str) -> date | None:
    if row.shift_date is not None:
        return row.shift_date
    if row.timestamp is None:
        return None
    return utc_to_local(row.timestamp, timezone_name).date()


def find_open_session(db: Session, staff_id: int) -> Attendance | None:
    """Return the exact open clock-in row for ``staff_id``, if any.

    New rows carry ``open_key``. Legacy rows (created before this migration)
    fall back to the previous "latest in/out" scan so existing open sessions
    are not lost.
    """

    explicit = db.scalars(
        select(Attendance)
        .where(
            Attendance.staff_id == staff_id,
            Attendance.type == "in",
            Attendance.undone_at.is_(None),
            Attendance.open_key.is_not(None),
        )
        .order_by(Attendance.timestamp.desc(), Attendance.id.desc())
    ).all()
    for row in explicit:
        closed = db.scalar(
            select(Attendance.id)
            .where(
                Attendance.staff_id == staff_id,
                Attendance.type == "out",
                Attendance.undone_at.is_(None),
                Attendance.paired_in_id == row.id,
            )
            .limit(1)
        )
        if closed is None:
            return row

    # Legacy fallback: newest event decides (in = open, out = closed).
    rows = db.scalars(
        select(Attendance)
        .where(Attendance.staff_id == staff_id, Attendance.undone_at.is_(None))
        .order_by(Attendance.timestamp.desc(), Attendance.id.desc())
    ).all()
    for row in rows:
        if row.type == "in":
            return row
        if row.type == "out":
            return None
    return None


def completed_session_for_shift(
    db: Session, staff_id: int, shift_date: date | None
) -> bool:
    """True when an ``in`` has a later ``out`` for the same person + shift date."""

    if shift_date is None:
        return False
    timezone_name = get_settings().timezone
    rows = db.scalars(
        select(Attendance)
        .where(
            Attendance.staff_id == staff_id,
            Attendance.undone_at.is_(None),
            or_(
                Attendance.shift_date == shift_date,
                Attendance.shift_date.is_(None),
            ),
        )
        .order_by(Attendance.timestamp.asc(), Attendance.id.asc())
    ).all()
    seen_in: Attendance | None = None
    for row in rows:
        if _effective_shift_date(row, timezone_name) != shift_date:
            continue
        if row.type == "in" and row.flag != "missing_clock_out":
            seen_in = row
        elif (
            row.type == "out"
            and seen_in is not None
            and row.timestamp >= seen_in.timestamp
        ):
            return True
    return False


def resolve_today(
    db: Session, staff_id: int, local_now: datetime
) -> ResolvedShift | None:
    """Resolve the shift that applies to ``local_now`` (including overnight)."""

    data = _shifts_data(db)
    today = local_now.date()
    candidates: list[ResolvedShift] = []
    for day in (today, today - timedelta(days=1)):
        shift = resolve_shift(staff_id, day, data)
        if shift is None:
            continue
        if day == today or (shift.overnight and local_now <= shift.end):
            candidates.append(shift)
    if not candidates:
        return None
    for shift in candidates:
        if (
            shift.start - timedelta(minutes=shift.early_window)
            <= local_now
            <= shift.end + timedelta(hours=4)
        ):
            return shift
    return candidates[0]


NEXT_SHIFT_SEARCH_DAYS = 21


def next_shift_for(
    db: Session,
    staff_id: int,
    local_now: datetime,
    *,
    current_shift_date: date | None = None,
    search_days: int = NEXT_SHIFT_SEARCH_DAYS,
) -> ResolvedShift | None:
    """Return the next shift that starts after ``local_now``.

    Used by the kiosk after a shift has ended so the employee is told when to
    come back instead of only being blocked. The current shift (identified by
    ``current_shift_date``) is skipped even if its stored window is anomalous.
    """

    data = _shifts_data(db)
    for offset in range(0, search_days + 1):
        day = local_now.date() + timedelta(days=offset)
        if current_shift_date is not None and day == current_shift_date:
            continue
        shift = resolve_shift(staff_id, day, data)
        if shift is not None and shift.start > local_now:
            return shift
    return None


def resolve_shift_for_open_session(
    db: Session, open_in: Attendance
) -> ResolvedShift | None:
    """Rebuild the shift stored on an open session, never today's shift."""

    if open_in.shift_id is None:
        return None
    on_date = open_in.shift_date
    if on_date is None:
        on_date = utc_to_local(open_in.timestamp, get_settings().timezone).date()
    return resolve_pattern(open_in.shift_id, on_date, _shifts_data(db))


def _allow_reentry(db: Session) -> bool:
    try:
        return bool(get_value(db, "allow_reentry_same_shift"))
    except Exception:
        return False


def state_for_staff(db: Session, staff: Staff, now_utc: datetime) -> dict:
    """Return the authoritative lifecycle state for one employee.

    The returned ``evaluation`` is the value produced by :func:`app.shifts.evaluate`
    (with an explicit override for open sessions that have no shift).
    """

    timezone_name = get_settings().timezone
    local_now = utc_to_local(now_utc, timezone_name)
    open_in = find_open_session(db, staff.id)
    allow_reentry = _allow_reentry(db)

    if open_in is not None:
        shift = resolve_shift_for_open_session(db, open_in)
        shift_date = open_in.shift_date
        if shift is None:
            evaluation = {
                "allowed_actions": ["out"],
                "message": "recorded",
                "flag": "none",
                "confirm_required": False,
            }
        else:
            evaluation = evaluate(
                local_now,
                shift,
                True,
                margin_overtime=0,
                completed_session_for_shift=False,
            )
        return {
            "state": "open_session",
            "local_now": local_now,
            "open_in": open_in,
            "open_session": True,
            "shift": shift,
            "shift_date": shift_date,
            "next_shift": None,
            "completed": False,
            "allow_reentry": allow_reentry,
            "evaluation": evaluation,
        }

    shift = resolve_today(db, staff.id, local_now)
    shift_date = shift_date_for(local_now, shift) if shift is not None else local_now.date()
    completed = completed_session_for_shift(db, staff.id, shift_date)
    evaluation = evaluate(
        local_now,
        shift,
        False,
        margin_overtime=0,
        completed_session_for_shift=completed,
    )

    state = "no_session"
    if evaluation.get("message") == "shift_already_completed":
        state = "completed"
        if allow_reentry:
            evaluation = {
                **evaluation,
                "allowed_actions": ["in"],
                "message": "reentry_approval_required",
                "flag": "none",
                "reason_code": "reentry",
            }
            state = "reentry_required"
    elif evaluation.get("message") == "no_shift_request_approval":
        state = "no_shift"
    elif evaluation.get("message") == "before_window":
        state = "before_window"
    elif evaluation.get("message") == "after_shift_end":
        state = "after_shift_end"

    # After the shift has ended (or the session is already complete) tell the
    # employee when their next shift starts. This is display context only; it
    # never authorises an action.
    next_shift = None
    if completed or evaluation.get("message") == "after_shift_end":
        next_shift = next_shift_for(
            db,
            staff.id,
            local_now,
            current_shift_date=shift_date if shift is not None else None,
        )

    return {
        "state": state,
        "local_now": local_now,
        "open_in": None,
        "open_session": False,
        "shift": shift,
        "shift_date": shift_date,
        "next_shift": next_shift,
        "completed": completed,
        "allow_reentry": allow_reentry,
        "evaluation": evaluation,
    }


def reason_code_for_message(message: str | None) -> str | None:
    if message == "no_shift_request_approval":
        return "no_shift"
    if message in ("before_window", "after_shift_end", "no_open_session"):
        return "out_of_window"
    if message in ("shift_already_completed", "reentry_approval_required", "session_already_open"):
        return "reentry"
    if message == "clock_out_early_confirm":
        return "early_out"
    return None


def blocked_detail(state: dict, action: str) -> dict | None:
    """Return a 409 detail body when ``action`` is not currently legitimate."""

    open_in = state.get("open_in")
    evaluation = state.get("evaluation") or {}
    message = evaluation.get("message")

    if action == "out":
        if open_in is None:
            if state.get("completed"):
                return {
                    "status": "blocked",
                    "message": "shift_already_completed",
                    "allowed_actions": [],
                    "reason_code": "reentry",
                }
            return {
                "status": "blocked",
                "message": "no_open_session",
                "allowed_actions": [],
                "reason_code": "out_of_window",
            }
        return None

    if action == "in":
        if open_in is not None:
            return {
                "status": "blocked",
                "message": "session_already_open",
                "allowed_actions": ["out"],
                "reason_code": "reentry",
            }
        if message == "shift_already_completed":
            return {
                "status": "blocked",
                "message": "shift_already_completed",
                "allowed_actions": [],
                "reason_code": "reentry",
            }
        if message == "reentry_approval_required":
            return {
                "status": "blocked",
                "message": "reentry_approval_required",
                "allowed_actions": [],
                "reason_code": "reentry",
            }
        allowed = list(evaluation.get("allowed_actions") or [])
        if action not in allowed:
            return {
                "status": "blocked",
                "message": message or "no_shift_request_approval",
                "allowed_actions": allowed,
                "reason_code": reason_code_for_message(message),
            }
        return None

    return {
        "status": "blocked",
        "message": "invalid_action",
        "allowed_actions": [],
        "reason_code": None,
    }


def record_event(
    db: Session,
    *,
    staff_id: int,
    action: str,
    now_utc: datetime,
    shift_id: int | None,
    shift_date: date | None,
    flag: str | None = None,
    method: str = "face",
    token_jti: str | None = None,
    paired_in_id: int | None = None,
    approved_by: int | None = None,
    edited_by: int | None = None,
    edit_reason: str | None = None,
) -> Attendance:
    """Create exactly one attendance row and maintain open-session markers."""

    row = Attendance(
        staff_id=staff_id,
        type=action,
        timestamp=now_utc,
        method=method,
        shift_id=shift_id,
        shift_date=shift_date,
        flag=flag if flag and flag != "none" else None,
        token_jti=token_jti,
        paired_in_id=paired_in_id,
        approved_by=approved_by,
        edited_by=edited_by,
        edit_reason=edit_reason,
    )
    if action == "in":
        # Unique index prevents a second simultaneously-open session.
        row.open_key = str(staff_id)
    db.add(row)
    db.flush()
    return row


def close_open_session(
    db: Session,
    open_in: Attendance,
    *,
    now_utc: datetime,
    flag: str | None = None,
    method: str = "face",
    token_jti: str | None = None,
    approved_by: int | None = None,
    edited_by: int | None = None,
    edit_reason: str | None = None,
) -> Attendance:
    """Close the exact open session, inheriting its shift identity."""

    open_in.open_key = None
    return record_event(
        db,
        staff_id=open_in.staff_id,
        action="out",
        now_utc=now_utc,
        shift_id=open_in.shift_id,
        shift_date=open_in.shift_date,
        flag=flag,
        method=method,
        token_jti=token_jti,
        paired_in_id=open_in.id,
        approved_by=approved_by,
        edited_by=edited_by,
        edit_reason=edit_reason,
    )
