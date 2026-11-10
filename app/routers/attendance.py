"""Admin attendance API: list, manual records/corrections, exports, monthly report."""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.db import get_session
from app.models import Admin, Attendance, Pattern, RecognitionAttempt, Staff
from app.routers.shifts import _shifts_data, _shifts_data_for
from app.security import ROLE_ADMIN, ROLE_MANAGER, require_admin, require_admin_csrf, require_role
from app.settings_store import get_all
from app.shifts import resolve_shift
from app.signin import iso_utc, local_day_bounds, to_local_time

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
    shift_date: date | None = None
    flag: str | None = None
    edited_by: int | None
    edit_reason: str | None

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str | None:
        # Stored values are naive UTC; always expose an explicit UTC offset.
        return iso_utc(value)


class ManualAttendance(BaseModel):
    staff_id: int
    type: str = Field(pattern="^(in|out)$")
    timestamp: datetime
    reason: str = Field(min_length=1, max_length=500)


class AttendanceCorrection(BaseModel):
    type: str | None = Field(default=None, pattern="^(in|out)$")
    timestamp: datetime | None = None
    reason: str = Field(min_length=1, max_length=500)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_local_dt(value: datetime, timezone_name: str) -> datetime:
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


def _range_bounds(start: date | None, end: date | None, timezone_name: str):
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
        id=row.id, staff_id=row.staff_id,
        staff_code=staff.staff_code if staff else None,
        staff_name=staff.name if staff else None,
        department=staff.department if staff else None,
        type=row.type, timestamp=row.timestamp,
        local_time=to_local_time(row.timestamp, timezone_name),
        method=row.method,
        shift_date=getattr(row, "shift_date", None),
        flag=getattr(row, "flag", None),
        edited_by=row.edited_by, edit_reason=row.edit_reason,
    )


@router.get("", response_model=list[AttendanceRead])
def list_attendance(
    start: date | None = None, end: date | None = None,
    staff_id: int | None = None, department: str | None = None,
    _admin: Admin = Depends(require_admin), db: Session = Depends(get_session),
) -> list[AttendanceRead]:
    settings = get_settings()
    lo, hi = _range_bounds(start, end, settings.timezone)
    stmt = (select(Attendance, Staff).join(Staff, Staff.id == Attendance.staff_id)
            .order_by(Attendance.timestamp.asc(), Attendance.id.asc()))
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
    admin: Admin = Depends(require_admin_csrf), db: Session = Depends(get_session),
) -> AttendanceRead:
    settings = get_settings()
    staff = _staff_or_404(db, payload.staff_id)
    ts = _parse_local_dt(payload.timestamp, settings.timezone)
    row = Attendance(staff_id=staff.id, type=payload.type, timestamp=ts, method="manual",
                     edited_by=admin.id, edit_reason=payload.reason.strip())
    db.add(row); db.flush()
    record_audit(db, admin.id, "manual_attendance", f"attendance:{row.id}", payload.reason.strip())
    db.commit(); db.refresh(row)
    return _row_payload(row, staff, settings.timezone)


@router.patch("/{attendance_id}", response_model=AttendanceRead)
def correct_record(
    attendance_id: int, payload: AttendanceCorrection,
    admin: Admin = Depends(require_admin_csrf), db: Session = Depends(get_session),
) -> AttendanceRead:
    settings = get_settings()
    row = _attendance_or_404(db, attendance_id)
    if payload.type is not None:
        row.type = payload.type
    if payload.timestamp is not None:
        row.timestamp = _parse_local_dt(payload.timestamp, settings.timezone)
    row.edited_by = admin.id
    row.edit_reason = payload.reason.strip()
    record_audit(db, admin.id, "correct_attendance", f"attendance:{row.id}", payload.reason.strip())
    db.commit(); db.refresh(row)
    staff = db.get(Staff, row.staff_id)
    return _row_payload(row, staff, settings.timezone)


