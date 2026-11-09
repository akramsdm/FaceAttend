"""Admin/manager metrics API (Step 2B). Department scope enforced.

All endpoints accept from/to (YYYY-MM-DD), capped at 366 days inclusive, and an
optional department filter (further restricts, never widens, manager scope).
Uses a constant number of queries per endpoint (independent of staff count,
aside from small IN-bounded loads over the already-scoped staff set).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_session
from app.metrics import (
    arrival_histogram as m_arrival_histogram,
    compute_insights as m_compute_insights,
    daily_series as m_daily_series,
    late_arrival_hours as m_late_arrival_hours,
    lateness_by_weekday as m_lateness_by_weekday,
    person_metrics as m_person_metrics,
    team_metrics as m_team_metrics,
    top_n as m_top_n,
    _prev_equal_period,
)
from app.models import Admin, ApprovalRequest, Assignment, Attendance, Pattern, PatternDay, RecognitionAttempt, Setting, ShiftException, Staff
from app.security import ROLE_ADMIN, ROLE_MANAGER, require_role
from app.settings_store import DEFINITIONS, get_all, _coerce
from app.shifts import ResolvedShift, resolve_shift
from app.signin import local_day_bounds

router = APIRouter(prefix="/api/admin/metrics", tags=["metrics"])

MAX_DAYS = 366


def _role(admin: Admin) -> str:
    return getattr(admin, "role", None) or ROLE_ADMIN


def _parse_range(from_d: date, to_d: date) -> tuple[date, date]:
    if to_d < from_d:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_d - from_d).days > (MAX_DAYS - 1):
        raise HTTPException(status_code=422, detail=f"date range exceeds {MAX_DAYS} days")
    return from_d, to_d


def _scoped_staff(db: Session, admin: Admin, department: str | None) -> list[Staff]:
    q = select(Staff).where(Staff.active.is_(True), Staff.anonymised.is_(False))
    if _role(admin) == ROLE_MANAGER:
        scope = getattr(admin, "department_scope", None)
        q = q.where(Staff.department == scope)
        # a manager-supplied department filter may only narrow within scope
        if department and scope is not None and department != scope:
            return []
    if department:
        q = q.where(Staff.department == department)
    return list(db.scalars(q.order_by(Staff.id)).all())


def _pattern_data(p: Pattern) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "owner_staff_id": p.owner_staff_id,
        "early_window_min": p.early_window_min,
        "grace_min": p.grace_min,
        "break_min": p.break_min,
        "is_default": p.is_default,
        "days": [
            {
                "weekday": d.weekday,
                "start_time": d.start_time,
                "end_time": d.end_time,
                "overnight": d.overnight,
                "enabled": d.enabled,
            }
            for d in sorted(p.days, key=lambda x: x.weekday)
        ],
    }


def _shifts_data_for(db: Session, staff_ids: list[int], start: date, end: date) -> dict[str, Any]:
    """Bounded shifts data for resolve_shift (constant query count)."""
    pats = list(db.scalars(select(Pattern).options(selectinload(Pattern.days))).all())
    patterns = [_pattern_data(p) for p in pats]
    if not staff_ids:
        return {"patterns": patterns, "assignments": [], "exceptions": []}
    assigns = [
        {
            "staff_id": a.staff_id,
            "pattern_id": a.pattern_id,
            "effective_from": a.effective_from,
            "effective_to": a.effective_to,
        }
        for a in db.scalars(
            select(Assignment).where(
                Assignment.staff_id.in_(staff_ids),
                (Assignment.effective_to.is_(None)) | (Assignment.effective_to >= start),
                Assignment.effective_from <= end,
            )
        ).all()
    ]
    exs = [
        {
            "staff_id": e.staff_id,
            "date": e.date,
            "kind": e.kind,
            "start_time": e.start_time,
            "end_time": e.end_time,
        }
        for e in db.scalars(
            select(ShiftException).where(
                ShiftException.staff_id.in_(staff_ids),
                ShiftException.date >= start,
                ShiftException.date <= end,
            )
        ).all()
    ]
    return {"patterns": patterns, "assignments": assigns, "exceptions": exs}


def _attendance_rows(db: Session, staff_ids: list[int], start: date, end: date, tz_name: str) -> list[dict[str, Any]]:
    if not staff_ids:
        return []
    zone = ZoneInfo(tz_name)
    lo_local = datetime.combine(start - timedelta(days=1), time.min, tzinfo=zone)
    hi_local = datetime.combine(end + timedelta(days=2), time.min, tzinfo=zone)
    lo = lo_local.astimezone(timezone.utc).replace(tzinfo=None)
    hi = hi_local.astimezone(timezone.utc).replace(tzinfo=None)
    rows = db.scalars(
        select(Attendance)
        .where(Attendance.staff_id.in_(staff_ids), Attendance.timestamp >= lo, Attendance.timestamp < hi)
        .order_by(Attendance.staff_id, Attendance.timestamp, Attendance.id)
    ).all()
    out = []
    for r in rows:
        ts = r.timestamp
        if ts is not None and ts.tzinfo is None:
            ts_local = ts.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
        elif ts is not None:
            ts_local = ts.astimezone(zone).replace(tzinfo=None)
        else:
            ts_local = None
        out.append(
            {
                "id": r.id,
                "staff_id": r.staff_id,
                "type": r.type,
                "timestamp": ts_local,  # local naive for metric comparisons
                "shift_date": getattr(r, "shift_date", None),
                "flag": getattr(r, "flag", None),
                "undone_at": getattr(r, "undone_at", None),
            }
        )
    return out


def _pending_approvals(db: Session, staff_ids: list[int]) -> int:
    if not staff_ids:
        return 0
    return int(
        db.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.staff_id.in_(staff_ids), ApprovalRequest.status == "pending")
        )
        or 0
    )


def _today_kpis(db: Session, staff_ids: list[int], people: list[Staff], data: dict[str, Any], tz_name: str) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    start_utc, end_utc = local_day_bounds(now_utc, tz_name)
    zone = ZoneInfo(tz_name)
    today_local = start_utc.replace(tzinfo=timezone.utc).astimezone(zone).date()
    if not staff_ids:
        return {
            "date": today_local.isoformat(),
            "present": 0,
            "absent_so_far": 0,
            "late": 0,
            "currently_clocked_in": 0,
            "missing_clock_outs": 0,
            "pending_approvals": 0,
        }
    todays = db.scalars(
        select(Attendance)
        .where(Attendance.staff_id.in_(staff_ids), Attendance.timestamp >= start_utc, Attendance.timestamp < end_utc)
        .order_by(Attendance.staff_id, Attendance.timestamp, Attendance.id)
    ).all()
    by_sid: dict[int, list[Attendance]] = defaultdict(list)
    for r in todays:
        by_sid[r.staff_id].append(r)

    present = 0
    absent_so_far = 0
    late = 0
    clocked_in = 0
    missing_out = 0
    now_local_naive = now_utc.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
    for st in people:
        sid = st.id
        sh = None
        try:
            sh = resolve_shift(sid, today_local, data)
        except Exception:
            sh = None
        ev = [r for r in by_sid.get(sid, []) if getattr(r, "undone_at", None) is None]
        has_in = any(r.type == "in" and getattr(r, "flag", None) != "missing_clock_out" for r in ev)
        missing_out += sum(1 for r in ev if getattr(r, "flag", None) == "missing_clock_out")
        if sh is not None and has_in:
            present += 1
        # late: first valid in after grace
        first_in = next((r for r in ev if r.type == "in" and getattr(r, "flag", None) != "missing_clock_out"), None)
        if first_in is not None and sh is not None:
            ts = first_in.timestamp.replace(tzinfo=None) if first_in.timestamp else None
            if ts is not None:
                grace_end = sh.start.replace(tzinfo=None) + timedelta(minutes=int(sh.grace))
                if getattr(first_in, "flag", None) == "late" or ts > grace_end:
                    late += 1
        # absent so far: scheduled, shift end passed, no in
        if sh is not None and now_local_naive > sh.end.replace(tzinfo=None) and not has_in:
            absent_so_far += 1
        # currently clocked in: last valid event is an in (and not missing-clock-out marker)
        last = ev[-1] if ev else None
        if last is not None and last.type == "in" and getattr(last, "flag", None) != "missing_clock_out":
            clocked_in += 1
    return {
        "date": today_local.isoformat(),
        "present": present,
        "absent_so_far": absent_so_far,
        "late": late,
        "currently_clocked_in": clocked_in,
        "missing_clock_outs": missing_out,
        "pending_approvals": _pending_approvals(db, staff_ids),
    }


def _range_kpis(tm: dict[str, Any]) -> dict[str, Any]:
    # overtime hours not tracked as a separate duration in person_metrics; report overtime count-derived hours as 0
    # and total hours/overtime count transparently.
    total_ot_hours = 0.0
    for pm in tm.get("people", []) or []:
        # sum overtime minutes not stored; we expose overtime (count) and hours_worked
        total_ot_hours += 0.0
    return {
        "attendance_rate": tm.get("attendance_rate", 0.0),
        "on_time_rate": tm.get("on_time_rate", 0.0),
        "total_hours": tm.get("hours_worked", 0.0),
        "late_count": tm.get("late", 0),
        "overtime_count": tm.get("overtime", 0),
        "overtime_hours": round(total_ot_hours, 4),
    }


def _load_settings(db: Session) -> dict[str, Any]:
    from app.config import get_settings
    from app.models import Setting

    defaults = get_settings()
    rows = {r.key: r.value for r in db.scalars(select(Setting)).all()}
    out = {}
    for key, spec in DEFINITIONS.items():
        raw = rows[key] if key in rows else spec["default"](defaults)
        out[key] = _coerce(key, str(raw))
    return out


def _context(db: Session, admin: Admin, from_d: date, to_d: date, department: str | None):
    start, end = _parse_range(from_d, to_d)
    settings = _load_settings(db)
    tz_name = settings.get("timezone") or "UTC"
    people = _scoped_staff(db, admin, department)
    ids = [s.id for s in people]
    data = _shifts_data_for(db, ids, start, end)
    rows = _attendance_rows(db, ids, start, end, tz_name)
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
    return start, end, tz_name, people, ids, data, rows, now_local


@router.get("/summary")
def summary(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    department: str | None = None,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    tm = m_team_metrics(people, (start, end), rows, data, now=now_local)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "department": department,
        "today": _today_kpis(db, ids, people, data, tz_name),
        "range": _range_kpis(tm),
    }


@router.get("/trend")
def trend(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    department: str | None = None,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    series = m_daily_series(people, (start, end), rows, data, now=now_local)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "department": department,
        "items": [
            {"date": d["date"].isoformat() if hasattr(d["date"], "isoformat") else str(d["date"]), "present": d["present"], "late": d["late"], "absent": d["absent"]}
            for d in series
        ],
    }


@router.get("/breakdown")
def breakdown(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    by: str = Query("department", pattern="^(department|weekday|shift)$"),
    department: str | None = None,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    groups = m_team_metrics(people, (start, end), rows, data, now=now_local, group_by=by)
    return {"from": start.isoformat(), "to": end.isoformat(), "department": department, "by": by, "items": groups}


@router.get("/arrivals")
def arrivals(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    bucket_minutes: int = Query(15, ge=1, le=240),
    department: str | None = None,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    hist = m_arrival_histogram(people, (start, end), rows, data, bucket_minutes=bucket_minutes)
    return {"from": start.isoformat(), "to": end.isoformat(), "department": department, "bucket_minutes": bucket_minutes, "items": hist}


_SORT_KEY = {"late": "late", "hours": "hours_worked", "absent": "absent", "punctuality": "on_time_rate"}


@router.get("/people")
def people_metrics(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    sort: str = Query("late", pattern="^(late|hours|absent|punctuality)$"),
    department: str | None = None,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    per = [m_person_metrics(p, (start, end), rows, data, now=now_local) for p in people]
    key = _SORT_KEY[sort]
    reverse = sort != "punctuality"  # punctuality: higher is better, but ranking "top" by it means desc; spec says ranked list
    ranked = m_top_n(per, key, n=len(per), reverse=True if sort != "punctuality" else True)
    # for punctuality, show highest on-time rate first (reverse True)
    return {"from": start.isoformat(), "to": end.isoformat(), "department": department, "sort": sort, "items": ranked}


@router.get("/person/{staff_id}")
def person_detail(
    staff_id: int,
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end = _parse_range(from_d, to_d)
    tz_name = get_all(db).get("timezone") or "UTC"
    st = db.get(Staff, staff_id)
    if st is None or st.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")
    # scope
    if _role(admin) == ROLE_MANAGER and getattr(admin, "department_scope", None) != st.department:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = _shifts_data_for(db, [staff_id], start, end)
    rows = _attendance_rows(db, [staff_id], start, end, tz_name)
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
    pm = m_person_metrics(st, (start, end), rows, data, now=now_local)

    # per-day status list
    day_rows = defaultdict(list)
    for r in rows:
        sd = r.get("shift_date")
        if sd is None and r.get("timestamp") is not None:
            sd = r["timestamp"].date()
        if sd is not None:
            day_rows[sd].append(r)
    status_list = []
    cur = start
    while cur <= end:
        sh = None
        try:
            sh = resolve_shift(staff_id, cur, data)
        except Exception:
            sh = None
        dr = [r for r in day_rows.get(cur, []) if r.get("undone_at") is None]
        has_in = any(r["type"] == "in" and r.get("flag") != "missing_clock_out" for r in dr)
        miss = any(r.get("flag") == "missing_clock_out" for r in dr)
        late = any(r.get("flag") == "late" for r in dr if r["type"] == "in")
        if sh is None:
            status = "day off" if miss else "not scheduled"
        elif miss and not has_in:
            status = "missing clock-out"
        elif not has_in:
            ended = now_local > sh.end.replace(tzinfo=None)
            status = "absent" if ended else "not scheduled"  # in-progress/future not absent
        elif miss:
            status = "missing clock-out"
        elif late:
            status = "late"
        else:
            status = "on time"
        status_list.append({"date": cur.isoformat(), "status": status})
        cur += timedelta(days=1)

    # recent events (from range, latest 20, exclude undone from "active" view but show flag)
    recent = sorted(rows, key=lambda r: (r.get("timestamp") or datetime.min, r.get("id") or 0), reverse=True)[:20]
    recent_ev = [
        {
            "id": r["id"],
            "type": r["type"],
            "timestamp": r["timestamp"].isoformat() if r.get("timestamp") else None,
            "shift_date": r["shift_date"].isoformat() if hasattr(r.get("shift_date"), "isoformat") and r.get("shift_date") else (str(r.get("shift_date")) if r.get("shift_date") else None),
            "flag": r.get("flag"),
            "undone": r.get("undone_at") is not None,
        }
        for r in recent
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "staff": {"id": st.id, "name": st.name, "department": st.department, "staff_code": st.staff_code},
        "metrics": pm,
        "days": status_list,
        "recent_events": recent_ev,
    }


def _recognition_health(db: Session, start: date, end: date, tz_name: str) -> dict[str, Any]:
    zone = ZoneInfo(tz_name)
    lo_local = datetime.combine(start, time.min, tzinfo=zone)
    hi_local = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
    lo = lo_local.astimezone(timezone.utc).replace(tzinfo=None)
    hi = hi_local.astimezone(timezone.utc).replace(tzinfo=None)
    attempts = db.scalars(
        select(RecognitionAttempt).where(RecognitionAttempt.timestamp >= lo, RecognitionAttempt.timestamp < hi)
    ).all()
    total = len(attempts)
    matched = sum(1 for a in attempts if a.outcome == "matched")
    rejected = sum(1 for a in attempts if a.outcome != "matched")
    by_day: dict[date, int] = defaultdict(int)
    for a in attempts:
        if a.outcome == "matched":
            continue
        ts = a.timestamp
        if ts is None:
            continue
        if ts.tzinfo is None:
            ld = ts.replace(tzinfo=timezone.utc).astimezone(zone).date()
        else:
            ld = ts.astimezone(zone).date()
        by_day[ld] += 1
    backlog = int(db.scalar(select(func.count()).select_from(RecognitionAttempt).where(RecognitionAttempt.resolved.is_(False))) or 0)
    return {
        "attempts": total,
        "matched": matched,
        "rejected": rejected,
        "success_rate": (matched / total) if total else 0.0,
        "rejected_by_day": [{"date": d.isoformat(), "count": by_day[d]} for d in sorted(by_day)],
        "backlog": backlog,
    }


@router.get("/insights")
def insights(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    department: str | None = None,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    tm_now = m_team_metrics(people, (start, end), rows, data, now=now_local)
    per_now = [m_person_metrics(p, (start, end), rows, data, now=now_local) for p in people]
    weekday_now = m_lateness_by_weekday(people, (start, end), rows, data)
    late_hours = m_late_arrival_hours(people, (start, end), rows, data)
    dept_groups = m_team_metrics(people, (start, end), rows, data, now=now_local, group_by="department")

    # previous equal period (bounded)
    pstart, pend = _prev_equal_period(start, end)
    data_prev = _shifts_data_for(db, ids, pstart, pend)
    rows_prev = _attendance_rows(db, ids, pstart, pend, tz_name)
    tm_prev = m_team_metrics(people, (pstart, pend), rows_prev, data_prev, now=now_local)

    reco = _recognition_health(db, start, end, tz_name)
    items = m_compute_insights(
        start=start, end=end, department=department,
        team_now=tm_now, team_prev=tm_prev,
        weekday_now=weekday_now, late_hours=late_hours,
        people_now=per_now, dept_groups=dept_groups,
        recognition=reco,
    )
    return {
        "from": start.isoformat(), "to": end.isoformat(), "department": department,
        "previous": {"from": pstart.isoformat(), "to": pend.isoformat()},
        "items": items,
    }
