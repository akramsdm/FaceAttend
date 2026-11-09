"""Admin/manager metrics API (Step 2B). Department scope enforced.

All endpoints accept from/to (YYYY-MM-DD), capped at 366 days inclusive, and an
optional department filter (further restricts, never widens, manager scope).
Uses a constant number of queries per endpoint (independent of staff count,
aside from small IN-bounded loads over the already-scoped staff set).
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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
    followup_flags as m_followup_flags,
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
                "method": getattr(r, "method", None),
                "edited_by": getattr(r, "edited_by", None),
                "edit_reason": getattr(r, "edit_reason", None),
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
    n, d = _followup_cfg(db)
    per = _attach_followup(per, n, d)
    key = _SORT_KEY[sort]
    reverse = sort != "punctuality"  # punctuality: higher is better, but ranking "top" by it means desc; spec says ranked list
    ranked = m_top_n(per, key, n=len(per), reverse=True if sort != "punctuality" else True)
    # for punctuality, show highest on-time rate first (reverse True)
    return {"from": start.isoformat(), "to": end.isoformat(), "department": department, "sort": sort,
            "followup": {"late_count": n, "period_days": d, "note": "Follow-up is a prompt for a conversation, not an automatic penalty."},
            "items": ranked}


def _person_context(db: Session, admin: Admin, staff_id: int, from_d: date, to_d: date):
    start, end = _parse_range(from_d, to_d)
    tz_name = get_all(db).get("timezone") or "UTC"
    st = db.get(Staff, staff_id)
    if st is None or st.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")
    if _role(admin) == ROLE_MANAGER and getattr(admin, "department_scope", None) != st.department:
        raise HTTPException(status_code=403, detail="Forbidden")
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
    today_local = now_local.date()
    # include today so "current schedule" resolves from the same bounded data (constant queries)
    d0 = min(start, today_local)
    d1 = max(end, today_local)
    data = _shifts_data_for(db, [staff_id], d0, d1)
    rows = _attendance_rows(db, [staff_id], start, end, tz_name)
    return start, end, tz_name, st, data, rows, now_local, today_local


def _arrival_offset_minutes(day_rows: list[dict[str, Any]], sh) -> float | None:
    ins = [r for r in day_rows if r.get("type") == "in" and r.get("flag") != "missing_clock_out" and r.get("undone_at") is None]
    if not ins or sh is None:
        return None
    ins.sort(key=lambda r: (r.get("timestamp") or datetime.min, r.get("id") or 0))
    ts = ins[0].get("timestamp")
    if ts is None:
        return None
    start = sh.start.replace(tzinfo=None) if getattr(sh.start, "tzinfo", None) else sh.start
    tsn = ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts
    return round((tsn - start).total_seconds() / 60.0, 2)


def _pattern_name(data: dict[str, Any], pattern_id: int | None) -> str | None:
    if pattern_id is None:
        return None
    for p in data.get("patterns") or []:
        if p.get("id") == pattern_id:
            return p.get("name")
    return None


def _current_schedule_from(data: dict[str, Any], staff_id: int, today_local: date):
    sh = resolve_shift(staff_id, today_local, data)
    assign = None
    for a in data.get("assignments") or []:
        if a.get("staff_id") != staff_id:
            continue
        ef, et = a.get("effective_from"), a.get("effective_to")
        if ef and ef <= today_local and (et is None or et >= today_local):
            assign = a
    exc = next((e for e in (data.get("exceptions") or []) if e.get("staff_id") == staff_id and e.get("date") == today_local), None)
    if exc is not None:
        return {"date": today_local.isoformat(), "source": "exception", "kind": exc.get("kind"),
                "start": exc.get("start_time").strftime("%H:%M") if exc.get("start_time") else None,
                "end": exc.get("end_time").strftime("%H:%M") if exc.get("end_time") else None,
                "pattern": _pattern_name(data, assign.get("pattern_id") if assign else None)}
    if sh is None:
        return {"date": today_local.isoformat(), "source": "not scheduled", "pattern": _pattern_name(data, assign.get("pattern_id") if assign else None)}
    pat_name = _pattern_name(data, assign.get("pattern_id") if assign else None)
    src = getattr(sh, "source", None) or ("shared pattern" if assign else "not scheduled")
    return {"date": today_local.isoformat(), "source": src, "pattern": pat_name,
            "start": sh.start.replace(tzinfo=None).strftime("%H:%M"),
            "end": sh.end.replace(tzinfo=None).strftime("%H:%M"),
            "grace_min": sh.grace, "break_min": sh.break_min, "overnight": bool(sh.overnight)}


@router.get("/person/{staff_id}")
def person_detail(
    staff_id: int,
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, st, data, rows, now_local, today_local = _person_context(db, admin, staff_id, from_d, to_d)
    pm = m_person_metrics(st, (start, end), rows, data, now=now_local)
    late = int(pm.get("late", 0) or 0)
    pm = dict(pm)
    pm["average_late_minutes"] = round(float(pm.get("late_minutes", 0) or 0) / late, 2) if late else 0.0

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
        late_flag = any(r.get("flag") == "late" for r in dr if r["type"] == "in")
        if sh is None:
            status = "day off" if miss else "not scheduled"
        elif miss and not has_in:
            status = "missing clock-out"
        elif not has_in:
            ended = now_local > sh.end.replace(tzinfo=None)
            status = "absent" if ended else "not scheduled"
        elif miss:
            status = "missing clock-out"
        elif late_flag:
            status = "late"
        else:
            status = "on time"
        status_list.append({"date": cur.isoformat(), "status": status, "arrival_offset_minutes": _arrival_offset_minutes(day_rows.get(cur, []), sh)})
        cur += timedelta(days=1)

    # full event table for range (with manager edits/reasons) + recent_events back-compat
    editor_ids = {int(r["edited_by"]) for r in rows if r.get("edited_by") is not None}
    editors = {}
    if editor_ids:
        for a in db.scalars(select(Admin).where(Admin.id.in_(editor_ids))).all():
            editors[a.id] = getattr(a, "username", None) or str(a.id)
    events = []
    for r in sorted(rows, key=lambda x: (x.get("timestamp") or datetime.min, x.get("id") or 0)):
        events.append({
            "id": r["id"], "type": r["type"], "method": r.get("method"),
            "timestamp": r["timestamp"].isoformat() if r.get("timestamp") else None,
            "shift_date": r["shift_date"].isoformat() if hasattr(r.get("shift_date"), "isoformat") and r.get("shift_date") else (str(r.get("shift_date")) if r.get("shift_date") else None),
            "flag": r.get("flag"), "undone": r.get("undone_at") is not None,
            "edited_by": r.get("edited_by"), "edited_by_name": editors.get(r.get("edited_by")),
            "edit_reason": r.get("edit_reason"),
        })
    recent_ev = list(reversed(events))[:20]
    n, d = _followup_cfg(db)
    fu = m_followup_flags([pm], late_count=n, period_days=d).get(staff_id)
    schedule = _current_schedule_from(data, staff_id, today_local)
    return {
        "from": start.isoformat(), "to": end.isoformat(),
        "staff": {"id": st.id, "name": st.name, "department": st.department, "staff_code": st.staff_code},
        "metrics": pm,
        "followup": {"late_count": n, "period_days": d, "note": "Follow-up is a prompt for a conversation, not an automatic penalty.",
                     "flagged": bool(fu), **({"late": fu["late"]} if fu else {})},
        "days": status_list,
        "events": events,
        "recent_events": recent_ev,
        "schedule": schedule,
    }


@router.get("/person/{staff_id}/export.csv")
def person_export_csv(
    staff_id: int,
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, st, data, rows, now_local, _today = _person_context(db, admin, staff_id, from_d, to_d)
    editor_ids = {int(r["edited_by"]) for r in rows if r.get("edited_by") is not None}
    editors = {a.id: (getattr(a, "username", None) or str(a.id)) for a in db.scalars(select(Admin).where(Admin.id.in_(editor_ids))).all()} if editor_ids else {}
    headers = ["event_id", "staff_id", "staff_code", "name", "department", "type", "method", "local_time", "shift_date", "flag", "undone", "edited_by", "edit_reason"]
    out_rows = []
    for r in sorted(rows, key=lambda x: (x.get("timestamp") or datetime.min, x.get("id") or 0)):
        out_rows.append([
            r["id"], st.id, st.staff_code, st.name, st.department or "", r["type"], r.get("method") or "",
            r["timestamp"].isoformat() if r.get("timestamp") else "",
            r["shift_date"].isoformat() if hasattr(r.get("shift_date"), "isoformat") and r.get("shift_date") else (str(r.get("shift_date")) if r.get("shift_date") else ""),
            r.get("flag") or "", "yes" if r.get("undone_at") is not None else "no",
            editors.get(r.get("edited_by")) or (r.get("edited_by") or ""), r.get("edit_reason") or "",
        ])
    return _csv_response(headers, out_rows, f"person-{st.staff_code}-{start.isoformat()}-to-{end.isoformat()}.csv")


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


# ------------------------------- 3B reports/follow-up -------------------

def _followup_cfg(db: Session) -> tuple[int, int]:
    vals = _load_settings(db)
    try:
        n = int(vals.get("followup_late_count", 3) or 3)
    except Exception:
        n = 3
    try:
        d = int(vals.get("followup_period_days", 30) or 30)
    except Exception:
        d = 30
    return n, d


def _attach_followup(per: list[dict[str, Any]], n: int, d: int) -> list[dict[str, Any]]:
    flags = m_followup_flags(per, late_count=n, period_days=d)
    out = []
    for pm in per:
        x = dict(pm)
        f = flags.get(int(x.get("staff_id") or 0))
        x["needs_followup"] = bool(f)
        if f:
            x["followup"] = f
        out.append(x)
    return out


def _csv_response(headers: list[str], rows: list[list[Any]], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/reports/late-arrivals")
def report_late_arrivals(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    department: str | None = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    per = [m_person_metrics(p, (start, end), rows, data, now=now_local) for p in people]
    n, d = _followup_cfg(db)
    per = _attach_followup(per, n, d)
    items = [p for p in per if int(p.get("late", 0) or 0) > 0]
    items.sort(key=lambda x: (-int(x.get("late", 0) or 0), -int(x.get("late_minutes", 0) or 0), str(x.get("name") or "")))
    totals = {
        "staff": len(items),
        "late_arrivals": sum(int(x.get("late", 0) or 0) for x in items),
        "late_minutes": sum(int(x.get("late_minutes", 0) or 0) for x in items),
    }
    cols = ["staff_id", "staff_code", "name", "department", "late_arrivals", "late_minutes", "on_time_rate", "attendance_rate", "needs_followup"]
    if format == "csv":
        csv_rows = []
        for x in items:
            csv_rows.append([x.get("staff_id"), x.get("staff_code") or "", x.get("name") or "", x.get("department") or "",
                             int(x.get("late", 0) or 0), int(x.get("late_minutes", 0) or 0),
                             round(float(x.get("on_time_rate", 0) or 0), 6), round(float(x.get("attendance_rate", 0) or 0), 6),
                             "yes" if x.get("needs_followup") else "no"])
        csv_rows.append(["TOTAL", "", "", "", totals["late_arrivals"], totals["late_minutes"], "", "", ""])
        return _csv_response(cols, csv_rows, f"late-arrivals-{start.isoformat()}-to-{end.isoformat()}.csv")
    # json: strip bulky per-day detail, keep summary fields
    slim = [{k: x.get(k) for k in ("staff_id", "staff_code", "name", "department", "clock_ins", "on_time", "late", "late_minutes", "on_time_rate", "attendance_rate", "hours_worked", "missing_clock_outs", "needs_followup")} for x in items]
    return {"from": start.isoformat(), "to": end.isoformat(), "department": department,
            "followup": {"late_count": n, "period_days": d, "note": "Follow-up is a prompt for a conversation, not an automatic penalty."},
            "totals": totals, "columns": cols, "items": slim}


@router.get("/reports/attendance-summary")
def report_attendance_summary(
    from_d: date = Query(..., alias="from"),
    to_d: date = Query(..., alias="to"),
    department: str | None = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    start, end, tz_name, people, ids, data, rows, now_local = _context(db, admin, from_d, to_d, department)
    per = [m_person_metrics(p, (start, end), rows, data, now=now_local) for p in people]
    n, d = _followup_cfg(db)
    per = _attach_followup(per, n, d)
    items = sorted(per, key=lambda x: (str(x.get("department") or ""), str(x.get("name") or "")))
    totals = {
        "staff": len(items),
        "scheduled_ended": sum(int(x.get("scheduled_ended", 0) or 0) for x in items),
        "present": sum(int(x.get("present", 0) or 0) for x in items),
        "absent": sum(int(x.get("absent", 0) or 0) for x in items),
        "late": sum(int(x.get("late", 0) or 0) for x in items),
        "hours_worked": round(sum(float(x.get("hours_worked", 0) or 0) for x in items), 4),
        "missing_clock_outs": sum(int(x.get("missing_clock_outs", 0) or 0) for x in items),
    }
    cols = ["staff_id", "staff_code", "name", "department", "scheduled_ended", "present", "absent", "late", "on_time_rate", "attendance_rate", "hours_worked", "missing_clock_outs", "needs_followup"]
    if format == "csv":
        csv_rows = []
        for x in items:
            csv_rows.append([x.get("staff_id"), x.get("staff_code") or "", x.get("name") or "", x.get("department") or "",
                             int(x.get("scheduled_ended", 0) or 0), int(x.get("present", 0) or 0), int(x.get("absent", 0) or 0),
                             int(x.get("late", 0) or 0), round(float(x.get("on_time_rate", 0) or 0), 6),
                             round(float(x.get("attendance_rate", 0) or 0), 6), round(float(x.get("hours_worked", 0) or 0), 4),
                             int(x.get("missing_clock_outs", 0) or 0), "yes" if x.get("needs_followup") else "no"])
        csv_rows.append(["TOTAL", "", "", "", totals["scheduled_ended"], totals["present"], totals["absent"], totals["late"], "", "", totals["hours_worked"], totals["missing_clock_outs"], ""])
        return _csv_response(cols, csv_rows, f"attendance-summary-{start.isoformat()}-to-{end.isoformat()}.csv")
    slim = [{k: x.get(k) for k in ("staff_id", "staff_code", "name", "department", "scheduled_ended", "present", "absent", "late", "on_time_rate", "attendance_rate", "hours_worked", "missing_clock_outs", "needs_followup")} for x in items]
    return {"from": start.isoformat(), "to": end.isoformat(), "department": department,
            "followup": {"late_count": n, "period_days": d, "note": "Follow-up is a prompt for a conversation, not an automatic penalty."},
            "totals": totals, "columns": cols, "items": slim}


@router.get("/followup")
def followup_config(
    _admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    n, d = _followup_cfg(db)
    return {"late_count": n, "period_days": d, "note": "Follow-up is a prompt for a conversation, not an automatic penalty."}