def _export_rows(db: Session, start: date | None, end: date | None, settings):
    lo, hi = _range_bounds(start, end, settings.timezone)
    stmt = (select(Attendance, Staff).join(Staff, Staff.id == Attendance.staff_id)
            .order_by(Attendance.timestamp.asc(), Attendance.id.asc()))
    if lo is not None:
        stmt = stmt.where(Attendance.timestamp >= lo)
    if hi is not None:
        stmt = stmt.where(Attendance.timestamp < hi)
    return db.execute(stmt).all()


# --- shift-aware helpers ---

def _parse_work_start(value: str) -> time:
    try:
        parts = value.strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except Exception:
        return time(9, 0)


def _is_missing(row: Attendance) -> bool:
    return getattr(row, "flag", None) == "missing_clock_out"


def _is_undone(row: Attendance) -> bool:
    return getattr(row, "undone_at", None) is not None


def _local_dt(ts: datetime, zone: ZoneInfo) -> datetime:
    return ts.replace(tzinfo=timezone.utc).astimezone(zone)


def _group_key(row: Attendance, zone: ZoneInfo):
    sd = getattr(row, "shift_date", None)
    if sd is not None:
        return row.staff_id, sd
    return row.staff_id, _local_dt(row.timestamp, zone).date()


def _month_bounds(year: int, month: int, timezone_name: str):
    zone = ZoneInfo(timezone_name)
    start_local = datetime(year, month, 1, tzinfo=zone)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=zone)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=zone)
    # include a buffer day on each side so overnight shifts starting end-of-month
    # (or whose out lands in next month) are captured; grouping by shift_date keeps them correct
    lo = (start_local - timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    hi = (end_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return lo, hi, zone, start_local.date(), (end_local - timedelta(days=1)).date()


def _resolve_for(db: Session, staff_id: int, d: date, data):
    try:
        return resolve_shift(staff_id, d, data)
    except Exception:
        return None


def _is_late(first_in_local: datetime, sh, fallback_start: time, fallback_grace: timedelta) -> bool:
    if sh is not None:
        threshold = sh.start + timedelta(minutes=sh.grace)
        # compare naive-local wall times
        a = first_in_local.replace(tzinfo=None) if first_in_local.tzinfo else first_in_local
        b = threshold.replace(tzinfo=None)
        return a > b
    threshold = datetime.combine(first_in_local.date(), fallback_start, tzinfo=first_in_local.tzinfo) + fallback_grace
    return first_in_local > threshold


@router.get("/dashboard")
def dashboard_summary(_admin: Admin = Depends(require_admin), db: Session = Depends(get_session)) -> dict:
    settings = get_all(db)
    tz_name = get_settings().timezone
    now = _utcnow_naive()
    start, end = local_day_bounds(now, tz_name)
    zone = ZoneInfo(tz_name)
    today_local = start.replace(tzinfo=timezone.utc).astimezone(zone).date()
    data = _shifts_data(db)

    active_staff = db.scalars(
        select(Staff).where(Staff.active.is_(True), Staff.anonymised.is_(False))
    ).all()
    todays = db.execute(
        select(Attendance).where(Attendance.timestamp >= start, Attendance.timestamp < end)
    ).scalars().all()

    present_ids: set[int] = set()
    for r in todays:
        if r.type == "in" and not _is_undone(r) and not _is_missing(r):
            present_ids.add(r.staff_id)

    scheduled_ids: set[int] = set()
    for st in active_staff:
        sh = _resolve_for(db, st.id, today_local, data)
        if sh is not None:
            scheduled_ids.add(st.id)

    # absent against assigned shift only (a shift was assigned)
    present_scheduled = len(present_ids & scheduled_ids)
    absent = max(0, len(scheduled_ids - present_ids))

    # late arrivals (shift start+grace; fallback global work_start/grace)
    work_start = _parse_work_start(str(settings.get("work_start", "09:00")))
    grace = timedelta(minutes=int(settings.get("grace_minutes", 15)))
    first_in: dict[int, datetime] = {}
    for r in sorted(todays, key=lambda x: (x.timestamp, x.id)):
        if r.type == "in" and not _is_undone(r) and r.staff_id not in first_in and not _is_missing(r):
            first_in[r.staff_id] = _local_dt(r.timestamp, zone)
    late = 0
    for sid, local_in in first_in.items():
        sh = _resolve_for(db, sid, today_local, data)
        if _is_late(local_in, sh, work_start, grace):
            late += 1

    attempts = db.scalars(
        select(RecognitionAttempt).where(RecognitionAttempt.timestamp >= start, RecognitionAttempt.timestamp < end)
    ).all()
    total_attempts = len(attempts)
    matched = sum(1 for a in attempts if a.outcome == "matched")
    success_rate = (matched / total_attempts) if total_attempts else None
    missing_clock_out = sum(1 for r in todays if getattr(r, "flag", None) == "missing_clock_out" and not _is_undone(r))

    return {
        "date_local": today_local.isoformat(),
        "timezone": tz_name,
        "active_staff": len(active_staff),
        "present_today": present_scheduled,
        "absent_today": absent,
        "late_arrivals": late,
        "missing_clock_out": missing_clock_out,
        "recognition_attempts_today": total_attempts,
        "matched_today": matched,
        "recognition_success_rate": success_rate,
    }


@router.get("/export.csv")
def export_csv(
    start: date | None = None, end: date | None = None,
    _admin: Admin = Depends(require_admin), db: Session = Depends(get_session),
) -> Response:
    settings = get_settings()
    buf = io.StringIO()
    writer = csv.writer(buf)
    # preserve existing columns prefix for backward compat, append shift fields
    writer.writerow([
        "id", "staff_id", "staff_code", "staff_name", "department", "type",
        "timestamp_utc", "local_time", "method", "shift_date", "flag", "edited_by", "edit_reason",
    ])
    for row, staff in _export_rows(db, start, end, settings):
        local = to_local_time(row.timestamp, settings.timezone)
        writer.writerow([
            row.id, row.staff_id, staff.staff_code, staff.name, staff.department or "", row.type,
            row.timestamp.isoformat() + "Z", local.isoformat(), row.method,
            getattr(row, "shift_date", None) or "", getattr(row, "flag", None) or "",
            row.edited_by or "", row.edit_reason or "",
        ])
    return Response(content=buf.getvalue(), media_type="text/csv")


@router.get("/report/monthly.csv")
def monthly_report(
    year: int = Query(..., ge=2000, le=2100), month: int = Query(..., ge=1, le=12),
    _admin: Admin = Depends(require_admin), db: Session = Depends(get_session),
) -> Response:
    settings = get_settings()
    lo, hi, zone, start_day, end_day = _month_bounds(year, month, settings.timezone)
    work_start = _parse_work_start(settings.work_start)
    grace = timedelta(minutes=settings.grace_minutes)
    data = _shifts_data(db)

    rows = db.execute(
        select(Attendance, Staff).join(Staff, Staff.id == Attendance.staff_id)
        .where(Attendance.timestamp >= lo, Attendance.timestamp < hi)
        .order_by(Staff.id.asc(), Attendance.timestamp.asc(), Attendance.id.asc())
    ).all()

    by_staff: dict[int, dict] = {}
    for row, staff in rows:
        entry = by_staff.setdefault(staff.id, {
            "staff_code": staff.staff_code, "staff_name": staff.name, "department": staff.department or "", "days": {}
        })
        day = _group_key(row, zone)[1]
        # only count days that fall in the requested month (by shift_date/calendar key)
        if hasattr(day, "year") and (day < start_day or day > end_day):
            continue
        entry["days"].setdefault(day, []).append(row)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "staff_id", "staff_code", "staff_name", "department", "year", "month",
        "days_present", "late_arrivals", "early_departures", "overtime_count",
        "missing_clock_outs", "hours_worked", "unpaired_in_days",
    ])
    for staff_id in sorted(by_staff):
        entry = by_staff[staff_id]
        days_present = 0
        late_arrivals = 0
        early_departures = 0
        overtime_count = 0
        missing_clock_outs = 0
        total_seconds = 0.0
        unpaired_days: list[str] = []
        for day in sorted(entry["days"]):
            ev_rows = sorted(entry["days"][day], key=lambda r: (r.timestamp, r.id))
            valid = [r for r in ev_rows if not _is_undone(r)]
            # flags
            for r in valid:
                if getattr(r, "flag", None) == "early_departure":
                    early_departures += 1
                if getattr(r, "flag", None) == "overtime":
                    overtime_count += 1
                if getattr(r, "flag", None) == "missing_clock_out":
                    missing_clock_outs += 1
            first_in = next((r for r in valid if r.type == "in" and not _is_missing(r)), None)
            sh = _resolve_for(db, staff_id, day, data)
            if first_in is not None:
                days_present += 1
                local_in = _local_dt(first_in.timestamp, zone)
                if getattr(first_in, "flag", None) == "late" or _is_late(local_in, sh, work_start, grace):
                    late_arrivals += 1
            # hours: pair in->out; missing clock-outs EXCLUDED from totals
            pending_in = None
            unpaired = False
            break_min = sh.break_min if sh is not None else 0
            for r in valid:
                if r.type == "in" and not _is_missing(r):
                    if pending_in is not None:
                        unpaired = True
                    pending_in = _local_dt(r.timestamp, zone)
                elif r.type == "out":
                    if _is_missing(r):
                        pending_in = None
                        continue
                    if pending_in is not None:
                        delta = (_local_dt(r.timestamp, zone) - pending_in).total_seconds()
                        if delta > 0:
                            worked = max(0.0, delta - break_min * 60)
                            total_seconds += worked
                        pending_in = None
                    else:
                        unpaired = True
            if pending_in is not None:
                unpaired = True
            if unpaired:
                unpaired_days.append(day.isoformat() if hasattr(day, "isoformat") else str(day))
        writer.writerow([
            staff_id, entry["staff_code"], entry["staff_name"], entry["department"], year, month,
            days_present, late_arrivals, early_departures, overtime_count,
            missing_clock_outs, f"{total_seconds / 3600:.2f}", ";".join(unpaired_days),
        ])
    return Response(content=buf.getvalue(), media_type="text/csv")


