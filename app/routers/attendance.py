"""Admin attendance API: list, manual records/corrections, exports, monthly report."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.db import get_session
from app.models import Admin, Attendance, RecognitionAttempt, Staff
from app.security import require_admin, require_admin_csrf
from app.settings_store import get_all
from app.signin import local_day_bounds, to_local_time

router = APIRouter(prefix="/api/admin/attendance", tags=["attendance"])


class AttendanceRead(BaseModel):
    id: int
    staff_id: int
    staff_code: str | None
    staff_name: str | None
    department: str | None
    type: str
    timestamp: datetime
    local_time: datetime
    method: str
    edited_by: int | None
    edit_reason: str | None


class ManualAttendance(BaseModel):
    staff_id: int
    type: str = Field(pattern="^(in|out)$")
    timestamp: datetime
    reason: str = Field(min_length=1, max_length=500)


class AttendanceCorrection(BaseModel):
    type: str | None = Field(default=None, pattern="^(in|out)$")
    timestamp: datetime | None = None
    reason: str = Field(min_length=1, max_length=500)


def _parse_local_dt(value: datetime, timezone_name: str) -> datetime:
    """Interpret naive datetimes as local to TIMEZONE; return naive UTC for storage."""
    if value.tzinfo is None:
        local = value.replace(tzinfo=ZoneInfo(timezone_name))
    else:
        local = value.astimezone(ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _date_to_utc_range(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    local_start = datetime.combine(day, time.min, tzinfo=ZoneInfo(timezone_name))
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _range_bounds(
    start: date | None, end: date | None, timezone_name: str
) -> tuple[datetime | None, datetime | None]:
    lo = _date_to_utc_range(start, timezone_name)[0] if start else None
    hi = _date_to_utc_range(end, timezone_name)[1] if end else None
    return lo, hi


def _staff_or_404(db: Session, staff_id: int) -> Staff:
    staff = db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=404, detail="Staff not found")
    return staff


def _attendance_or_404(db: Session, attendance_id: int) -> Attendance:
    row = db.get(Attendance, attendance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    return row


def _row_payload(row: Attendance, staff: Staff | None, timezone_name: str) -> AttendanceRead:
    return AttendanceRead(
        id=row.id,
        staff_id=row.staff_id,
        staff_code=staff.staff_code if staff else None,
        staff_name=staff.name if staff else None,
        department=staff.department if staff else None,
        type=row.type,
        timestamp=row.timestamp,
        local_time=to_local_time(row.timestamp, timezone_name),
        method=row.method,
        edited_by=row.edited_by,
        edit_reason=row.edit_reason,
    )


@router.get("", response_model=list[AttendanceRead])
def list_attendance(
    start: date | None = None,
    end: date | None = None,
    staff_id: int | None = None,
    department: str | None = None,
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[AttendanceRead]:
    settings = get_settings()
    lo, hi = _range_bounds(start, end, settings.timezone)
    stmt = (
        select(Attendance, Staff)
        .join(Staff, Staff.id == Attendance.staff_id)
        .order_by(Attendance.timestamp.asc(), Attendance.id.asc())
    )
    if lo is not None:
        stmt = stmt.where(Attendance.timestamp >= lo)
    if hi is not None:
        stmt = stmt.where(Attendance.timestamp < hi)
    if staff_id is not None:
        stmt = stmt.where(Attendance.staff_id == staff_id)
    if department:
        stmt = stmt.where(Staff.department == department)
    return [_row_payload(row, staff, settings.timezone) for row, staff in db.execute(stmt).all()]


@router.post("", response_model=AttendanceRead, status_code=201)
def create_manual_record(
    payload: ManualAttendance,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
) -> AttendanceRead:
    settings = get_settings()
    staff = _staff_or_404(db, payload.staff_id)
    ts = _parse_local_dt(payload.timestamp, settings.timezone)
    row = Attendance(
        staff_id=staff.id,
        type=payload.type,
        timestamp=ts,
        method="manual",
        edited_by=admin.id,
        edit_reason=payload.reason.strip(),
    )
    db.add(row)
    db.flush()
    record_audit(
        db, admin.id, "manual_attendance", f"attendance:{row.id}", payload.reason.strip()
    )
    db.commit()
    db.refresh(row)
    return _row_payload(row, staff, settings.timezone)


@router.patch("/{attendance_id}", response_model=AttendanceRead)
def correct_record(
    attendance_id: int,
    payload: AttendanceCorrection,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
) -> AttendanceRead:
    settings = get_settings()
    row = _attendance_or_404(db, attendance_id)
    if payload.type is not None:
        row.type = payload.type
    if payload.timestamp is not None:
        row.timestamp = _parse_local_dt(payload.timestamp, settings.timezone)
    row.edited_by = admin.id
    row.edit_reason = payload.reason.strip()
    record_audit(
        db, admin.id, "correct_attendance", f"attendance:{row.id}", payload.reason.strip()
    )
    db.commit()
    db.refresh(row)
    staff = db.get(Staff, row.staff_id)
    return _row_payload(row, staff, settings.timezone)


def _export_rows(db: Session, start: date | None, end: date | None, settings):
    lo, hi = _range_bounds(start, end, settings.timezone)
    stmt = (
        select(Attendance, Staff)
        .join(Staff, Staff.id == Attendance.staff_id)
        .order_by(Attendance.timestamp.asc(), Attendance.id.asc())
    )
    if lo is not None:
        stmt = stmt.where(Attendance.timestamp >= lo)
    if hi is not None:
        stmt = stmt.where(Attendance.timestamp < hi)
    return db.execute(stmt).all()


@router.get("/dashboard")
def dashboard_summary(
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
) -> dict:
    settings = get_all(db)
    tz_name = get_settings().timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start, end = local_day_bounds(now, tz_name)
    zone = ZoneInfo(tz_name)

    active_total = db.scalar(
        select(func.count()).select_from(Staff).where(Staff.active.is_(True), Staff.anonymised.is_(False))
    ) or 0

    # people with an 'in' today (simple presence signal)
    present_ids = db.scalars(
        select(Attendance.staff_id)
        .where(Attendance.timestamp >= start, Attendance.timestamp < end, Attendance.type == "in")
        .distinct()
    ).all()
    present = len(present_ids)
    absent = max(0, active_total - present)

    # late arrivals: first in after work_start + grace
    work_start = _parse_work_start(str(settings.get("work_start", "09:00")))
    grace = timedelta(minutes=int(settings.get("grace_minutes", 15)))
    late = 0
    todays = db.execute(
        select(Attendance.staff_id, Attendance.timestamp, Attendance.type)
        .where(Attendance.timestamp >= start, Attendance.timestamp < end)
        .order_by(Attendance.staff_id.asc(), Attendance.timestamp.asc(), Attendance.id.asc())
    ).all()
    first_in: dict[int, datetime] = {}
    for staff_id, ts, typ in todays:
        if typ == "in" and staff_id not in first_in:
            local = ts.replace(tzinfo=timezone.utc).astimezone(zone)
            first_in[staff_id] = local
    for local in first_in.values():
        threshold = datetime.combine(local.date(), work_start, tzinfo=zone) + grace
        if local > threshold:
            late += 1

    # recognition success rate today (matched / attempts with face outcome context)
    attempts = db.scalars(
        select(RecognitionAttempt).where(RecognitionAttempt.timestamp >= start, RecognitionAttempt.timestamp < end)
    ).all()
    total_attempts = len(attempts)
    matched = sum(1 for a in attempts if a.outcome == "matched")
    success_rate = (matched / total_attempts) if total_attempts else None

    return {
        "date_local": start.replace(tzinfo=timezone.utc).astimezone(zone).date().isoformat(),
        "timezone": tz_name,
        "active_staff": active_total,
        "present_today": present,
        "absent_today": absent,
        "late_arrivals": late,
        "recognition_attempts_today": total_attempts,
        "matched_today": matched,
        "recognition_success_rate": success_rate,
    }


def export_csv(
    start: date | None = None,
    end: date | None = None,
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Response:
    settings = get_settings()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "staff_id",
            "staff_code",
            "staff_name",
            "department",
            "type",
            "timestamp_utc",
            "local_time",
            "method",
            "edited_by",
            "edit_reason",
        ]
    )
    for row, staff in _export_rows(db, start, end, settings):
        local = to_local_time(row.timestamp, settings.timezone)
        writer.writerow(
            [
                row.id,
                row.staff_id,
                staff.staff_code,
                staff.name,
                staff.department or "",
                row.type,
                row.timestamp.isoformat() + "Z",
                local.isoformat(),
                row.method,
                row.edited_by or "",
                row.edit_reason or "",
            ]
        )
    return Response(content=buf.getvalue(), media_type="text/csv")


def _parse_work_start(value: str) -> time:
    try:
        parts = value.strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except Exception:
        return time(9, 0)


def _month_bounds(year: int, month: int, timezone_name: str):
    zone = ZoneInfo(timezone_name)
    start_local = datetime(year, month, 1, tzinfo=zone)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=zone)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=zone)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
        zone,
        start_local.date(),
        (end_local - timedelta(days=1)).date(),
    )


@router.get("/report/monthly.csv")
def monthly_report(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Response:
    settings = get_settings()
    lo, hi, zone, start_day, end_day = _month_bounds(year, month, settings.timezone)
    work_start = _parse_work_start(settings.work_start)
    grace = timedelta(minutes=settings.grace_minutes)

    rows = db.execute(
        select(Attendance, Staff)
        .join(Staff, Staff.id == Attendance.staff_id)
        .where(Attendance.timestamp >= lo, Attendance.timestamp < hi)
        .order_by(Staff.id.asc(), Attendance.timestamp.asc(), Attendance.id.asc())
    ).all()

    by_staff: dict[int, dict] = {}
    for row, staff in rows:
        entry = by_staff.setdefault(
            staff.id,
            {
                "staff_code": staff.staff_code,
                "staff_name": staff.name,
                "department": staff.department or "",
                "days": {},
            },
        )
        local = row.timestamp.replace(tzinfo=timezone.utc).astimezone(zone)
        entry["days"].setattr if False else None
        entry["days"].setdefault(local.date(), []).append((local, row.type))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "staff_id",
            "staff_code",
            "staff_name",
            "department",
            "year",
            "month",
            "days_present",
            "late_arrivals",
            "hours_worked",
            "unpaired_in_days",
        ]
    )
    for staff_id in sorted(by_staff):
        entry = by_staff[staff_id]
        days_present = 0
        late_arrivals = 0
        total_seconds = 0.0
        unpaired_days: list[str] = []
        for day in sorted(entry["days"]):
            events = entry["days"][day]
            first_in = next((lt for lt, t in events if t == "in"), None)
            if first_in is not None:
                days_present += 1
                threshold = datetime.combine(day, work_start, tzinfo=zone) + grace
                if first_in > threshold:
                    late_arrivals += 1
            pending_in = None
            unpaired = False
            for local_t, typ in events:
                if typ == "in":
                    if pending_in is not None:
                        unpaired = True  # previous in never closed; flag day
                    pending_in = local_t
                elif typ == "out" and pending_in is not None:
                    delta = (local_t - pending_in).total_seconds()
                    if delta > 0:
                        total_seconds += delta
                    pending_in = None
                elif typ == "out" and pending_in is None:
                    unpaired = True
            if pending_in is not None:
                unpaired = True
            if unpaired:
                unpaired_days.append(day.isoformat())
        writer.writerow(
            [
                staff_id,
                entry["staff_code"],
                entry["staff_name"],
                entry["department"],
                year,
                month,
                days_present,
                late_arrivals,
                f"{total_seconds / 3600:.2f}",
                ";".join(unpaired_days),
            ]
        )
    return Response(content=buf.getvalue(), media_type="text/csv")
