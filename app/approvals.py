"""Approval queue and missing clock-out session closing (Phase 2 / Step I)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.models import ApprovalRequest, Attendance, Staff
from app.routers.shifts import _shifts_data
from app.settings_store import get_value
from app.shifts import resolve_shift, shift_date_for

MISSING_CLOSE_HOURS_DEFAULT = 4


def _localize(naive_utc: datetime, tz: str) -> datetime:
    from datetime import timezone as _tz
    return naive_utc.replace(tzinfo=_tz.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def _resolve_for_date(db: Session, staff_id: int, d):
    data = _shifts_data(db)
    return resolve_shift(staff_id, d, data)


def approval_flag(reason_code: str) -> str | None:
    if reason_code in ("out_of_window", "no_shift"):
        return "out_of_window"
    if reason_code == "early_out":
        return "early_departure"
    return None


def create_attendance_for_approval(db: Session, req: ApprovalRequest, *, admin_id: int, now: datetime) -> Attendance:
    tz = get_settings().timezone
    local = _localize(now, tz)
    d = local.date()
    sh = _resolve_for_date(db, req.staff_id, d)
    sdate = shift_date_for(local, sh) if sh is not None else d
    flag = approval_flag(req.reason_code)
    att = Attendance(
        staff_id=req.staff_id,
        type=req.action,
        timestamp=now,
        method="manual",
        shift_id=None,
        shift_date=sdate,
        flag=flag,
        approved_by=admin_id,
        edit_reason=f"approved_request:{req.id}",
    )
    db.add(att)
    db.flush()
    return att


def close_missing_sessions(db: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Close open sessions whose shift ended > missing_close_hours ago.

    Marks an auto 'out' row with flag missing_clock_out. Missing clock-outs are
    never counted as worked time automatically (reports must exclude them).
    Idempotent: skips staff who already have a missing_clock_out auto-close after
    their last open 'in'.
    """
    settings = get_settings()
    now = now or datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None)
    try:
        close_hours = int(get_value(db, "missing_close_hours"))
    except Exception:
        close_hours = MISSING_CLOSE_HOURS_DEFAULT
    tz = settings.timezone
    local_now = _localize(now, tz)
    # candidate open 'in' rows (recent window)
    cutoff = now - timedelta(days=7)
    ins = db.scalars(
        select(Attendance)
        .where(Attendance.type == "in", Attendance.undone_at.is_(None), Attendance.timestamp >= cutoff)
        .order_by(Attendance.timestamp.asc())
    ).all()
    closed: list[dict[str, Any]] = []
    for a in ins:
        # is there any out/closure after this in?
        later = db.scalar(
            select(Attendance.id).where(
                Attendance.staff_id == a.staff_id,
                Attendance.undone_at.is_(None),
                Attendance.timestamp > a.timestamp,
                Attendance.type == "out",
            ).order_by(Attendance.timestamp.asc()).limit(1)
        )
        if later is not None:
            continue
        # resolve shift for that in
        a_local = _localize(a.timestamp, tz)
        sh = _resolve_for_date(db, a.staff_id, a_local.date())
        if sh is None and a.shift_date is not None:
            sh = _resolve_for_date(db, a.staff_id, a.shift_date)
        if sh is None:
            continue  # cannot determine shift end safely
        cut_at = sh.end + timedelta(hours=close_hours)
        # close at the exact cut-off (and also if now is past it)
        if local_now < cut_at:
            continue
        # convert cut_at local-naive to naive UTC for storage
        from datetime import timezone as _tz
        close_utc = cut_at.replace(tzinfo=ZoneInfo(tz)).astimezone(_tz.utc).replace(tzinfo=None)
        # idempotency: already auto-closed for this in?
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
        sdate = shift_date_for(a_local, sh)
        row = Attendance(
            staff_id=a.staff_id,
            type="out",
            timestamp=close_utc,
            method="manual",
            shift_id=None,
            shift_date=sdate,
            flag="missing_clock_out",
            edit_reason=f"auto_missing:{a.id}",
        )
        db.add(row)
        db.flush()
        record_audit(db, None, "auto_close_missing_clockout", f"staff:{a.staff_id}", f"in:{a.id} out:{row.id}")
        closed.append({"staff_id": a.staff_id, "in_id": a.id, "out_id": row.id, "shift_date": str(sdate)})
    if closed:
        db.commit()
    return closed