# ============================ sessions (read-time) ============================

ARRIVAL_OPTS = {"early", "on_time", "late"}
DEPARTURE_OPTS = {"early_departure", "on_time", "late_clock_out"}
SESSION_OPTS = {"in_progress", "missing_clock_out", "orphan_out"}
RECORD_OPTS = {"manual", "edited", "approved_request"}


def _role(admin: Admin) -> str:
    return getattr(admin, "role", None) or ROLE_ADMIN


def _repeat(qp, key: str) -> list[str]:
    # accept ?arrival=early&arrival=late and ?arrival=early,late
    vals = qp.getlist(key) if hasattr(qp, "getlist") else []
    out: list[str] = []
    for v in vals:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _pattern_name_map(db: Session) -> dict[int, str]:
    rows = db.execute(select(Pattern.id, Pattern.name)).all()
    return {int(i): n for i, n in rows}


def _to_local_naive(ts: datetime | None, zone: ZoneInfo) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
    return ts.astimezone(zone).replace(tzinfo=None)


def _iso_local(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def _disp(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")


def _scoped_staff_sessions(db: Session, admin: Admin, department: str | None, staff_id: int | None, search: str | None) -> list[Staff]:
    q = select(Staff).where(Staff.anonymised.is_(False))
    if _role(admin) == ROLE_MANAGER:
        scope = getattr(admin, "department_scope", None)
        q = q.where(Staff.department == scope)
        if department and scope is not None and department != scope:
            return []
    if department:
        q = q.where(Staff.department == department)
    if staff_id is not None:
        q = q.where(Staff.id == staff_id)
    if search:
        like = f"%{search.strip()}%"
        q = q.where((Staff.name.ilike(like)) | (Staff.staff_code.ilike(like)))
    return list(db.scalars(q.order_by(Staff.id)).all())


def _attendance_for_range(db: Session, staff_ids: list[int], start: date, end: date, tz_name: str) -> list[Attendance]:
    if not staff_ids:
        return []
    zone = ZoneInfo(tz_name)
    lo_local = datetime.combine(start - timedelta(days=1), time.min, tzinfo=zone)
    hi_local = datetime.combine(end + timedelta(days=2), time.min, tzinfo=zone)
    lo = lo_local.astimezone(timezone.utc).replace(tzinfo=None)
    hi = hi_local.astimezone(timezone.utc).replace(tzinfo=None)
    return list(db.scalars(
        select(Attendance)
        .where(Attendance.staff_id.in_(staff_ids), Attendance.timestamp >= lo, Attendance.timestamp < hi)
        .order_by(Attendance.staff_id, Attendance.timestamp, Attendance.id)
    ).all())


def _compute_arrival(clock_in_local: datetime | None, sh, early_arrival_minutes: int):
    if clock_in_local is None or sh is None:
        return None, None
    start = sh.start.replace(tzinfo=None)
    ts = clock_in_local.replace(tzinfo=None)
    mins = int((ts - start).total_seconds() // 60)  # floor minutes vs start (negative = early)
    early_thr = start - timedelta(minutes=int(early_arrival_minutes))
    grace_end = start + timedelta(minutes=int(sh.grace))
    if ts <= early_thr:
        status = "early"
    elif ts > grace_end:
        status = "late"
    else:
        status = "on_time"
    return status, mins


def _compute_departure(clock_out_local: datetime | None, sh, late_clockout_minutes: int):
    if clock_out_local is None or sh is None:
        return None, None
    end = sh.end.replace(tzinfo=None)
    ts = clock_out_local.replace(tzinfo=None)
    mins = int((ts - end).total_seconds() // 60)  # minutes vs end (negative = early)
    late_thr = end + timedelta(minutes=int(late_clockout_minutes))
    if ts < end:
        status = "early_departure"
    elif ts >= late_thr:
        status = "late_clock_out"
    else:
        status = "on_time"
    return status, mins


def _pair_sessions(rows: list[Attendance], zone: ZoneInfo):
    # Group events per (person, shift-date). Clock-outs created by the lifecycle
    # service carry ``paired_in_id``; use it first so a later clock-in can never
    # steal an earlier session's clock-out. Legacy rows fall back to ordering.
    by_key: dict[tuple[int, date], list[Attendance]] = defaultdict(list)
    for r in rows:
        sd = getattr(r, "shift_date", None)
        if sd is None:
            sd = _local_dt(r.timestamp, zone).date()
        by_key[(r.staff_id, sd)].append(r)
    sessions = []
    for (sid, sd), ev in by_key.items():
        evs = [r for r in sorted(ev, key=lambda x: (x.timestamp, x.id)) if not _is_undone(r)]
        paired_outs: dict[int, Attendance] = {}
        for r in evs:
            if r.type == "out" and getattr(r, "paired_in_id", None):
                paired_outs.setdefault(r.paired_in_id, r)
        used_out_ids: set[int] = set()
        for in_r in [r for r in evs if r.type == "in" and not _is_missing(r)]:
            out_r = paired_outs.get(in_r.id)
            if out_r is None:
                for cand in evs:
                    if (
                        cand.type == "out"
                        and cand.id not in used_out_ids
                        and cand.timestamp >= in_r.timestamp
                        and not getattr(cand, "paired_in_id", None)
                    ):
                        out_r = cand
                        break
            if out_r is not None:
                used_out_ids.add(out_r.id)
            sessions.append((sid, sd, in_r, out_r))
        # Any clock-out that does not close an in-session is an orphan.
        for r in evs:
            if r.type == "out" and r.id not in used_out_ids:
                sessions.append((sid, sd, None, r))
    return sessions


def _session_record(sid, sd, in_r, out_r, staff, sh, pnames, zone, early_arrival_minutes, late_clockout_minutes):
    cin = _to_local_naive(in_r.timestamp, zone) if in_r else None
    cout = _to_local_naive(out_r.timestamp, zone) if out_r else None
    # orphan out
    orphan = in_r is None and out_r is not None
    missing = (in_r is not None and out_r is not None and getattr(out_r, "flag", None) == "missing_clock_out")
    in_progress = (in_r is not None and out_r is None)
    state_tags = []
    if in_progress:
        state_tags.append("in_progress")
    if missing:
        state_tags.append("missing_clock_out")
    if orphan:
        state_tags.append("orphan_out")

    # arrival/departure: read-time; no-shift -> null; also orphan has no arrival
    arr_status = arr_mins = dep_status = dep_mins = None
    if sh is not None and not orphan and in_r is not None:
        arr_status, arr_mins = _compute_arrival(cin, sh, early_arrival_minutes)
    if sh is not None and out_r is not None and not missing:
        dep_status, dep_mins = _compute_departure(cout, sh, late_clockout_minutes)
    # if out is missing_clock_out auto-close, still no departure status (not a real clock-out)

    # duration minus break, completed pairs only (exclude missing auto-close)
    duration = None
    if in_r is not None and out_r is not None and not missing and cin and cout:
        secs = (cout - cin).total_seconds()
        if secs >= 0:
            br = int(sh.break_min) if sh is not None else 0
            duration = int(max(0, secs - br * 60) // 60)

    evs = [r for r in (in_r, out_r) if r is not None]
    edited = any(getattr(r, "edited_by", None) is not None for r in evs)
    reason = next((getattr(r, "edit_reason", None) for r in evs if getattr(r, "edit_reason", None)), None)
    method = in_r.method if in_r is not None else (out_r.method if out_r else None)
    approved = any(getattr(r, "approved_by", None) is not None for r in evs)

    record_tags = []
    if method == "manual":
        record_tags.append("manual")
    if edited:
        record_tags.append("edited")
    if approved:
        record_tags.append("approved_request")

    shift_name = pnames.get(getattr(sh, "pattern_id", None) or getattr(sh, "shift_id", None) or (getattr(in_r, "shift_id", None) if in_r else getattr(out_r, "shift_id", None))) if sh else None
    # resolve pattern name via shift_id on events as fallback
    sid_for_name = (getattr(in_r, "shift_id", None) if in_r else None) or (getattr(out_r, "shift_id", None) if out_r else None)
    if not shift_name and sid_for_name:
        shift_name = pnames.get(int(sid_for_name))

    return {
        "staff_id": staff.id, "name": staff.name, "staff_code": staff.staff_code, "department": staff.department,
        "shift_date": sd.isoformat() if sd else None,
        "shift_name": shift_name,
        "shift_start": _iso_local(sh.start.replace(tzinfo=None) if sh else None),
        "shift_end": _iso_local(sh.end.replace(tzinfo=None) if sh else None),
        "clock_in": _iso_local(cin), "clock_in_display": _disp(cin),
        "clock_out": _iso_local(cout), "clock_out_display": _disp(cout),
        "duration_minutes": duration,
        "arrival_status": arr_status, "minutes_vs_start": arr_mins,
        "departure_status": dep_status, "minutes_vs_end": dep_mins,
        "state_tags": state_tags,
        "edited": edited, "reason": reason, "method": method,
        "record_tags": record_tags,
        "event_ids": [r.id for r in evs],
    }


def _matches_filters(rec, arrival, departure, session_f, record_f, arrival_min, departure_min):
    # AND across groups, OR within group. No filter in a group => that group passes.
    if arrival:
        if rec["arrival_status"] is None or rec["arrival_status"] not in set(arrival):
            return False
    if departure:
        if rec["departure_status"] is None or rec["departure_status"] not in set(departure):
            return False
    if session_f:
        if not (set(rec["state_tags"]) & set(session_f)):
            return False
    if record_f:
        if not (set(rec["record_tags"]) & set(record_f)):
            return False
    if arrival_min is not None:
        st = rec["arrival_status"]; mv = rec["minutes_vs_start"]
        if st is None or mv is None:
            return False
        if st == "early":
            ok = (-mv) >= int(arrival_min)
        elif st == "late":
            ok = mv >= int(arrival_min)
        else:
            return False
        if not ok:
            return False
    if departure_min is not None:
        st = rec["departure_status"]; mv = rec["minutes_vs_end"]
        if st is None or mv is None:
            return False
        if st == "early_departure":
            ok = (-mv) >= int(departure_min)
        elif st == "late_clock_out":
            ok = mv >= int(departure_min)
        else:
            return False
        if not ok:
            return False
    return True


def _day_summary(recs):
    c = defaultdict(int)
    for r in recs:
        if r.get("arrival_status"): c[f"arrival:{r['arrival_status']}"] += 1
        if r.get("departure_status"): c[f"departure:{r['departure_status']}"] += 1
        for t in r.get("state_tags") or []: c[f"session:{t}"] += 1
        for t in r.get("record_tags") or []: c[f"record:{t}"] += 1
    return dict(c)


def _build_sessions(db: Session, admin: Admin, from_d, to_d, department, staff_id, search, method,
                    days_per_page, page, arrival, departure, session_f, record_f, arrival_min, departure_min):
    if from_d is None or to_d is None:
        raise HTTPException(status_code=422, detail="from and to are required")
    if to_d < from_d:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_d - from_d).days > 61:
        raise HTTPException(status_code=422, detail="date range exceeds 62 days")
    days_per_page = max(1, min(int(days_per_page or 7), 62))
    page = max(1, int(page or 1))

    settings = get_all(db)
    tz_name = get_settings().timezone
    early_thr = int(settings.get("early_arrival_minutes", 15) or 15)
    late_out_thr = int(settings.get("late_clockout_minutes", 15) or 15)
    zone = ZoneInfo(tz_name)

    all_days = []
    cur = from_d
    while cur <= to_d:
        all_days.append(cur)
        cur += timedelta(days=1)
    total_days = len(all_days)
    start_i = (page - 1) * days_per_page
    page_days = all_days[start_i:start_i + days_per_page]
    page_from = page_days[0] if page_days else from_d
    page_to = page_days[-1] if page_days else to_d

    # constant query set: staff, settings(already), shifts data for scoped staff over page range, attendance over page range, pattern names
    people = _scoped_staff_sessions(db, admin, department, staff_id, search)
    ids = [s.id for s in people]
    staff_by = {s.id: s for s in people}
    data = _shifts_data_for(db, ids, page_from, page_to)
    pnames = _pattern_name_map(db)
    att_rows = _attendance_for_range(db, ids, page_from, page_to, tz_name)
    # method filter applied post-pairing via record/session context below? keep broad load; filter on session
    _method_filter = method

    paired = _pair_sessions(att_rows, zone)
    recs = []
    for sid, sd, in_r, out_r in paired:
        stf = staff_by.get(sid)
        if stf is None:
            continue
        sh = _resolve_for(db, sid, sd, data)
        rec = _session_record(sid, sd, in_r, out_r, stf, sh, pnames, zone, early_thr, late_out_thr)
        if _method_filter and not any(getattr(rr, "method", None) == _method_filter for rr in (in_r, out_r) if rr):
            continue
        if _matches_filters(rec, arrival, departure, session_f, record_f, arrival_min, departure_min):
            recs.append(rec)

    # group by day (paginate by day)
    by_day = defaultdict(list)
    for r in recs:
        by_day[r["shift_date"]].append(r)
    days_out = []
    for d in page_days:
        dk = d.isoformat()
        dr = sorted(by_day.get(dk, []), key=lambda x: (x["staff_id"], x["clock_in"] or "", x["event_ids"][0] if x["event_ids"] else 0))
        days_out.append({"date": dk, "summary": _day_summary(dr), "sessions": dr})

    return {
        "from": from_d.isoformat(), "to": to_d.isoformat(),
        "timezone": tz_name,
        "page": page, "days_per_page": days_per_page, "total_days": total_days,
        "early_arrival_minutes": early_thr, "late_clockout_minutes": late_out_thr,
        "filters": {"arrival": arrival, "departure": departure, "session": session_f, "record": record_f,
                    "arrival_min_minutes": arrival_min, "departure_min_minutes": departure_min,
                    "department": department, "staff_id": staff_id, "search": search, "method": method},
        "days": days_out,
    }


@router.get("/sessions")
def list_sessions(
    request: Request,
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    department: str | None = None,
    staff_id: int | None = None,
    search: str | None = None,
    method: str | None = None,
    days_per_page: int = Query(7, ge=1, le=62),
    page: int = Query(1, ge=1),
    arrival_min_minutes: int | None = Query(None, ge=0),
    departure_min_minutes: int | None = Query(None, ge=0),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    arrival = _repeat(request.query_params, "arrival")
    departure = _repeat(request.query_params, "departure")
    session_f = _repeat(request.query_params, "session")
    record_f = _repeat(request.query_params, "record")
    return _build_sessions(db, admin, from_d, to_d, department, staff_id, search, method,
                           days_per_page, page, arrival, departure, session_f, record_f,
                           arrival_min_minutes, departure_min_minutes)


@router.get("/sessions.csv")
def sessions_csv(
    request: Request,
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    department: str | None = None,
    staff_id: int | None = None,
    search: str | None = None,
    method: str | None = None,
    arrival_min_minutes: int | None = Query(None, ge=0),
    departure_min_minutes: int | None = Query(None, ge=0),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    arrival = _repeat(request.query_params, "arrival")
    departure = _repeat(request.query_params, "departure")
    session_f = _repeat(request.query_params, "session")
    record_f = _repeat(request.query_params, "record")
    # CSV exports all days in range (no day pagination) but still capped at 62
    payload = _build_sessions(db, admin, from_d, to_d, department, staff_id, search, method,
                              days_per_page=62, page=1, arrival=arrival, departure=departure,
                              session_f=session_f, record_f=record_f,
                              arrival_min=arrival_min_minutes, departure_min=departure_min_minutes)
    headers = [
        "shift_date","staff_id","staff_code","name","department","shift_name","shift_start","shift_end",
        "clock_in","clock_out","duration_minutes","arrival_status","minutes_vs_start",
        "departure_status","minutes_vs_end","state_tags","edited","reason","method","event_ids",
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for day in payload["days"]:
        for s in day["sessions"]:
            w.writerow([
                s.get("shift_date"), s.get("staff_id"), s.get("staff_code") or "", s.get("name") or "",
                s.get("department") or "", s.get("shift_name") or "", s.get("shift_start") or "", s.get("shift_end") or "",
                s.get("clock_in") or "", s.get("clock_out") or "",
                (s.get("duration_minutes") if s.get("duration_minutes") is not None else ""),
                s.get("arrival_status") or "", (s.get("minutes_vs_start") if s.get("minutes_vs_start") is not None else ""),
                s.get("departure_status") or "", (s.get("minutes_vs_end") if s.get("minutes_vs_end") is not None else ""),
                "|".join(s.get("state_tags") or []), "yes" if s.get("edited") else "no",
                s.get("reason") or "", s.get("method") or "", "|".join(str(i) for i in (s.get("event_ids") or [])),
            ])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="sessions-{payload["from"]}-to-{payload["to"]}.csv"'})
