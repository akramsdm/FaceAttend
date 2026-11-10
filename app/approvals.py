"""Approval queue and missing clock-out session closing (Phase 2 / Step I)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.attendance_service import (
    close_open_session,
    find_open_session,
    record_event,
    resolve_shift_for_open_session,
    resolve_today,
)
from app.audit import record_audit
from app.config import get_settings
from app.models import ApprovalRequest, Attendance
from app.settings_store import get_value
from app.shifts import shift_date_for

MISSING_CLOSE_HOURS_DEFAULT = 4


def _localize(naive_utc: datetime, tz: str) -> datetime:
    from datetime import timezone as _tz

    return naive_utc.replace(tzinfo=_tz.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def approval_flag(reason_code: str) -> str | None:
    if reason_code in ("out_of_window", "no_shift", "reentry"):
        return "out_of_window"
    if reason_code == "early_out":
        return "early_departure"
    return None


def create_attendance_for_approval(
    db: Session, req: ApprovalRequest, *, admin_id: int, now: datetime
) -> Attendance:
    """Create the single attendance row authorised by a manager approval.

    Approvals are explicit, but they still may not corrupt the lifecycle:
    an approved clock-out must close the *actual* open session, and an
    approved clock-in must not create a second open session.
    """

    tz = get_settings().timezone
    local = _localize(now, tz)
    flag = approval_flag(req.reason_code)

    if req.action == "out":
        open_in = find_open_session(db, req.staff_id)
        if open_in is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "blocked",
                    "message": "no_open_session",
                    "reason_code": "out_of_window",
                },
            )
        att = close_open_session(
            db,
            open_in,
            now_utc=now,
            flag=flag,
            method="manual",
            approved_by=admin_id,
            edit_reason=f"approved_request:{req.id}",
        )
        return att

    # Approved clock-in (no-shift, out-of-window, or explicit re-entry).
    if find_open_session(db, req.staff_id) is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "status": "blocked",
                "message": "session_already_open",
                "allowed_actions": ["out"],
                "reason_code": "reentry",
            },
        )

    shift = resolve_today(db, req.staff_id, local)
    sdate = shift_date_for(local, shift) if shift is not None else local.date()
    return record_event(
        db,
        staff_id=req.staff_id,
        action="in",
        now_utc=now,
        shift_id=getattr(shift, "pattern_id", None),
        shift_date=sdate,
        flag=flag,
        method="manual",
        approved_by=admin_id,
        edit_reason=f"approved_request:{req.id}",
    )


def close_missing_sessions(db: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Close open sessions whose shift ended > missing_close_hours ago.

    Marks an auto 'out' row with flag missing_clock_out. Missing clock-outs are
    never counted as worked time automatically (reports must exclude them).
    Idempotent: skips staff who already have a missing_clock_out auto-close after
    their last open 'in'.

    The auto-close inherits the open session's ``shift_id``/``shift_date`` and
    links via ``paired_in_id`` so it can never be mistaken for an orphan out.
    """

    settings = get_settings()
    now = now or datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None)
    try:
        close_hours = int(get_value(db, "missing_close_hours"))
    except Exception:
        close_hours = MISSING_CLOSE_HOURS_DEFAULT
    tz = settings.timezone
    local_now = _localize(now, tz)
    cutoff = now - timedelta(days=7)
    ins = db.scalars(
        select(Attendance)
        .where(
            Attendance.type == "in",
            Attendance.undone_at.is_(None),
            Attendance.timestamp >= cutoff,
        )
        .order_by(Attendance.timestamp.asc())
    ).all()
    closed: list[dict[str, Any]] = []
    for a in ins:
        # Only consider rows that are still the open session for this person.
        open_in = find_open_session(db, a.staff_id)
        if open_in is None or open_in.id != a.id:
            continue

        a_local = _localize(a.timestamp, tz)
        sh = resolve_shift_for_open_session(db, a)
        if sh is None:
            # Fall back to resolving the stored shift date (legacy rows without
            # shift_id) so existing sessions still get closed correctly.
            from app.routers.shifts import _shifts_data
            from app.shifts import resolve_shift

            on_date = a.shift_date or a_local.date()
            sh = resolve_shift(a.staff_id, on_date, _shifts_data(db))
        if sh is None:
            continue  # cannot determine shift end safely

        cut_at = sh.end + timedelta(hours=close_hours)
        if local_now < cut_at:
            continue

        # Never auto-close before the session actually started. Legacy rows may
        # have been clocked in after the shift end (the new after_shift_end
        # policy blocks those), so the policy cut can precede the clock-in.
        # Writing an 'out' before its 'in' would create a negative session;
        # leave those open for a manager to correct instead.
        if a_local >= cut_at:
            continue

        from datetime import timezone as _tz

        close_utc = cut_at.replace(tzinfo=ZoneInfo(tz)).astimezone(_tz.utc).replace(tzinfo=None)

        # Idempotency: already auto-closed for this in?
        existing = db.scalar(
            select(Attendance.id).where(
                Attendance.staff_id == a.staff_id,
                Attendance.type == "out",
                Attendance.flag == "missing_clock_out",
                Attendance.edit_reason == f"auto_missing:{a.id}",
            )
        )
        if existing is not None:
            continue

        row = close_open_session(
            db,
            a,
            now_utc=close_utc,
            flag="missing_clock_out",
            method="manual",
            edit_reason=f"auto_missing:{a.id}",
        )
        record_audit(
            db,
            None,
            "auto_close_missing_clockout",
            f"staff:{a.staff_id}",
            f"in:{a.id} out:{row.id}",
        )
        closed.append(
            {
                "staff_id": a.staff_id,
                "in_id": a.id,
                "out_id": row.id,
                "shift_date": str(row.shift_date),
            }
        )
    if closed:
        db.commit()
    return closed
