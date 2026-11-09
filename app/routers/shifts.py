"""Patterns/assignments/exceptions API with require_role (Phase 2 / Step D)."""
from __future__ import annotations

from datetime import date as date_t, datetime as datetime_t, time, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.audit import record_audit
from app.db import get_session
from app.models import Admin, Assignment, Attendance, Pattern, PatternDay, ShiftException, Staff
from app.security import ROLE_ADMIN, ROLE_MANAGER, can_access_staff, require_role
from app.shift_service import assignments_ending_for, can_edit_pattern, past_attendance_warning, save_personal_week
from app.shifts import resolve_shift, shift_date_for, validate_pattern

router = APIRouter(prefix="/api/admin/shifts", tags=["shifts"])

DEFAULT_PATTERNS = [
    ("Office hours", [(0, "08:00", "17:00", False), (1, "08:00", "17:00", False), (2, "08:00", "17:00", False),
                      (3, "08:00", "17:00", False), (4, "08:00", "17:00", False)], 30, 10, 60),
    ("Early", [(wd, "06:00", "14:00", False) for wd in range(0, 5)], 30, 10, 30),
    ("Late", [(wd, "14:00", "22:00", False) for wd in range(0, 5)], 30, 10, 30),
    ("Weekend", [(5, "09:00", "17:00", False), (6, "09:00", "17:00", False)], 30, 10, 30),
    ("Night", [(wd, "22:00", "06:00", True) for wd in range(0, 7)], 30, 10, 30),
]


def _t(s: str) -> time:
    p = s.strip().split(":")
    return time(int(p[0]), int(p[1]))


def seed_default_patterns(db: Session) -> None:
    existing = {r for (r,) in db.execute(select(Pattern.name).where(Pattern.owner_staff_id.is_(None))).all()}
    for name, days, early, grace, brk in DEFAULT_PATTERNS:
        if name in existing:
            continue
        pat = Pattern(name=name, owner_staff_id=None, early_window_min=early, grace_min=grace, break_min=brk, is_default=True)
        db.add(pat)
        db.flush()
        for wd, st, en, ov in days:
            db.add(PatternDay(pattern_id=pat.id, weekday=wd, start_time=_t(st), end_time=_t(en), overnight=ov, enabled=True))
    db.flush()


class DayIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: str
    end_time: str
    overnight: bool = False
    enabled: bool = True


class PatternIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    early_window_min: int = Field(default=30, ge=0, le=240)
    grace_min: int = Field(default=10, ge=0, le=240)
    break_min: int = Field(default=0, ge=0, le=480)
    days: list[DayIn]


class PatternOut(BaseModel):
    id: int
    name: str
    owner_staff_id: Optional[int] = None
    early_window_min: int
    grace_min: int
    break_min: int
    is_default: bool
    kind: Optional[str] = None
    based_on_pattern_id: Optional[int] = None
    archived_at: Optional[str] = None
    days: list[dict]


class AssignmentIn(BaseModel):
    pattern_id: int
    staff_ids: list[int] = Field(default_factory=list)
    department: Optional[str] = None
    effective_from: date_t
    dry_run: bool = False


class ExceptionIn(BaseModel):
    staff_id: int
    date: Optional[date_t] = None
    start_date: Optional[date_t] = None
    end_date: Optional[date_t] = None
    kind: Literal["day_off", "custom_hours"]
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: str = Field(min_length=1, max_length=500)
    dry_run: bool = False


class BulkExceptionIn(BaseModel):
    start_date: date_t
    end_date: date_t
    kind: Literal["day_off", "custom_hours"] = "day_off"
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: str = Field(min_length=1, max_length=500)
    everyone: bool = False
    department: Optional[str] = None
    staff_ids: list[int] = Field(default_factory=list)
    dry_run: bool = False


def _role(admin: Admin) -> str:
    return getattr(admin, "role", None) or ROLE_ADMIN


def _is_admin(admin: Admin) -> bool:
    return _role(admin) == ROLE_ADMIN


def _pattern_to_dict(p: Pattern) -> dict:
    return {
        "id": p.id, "name": p.name, "owner_staff_id": p.owner_staff_id,
        "early_window_min": p.early_window_min, "grace_min": p.grace_min,
        "break_min": p.break_min, "is_default": p.is_default,
        "kind": getattr(p, "kind", "shared" if p.owner_staff_id is None else "personal"),
        "based_on_pattern_id": getattr(p, "based_on_pattern_id", None),
        "archived_at": p.archived_at.isoformat() if getattr(p, "archived_at", None) else None,
        "days": [{"weekday": d.weekday, "start_time": d.start_time.strftime("%H:%M"),
                  "end_time": d.end_time.strftime("%H:%M"), "overnight": d.overnight,
                  "enabled": d.enabled} for d in sorted(p.days, key=lambda x: x.weekday)],
    }



def _iter_dates(start: date_t, end: date_t) -> list[date_t]:
    if end < start:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    if (end - start).days > 61:  # max 62 days inclusive
        raise HTTPException(status_code=422, detail="Date range exceeds 62 days")
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _exceptions_replaced(db: Session, staff_ids: list[int], dates: list[date_t]) -> list[dict]:
    if not staff_ids or not dates:
        return []
    rows = db.scalars(
        select(ShiftException).where(
            ShiftException.staff_id.in_(staff_ids), ShiftException.date.in_(dates)
        )
    ).all()
    return [
        {"id": e.id, "staff_id": e.staff_id, "date": e.date.isoformat(), "kind": e.kind}
        for e in rows
    ]


def _visible_patterns_query(db: Session, admin: Admin):
    q = select(Pattern)
    if not _is_admin(admin):
        pass
    return q


def _require_edit(db: Session, admin: Admin, pattern: Pattern) -> None:
    if not can_edit_pattern(admin, pattern, db):
        role = getattr(admin, "role", None) or ROLE_ADMIN
        if role == ROLE_MANAGER and getattr(pattern, "is_default", False):
            raise HTTPException(status_code=403, detail="Default schedules are admin-only (managers can duplicate)")
        if role == ROLE_MANAGER and (getattr(pattern, "kind", None) == "personal" or pattern.owner_staff_id is not None):
            raise HTTPException(status_code=403, detail="Not in department scope")
        raise HTTPException(status_code=403, detail="Not allowed to modify this pattern")


def _current_future_assignment_count(db: Session, pattern_id: int) -> int:
    from sqlalchemy import func
    today = date_t.today()
    return int(
        db.scalar(
            select(func.count())
            .select_from(Assignment)
            .where(
                Assignment.pattern_id == pattern_id,
                (Assignment.effective_to.is_(None)) | (Assignment.effective_to >= today),
            )
        )
        or 0
    )



def _staff_for_admin(db: Session, admin: Admin, staff_id: int) -> Staff:
    staff = db.get(Staff, staff_id)
    if staff is None or staff.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")
    if not can_access_staff(admin, staff):
        raise HTTPException(status_code=403, detail="Not in department scope")
    return staff


def _scoped_staff(db: Session, admin: Admin, *, department: str | None = None, search: str | None = None):
    from sqlalchemy import or_

    role = getattr(admin, "role", None) or ROLE_ADMIN
    q = select(Staff).where(Staff.anonymised.is_(False))
    if role == ROLE_MANAGER:
        q = q.where(Staff.department == getattr(admin, "department_scope", None))
    elif department:
        q = q.where(Staff.department == department)
    if search:
        like = f"%{search.strip()}%"
        q = q.where(or_(Staff.name.ilike(like), Staff.staff_code.ilike(like)))
    return list(db.scalars(q.order_by(Staff.name, Staff.id)).all())


def _iso_dt(val) -> str | None:
    if val is None:
        return None
    try:
        return val.isoformat()
    except Exception:
        return str(val)


def _tstr(val) -> str | None:
    if val is None:
        return None
    try:
        return val.strftime("%H:%M")
    except Exception:
        return str(val)


def _load_pattern(db: Session, pattern_id: int) -> Pattern:
    p = db.get(Pattern, pattern_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    # eager-ish days/owner access (relationships lazy ok in session)
    _ = p.days
    if getattr(p, "owner_staff_id", None) is not None:
        _ = db.get(Staff, p.owner_staff_id)
    return p



@router.get("/people")
def list_people(
    week_start: date_t = Query(...),
    department: str | None = Query(default=None),
    search: str | None = Query(default=None),
    working_today: bool = Query(default=False),
    debug: bool = Query(default=False),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    staff_list = _scoped_staff(db, admin, department=department, search=search)
    result = _people_rows(db, staff_list, week_start, working_today=working_today, return_query_count=True)
    if not debug:
        result.pop("query_count", None)
    return result


@router.post("/patterns", response_model=PatternOut, status_code=201)
def create_pattern(
    payload: PatternIn,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    errs = validate_pattern(payload.model_dump())
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    pat = Pattern(name=payload.name, owner_staff_id=None,
                  early_window_min=payload.early_window_min, grace_min=payload.grace_min,
                  break_min=payload.break_min, is_default=False, kind="shared", created_by=admin.id)
    db.add(pat); db.flush()
    for d in payload.days:
        db.add(PatternDay(pattern_id=pat.id, weekday=d.weekday, start_time=_t(d.start_time),
                          end_time=_t(d.end_time), overnight=d.overnight, enabled=d.enabled))
    record_audit(db, admin.id, "create_pattern", f"pattern:{pat.id}", payload.name)
    db.commit(); db.refresh(pat)
    return _pattern_to_dict(pat)



@router.get("/patterns", response_model=list[PatternOut])
def list_patterns(
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    pats = db.scalars(
        select(Pattern)
        .where(Pattern.kind == "shared", Pattern.archived_at.is_(None))
        .order_by(Pattern.is_default.desc(), Pattern.name)
    ).all()
    return [_pattern_to_dict(p) for p in pats]



@router.put("/patterns/{pattern_id}", response_model=PatternOut)
def update_pattern(
    pattern_id: int,
    payload: PatternIn,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    pat = _load_pattern(db, pattern_id)
    _require_edit(db, admin, pat)
    errs = validate_pattern(payload.model_dump())
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    pat.name = payload.name
    pat.early_window_min = payload.early_window_min
    pat.grace_min = payload.grace_min
    pat.break_min = payload.break_min
    for d in list(pat.days):
        db.delete(d)
    db.flush()
    for d in payload.days:
        db.add(PatternDay(pattern_id=pat.id, weekday=d.weekday, start_time=_t(d.start_time),
                          end_time=_t(d.end_time), overnight=d.overnight, enabled=d.enabled))
    record_audit(db, admin.id, "update_pattern", f"pattern:{pat.id}", payload.name)
    db.commit(); db.refresh(pat)
    return _pattern_to_dict(pat)



@router.delete("/patterns/{pattern_id}")
def delete_pattern(
    pattern_id: int,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    pat = _load_pattern(db, pattern_id)
    _require_edit(db, admin, pat)
    in_use = _current_future_assignment_count(db, pat.id)
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"Pattern has {in_use} current/future assignment(s); reassign before deleting",
        )
    # also guard refs that would violate FKs (past assignments, attendance rows)
    from sqlalchemy import func
    from app.models import Attendance

    total_asn = int(
        db.scalar(select(func.count()).select_from(Assignment).where(Assignment.pattern_id == pat.id)) or 0
    )
    att_refs = int(
        db.scalar(select(func.count()).select_from(Attendance).where(Attendance.shift_id == pat.id)) or 0
    )
    if total_asn or att_refs:
        parts = []
        if total_asn:
            parts.append(f"{total_asn} assignment(s)")
        if att_refs:
            parts.append(f"{att_refs} attendance row(s)")
        raise HTTPException(
            status_code=409,
            detail=f"Pattern is referenced by {' and '.join(parts)}; cannot delete (history preserved)",
        )
    record_audit(db, admin.id, "delete_pattern", f"pattern:{pat.id}", pat.name)
    db.delete(pat); db.commit()
    return {"status": "ok"}




@router.post("/patterns/{pattern_id}/customise/{staff_id}", response_model=PatternOut)
def customise_pattern_for_staff(
    pattern_id: int,
    staff_id: int,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    src = _load_pattern(db, pattern_id)
    staff = _staff_for_admin(db, admin, staff_id)
    from datetime import date as _date
    from app.shift_service import save_personal_week

    days = [
        {
            "weekday": d.weekday,
            "start_time": d.start_time.strftime("%H:%M"),
            "end_time": d.end_time.strftime("%H:%M"),
            "overnight": d.overnight,
            "enabled": d.enabled,
        }
        for d in sorted(src.days, key=lambda x: x.weekday)
    ]
    windows = {
        "early_window_min": src.early_window_min,
        "grace_min": src.grace_min,
        "break_min": src.break_min,
    }
    # resolve base to the shared pattern we intend to customise
    base_id = src.id if src.owner_staff_id is None else (getattr(src, "based_on_pattern_id", None) or src.id)
    result = save_personal_week(
        db, staff, days, windows, _date.today(), admin_id=admin.id, base_pattern_id=base_id
    )
    # identical to base => reset (assigns base). Return the effective pattern.
    pat_id = result.get("pattern_id") or result.get("pattern_id") or base_id
    if result.get("status") == "reset":
        pat_id = result["pattern_id"]
    else:
        pat_id = result.get("pattern_id") or base_id
    pat = _load_pattern(db, int(pat_id))
    record_audit(db, admin.id, "customise_pattern", f"pattern:{pat.id}", f"from {src.id} for staff {staff.id} result={result.get('status')}")
    db.commit(); db.refresh(pat)
    return _pattern_to_dict(pat)



def _resolve_targets(db: Session, admin: Admin, payload: AssignmentIn) -> list[Staff]:
    ids = set(payload.staff_ids or [])
    if payload.department:
        q = select(Staff).where(Staff.department == payload.department, Staff.anonymised.is_(False))
        for s in db.scalars(q).all():
            if can_access_staff(admin, s):
                ids.add(s.id)
    if not ids:
        raise HTTPException(status_code=422, detail="No staff selected")
    targets = []
    for sid in ids:
        targets.append(_staff_for_admin(db, admin, sid))
    return targets


@router.post("/assignments")
def create_assignments(
    payload: AssignmentIn,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    _load_pattern(db, payload.pattern_id)  # exists
    targets = _resolve_targets(db, admin, payload)
    staff_ids = [st.id for st in targets]
    ending = assignments_ending_for(db, staff_ids, payload.effective_from)
    warning = past_attendance_warning(db, staff_ids, [payload.effective_from])
    if payload.dry_run:
        return {
            "dry_run": True,
            "affected_people": staff_ids,
            "assignments_ending": ending,
            "warning": warning,
            "pattern_id": payload.pattern_id,
            "effective_from": payload.effective_from.isoformat(),
        }
    day_before = payload.effective_from - timedelta(days=1)
    created = 0
    for st in targets:
        # end previous current assignment (effective_to null or covering)
        currents = db.scalars(
            select(Assignment).where(
                Assignment.staff_id == st.id,
                (Assignment.effective_to.is_(None)) | (Assignment.effective_to >= payload.effective_from),
            )
        ).all()
        for a in currents:
            # if it starts on/after new effective_from, drop it; else close day before
            if a.effective_from >= payload.effective_from:
                db.delete(a)
            else:
                a.effective_to = day_before
        db.add(Assignment(staff_id=st.id, pattern_id=payload.pattern_id,
                          effective_from=payload.effective_from, effective_to=None))
        created += 1
    record_audit(db, admin.id, "assign_pattern", f"pattern:{payload.pattern_id}",
                 f"{created} staff effective {payload.effective_from}")
    db.commit()
    return {"dry_run": False, "status": "ok", "assigned": created, "affected_people": staff_ids, "assignments_ending": ending, "warning": warning}



@router.post("/exceptions")
def create_exception(
    payload: ExceptionIn,
    response: Response,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    staff = _staff_for_admin(db, admin, payload.staff_id)
    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if payload.kind == "custom_hours" and (not payload.start_time or not payload.end_time):
        raise HTTPException(status_code=422, detail="custom_hours requires start_time and end_time")
    start = payload.start_date or payload.date
    end = payload.end_date or payload.date
    if start is None or end is None:
        raise HTTPException(status_code=422, detail="date or start_date/end_date required")
    dates = _iter_dates(start, end)
    replaced = _exceptions_replaced(db, [staff.id], dates)
    warning = past_attendance_warning(db, [staff.id], dates)
    if payload.dry_run:
        return {
            "dry_run": True,
            "affected_people": [staff.id],
            "dates": [d.isoformat() for d in dates],
            "count": len(dates),
            "exceptions_replaced": replaced,
            "warning": warning,
        }
    st = _t(payload.start_time) if payload.start_time else None
    en = _t(payload.end_time) if payload.end_time else None
    results = []
    for d in dates:
        existing = db.scalar(select(ShiftException).where(
            ShiftException.staff_id == staff.id, ShiftException.date == d))
        if existing:
            existing.kind = payload.kind; existing.start_time = st; existing.end_time = en
            existing.reason = payload.reason.strip(); existing.created_by = admin.id
            ex = existing
        else:
            ex = ShiftException(staff_id=staff.id, date=d, kind=payload.kind,
                                start_time=st, end_time=en, reason=payload.reason.strip(), created_by=admin.id)
            db.add(ex)
            db.flush()
        results.append({"id": ex.id, "date": d.isoformat()})
    record_audit(db, admin.id, "set_exception", f"staff:{staff.id}", f"{len(dates)} day(s) {payload.kind}")
    db.commit()
    response.status_code = 201
    # backward-compatible single-date shape when one date requested
    if len(results) == 1 and payload.start_date is None and payload.end_date is None:
        r0 = results[0]
        return {"dry_run": False, "id": r0["id"], "staff_id": staff.id, "date": r0["date"], "kind": payload.kind, "reason": payload.reason, "count": 1, "items": results, "affected_people": [staff.id], "exceptions_replaced": replaced, "warning": warning}
    return {"dry_run": False, "count": len(results), "items": results, "affected_people": [staff.id], "exceptions_replaced": replaced, "warning": warning}




@router.post("/exceptions/bulk")
def bulk_exceptions(
    payload: BulkExceptionIn,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if payload.kind == "custom_hours" and (not payload.start_time or not payload.end_time):
        raise HTTPException(status_code=422, detail="custom_hours requires start_time and end_time")
    dates = _iter_dates(payload.start_date, payload.end_date)
    # resolve targets, scope-filtered
    if payload.everyone:
        if not _is_admin(admin):
            # manager "everyone" => their department scope only
            scope = getattr(admin, "department_scope", None)
            q = select(Staff).where(Staff.anonymised.is_(False), Staff.department == scope)
            targets = list(db.scalars(q).all())
        else:
            targets = list(db.scalars(select(Staff).where(Staff.anonymised.is_(False))).all())
    elif payload.department:
        q = select(Staff).where(Staff.anonymised.is_(False), Staff.department == payload.department)
        targets = [s for s in db.scalars(q).all() if can_access_staff(admin, s)]
    elif payload.staff_ids:
        targets = [_staff_for_admin(db, admin, sid) for sid in payload.staff_ids]
    else:
        raise HTTPException(status_code=422, detail="Provide everyone, department, or staff_ids")
    if not targets:
        raise HTTPException(status_code=422, detail="No staff in scope")
    staff_ids = [s.id for s in targets]
    replaced = _exceptions_replaced(db, staff_ids, dates)
    warning = past_attendance_warning(db, staff_ids, dates)
    if payload.dry_run:
        return {
            "dry_run": True,
            "affected_people": staff_ids,
            "dates": [d.isoformat() for d in dates],
            "count": len(staff_ids) * len(dates),
            "exceptions_replaced": replaced,
            "warning": warning,
        }
    st = _t(payload.start_time) if payload.start_time else None
    en = _t(payload.end_time) if payload.end_time else None
    written = 0
    for s in targets:
        for d in dates:
            existing = db.scalar(select(ShiftException).where(
                ShiftException.staff_id == s.id, ShiftException.date == d))
            if existing:
                existing.kind = payload.kind; existing.start_time = st; existing.end_time = en
                existing.reason = payload.reason.strip(); existing.created_by = admin.id
            else:
                db.add(ShiftException(staff_id=s.id, date=d, kind=payload.kind,
                                      start_time=st, end_time=en, reason=payload.reason.strip(), created_by=admin.id))
            written += 1
    record_audit(db, admin.id, "bulk_exception", f"{len(staff_ids)} staff", f"{len(dates)} day(s) {payload.kind}")
    db.commit()
    return {"dry_run": False, "count": written, "affected_people": staff_ids, "exceptions_replaced": replaced, "warning": warning}


class SaveWeekIn(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    pattern_id: Optional[int] = None
    days: list[DayIn]
    early_window_min: int = Field(default=30, ge=0, le=240)
    grace_min: int = Field(default=10, ge=0, le=240)
    break_min: int = Field(default=0, ge=0, le=480)
    effective_from: date_t
    dry_run: bool = False


@router.post("/staff/{staff_id}/week")
def save_week(
    staff_id: int,
    payload: SaveWeekIn,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    staff = _staff_for_admin(db, admin, staff_id)
    days = [d.model_dump() for d in payload.days]
    windows = {"early_window_min": payload.early_window_min, "grace_min": payload.grace_min, "break_min": payload.break_min}
    try:
        result = save_personal_week(
            db, staff, days, windows, payload.effective_from,
            admin_id=admin.id, base_pattern_id=payload.pattern_id, dry_run=payload.dry_run,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not payload.dry_run:
        db.commit()
    return result


def _pattern_data(p: Pattern) -> dict:
    return {
        "id": p.id,
        "owner_staff_id": p.owner_staff_id,
        "kind": getattr(p, "kind", "shared" if p.owner_staff_id is None else "personal"),
        "based_on_pattern_id": getattr(p, "based_on_pattern_id", None),
        "early_window_min": p.early_window_min,
        "grace_min": p.grace_min,
        "break_min": p.break_min,
        "days": [
            {
                "weekday": d.weekday,
                "start_time": d.start_time,
                "end_time": d.end_time,
                "overnight": d.overnight,
                "enabled": d.enabled,
            }
            for d in p.days
        ],
    }


def _shifts_data_for(db: Session, staff_ids: list[int], start: date_t, end: date_t) -> dict:
    """Build resolve_shift data in a constant number of queries for a staff set/week."""
    pats = list(db.scalars(select(Pattern).options(selectinload(Pattern.days))).all())
    assigns = list(
        db.scalars(
            select(Assignment).where(
                Assignment.staff_id.in_(staff_ids),
                Assignment.effective_from <= end,
                or_(Assignment.effective_to.is_(None), Assignment.effective_to >= start),
            )
        ).all()
    ) if staff_ids else []
    exs = list(
        db.scalars(
            select(ShiftException).where(
                ShiftException.staff_id.in_(staff_ids),
                ShiftException.date >= start,
                ShiftException.date <= end,
            )
        ).all()
    ) if staff_ids else []
    patterns = [_pattern_data(p) for p in pats]
    assignments = [
        {
            "staff_id": a.staff_id,
            "pattern_id": a.pattern_id,
            "effective_from": a.effective_from,
            "effective_to": a.effective_to,
        }
        for a in assigns
    ]
    exceptions = [
        {
            "staff_id": e.staff_id,
            "date": e.date,
            "kind": e.kind,
            "start_time": e.start_time,
            "end_time": e.end_time,
        }
        for e in exs
    ]
    return {"patterns": patterns, "assignments": assignments, "exceptions": exs}


def _people_rows(
    db: Session,
    staff_list: list[Staff],
    week_start: date_t,
    *,
    working_today: bool = False,
    return_query_count: bool = False,
):
    from sqlalchemy import event

    count = {"n": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        count["n"] += 1

    eng = db.get_bind()
    if return_query_count and eng is not None:
        event.listen(eng, "before_cursor_execute", _before)
    try:
        week_end = week_start + timedelta(days=6)
        today = date_t.today()
        ids = [s.id for s in staff_list]
        data = _shifts_data_for(db, ids, week_start, week_end)
        today_data = None
        if working_today and not (week_start <= today <= week_end):
            today_data = _shifts_data_for(db, ids, today, today)
        # pattern metadata for labels/based_on
        pat_meta = {p["id"]: p for p in data["patterns"]}
        pat_rows = list(db.scalars(select(Pattern)).all())
        pat_names = {p.id: p.name for p in pat_rows}
        pat_base = {p.id: getattr(p, "based_on_pattern_id", None) for p in pat_rows}
        pat_kind = {p.id: getattr(p, "kind", "shared" if p.owner_staff_id is None else "personal") for p in pat_rows}

        # active assignment per staff for the week (prefer current/covering week_start)
        asn_by_staff: dict[int, list] = {}
        for a in data["assignments"]:
            asn_by_staff.setdefault(a["staff_id"], []).append(a)

        def _active_for(sid: int):
            cands = asn_by_staff.get(sid, [])
            covering = [a for a in cands if a["effective_from"] <= week_start and (a["effective_to"] is None or a["effective_to"] >= week_start)]
            if covering:
                return sorted(covering, key=lambda x: (x["effective_from"], x["pattern_id"]), reverse=True)[0]
            # any overlapping
            if cands:
                return sorted(cands, key=lambda x: (x["effective_from"], x["pattern_id"]), reverse=True)[0]
            return None

        ex_by_staff: dict[int, set] = {}
        for e in data["exceptions"]:
            ex_by_staff.setdefault(e["staff_id"], set()).add(e["date"])

        out = []
        for st in staff_list:
            a = _active_for(st.id)
            pat_id = a["pattern_id"] if a else None
            label = pat_names.get(pat_id) if pat_id else None
            kind = pat_kind.get(pat_id) if pat_id else None
            base_id = pat_base.get(pat_id) if pat_id else None
            based_on = pat_names.get(base_id) if base_id else None
            has_ex_in_week = bool(ex_by_staff.get(st.id))
            if kind == "personal" or (pat_id and kind != "shared"):
                source = "changed" if has_ex_in_week else "custom"
            elif pat_id:
                source = "changed" if has_ex_in_week else "shared"
            else:
                source = "changed" if has_ex_in_week else "shared"
                label = label or "None"

            days = []
            for i in range(7):
                d = week_start + timedelta(days=i)
                sh = resolve_shift(st.id, d, data)
                if sh is None:
                    days.append({"date": d.isoformat(), "shift": None})
                else:
                    days.append(
                        {
                            "date": d.isoformat(),
                            "shift": {
                                "start": sh.start.isoformat(),
                                "end": sh.end.isoformat(),
                                "overnight": sh.overnight,
                                "source": sh.source,
                            },
                        }
                    )
            if working_today:
                tdata = data if (week_start <= today <= week_end) else (today_data or {"patterns": data["patterns"], "assignments": [], "exceptions": []})
                sh_t = resolve_shift(st.id, today, tdata)
                if sh_t is None:
                    continue
            out.append(
                {
                    "id": st.id,
                    "name": st.name,
                    "department": st.department,
                    "schedule_label": label or "None",
                    "source": source,
                    "based_on": based_on,
                    "days": days,
                }
            )
        result = {"items": out}
        if return_query_count:
            result["query_count"] = count["n"]
        return result
    finally:
        if return_query_count and eng is not None:
            try:
                event.remove(eng, "before_cursor_execute", _before)
            except Exception:
                pass


def _shifts_data(db: Session):
    """All-pattern/assignment/exception data for resolve_shift (constant small-table scan)."""
    pats = list(db.scalars(select(Pattern).options(selectinload(Pattern.days))).all())
    patterns = [_pattern_data(p) for p in pats]
    assigns = [
        {
            "staff_id": a.staff_id,
            "pattern_id": a.pattern_id,
            "effective_from": a.effective_from,
            "effective_to": a.effective_to,
        }
        for a in db.scalars(select(Assignment)).all()
    ]
    exs = [
        {
            "staff_id": e.staff_id,
            "date": e.date,
            "kind": e.kind,
            "start_time": e.start_time,
            "end_time": e.end_time,
        }
        for e in db.scalars(select(ShiftException)).all()
    ]
    return {"patterns": patterns, "assignments": assigns, "exceptions": exs}


@router.get("/staff/{staff_id}/history")
def staff_history(
    staff_id: int,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    staff = _staff_for_admin(db, admin, staff_id)
    # assignments (with pattern metadata) in time order
    asn_rows = list(
        db.scalars(
            select(Assignment).where(Assignment.staff_id == staff.id).order_by(Assignment.effective_from, Assignment.id)
        ).all()
    )
    pat_ids = {a.pattern_id for a in asn_rows}
    pat_map = {}
    if pat_ids:
        for p in db.scalars(select(Pattern).where(Pattern.id.in_(pat_ids))).all():
            pat_map[p.id] = p
    assignments = []
    for a in asn_rows:
        p = pat_map.get(a.pattern_id)
        assignments.append(
            {
                "id": a.id,
                "pattern_id": a.pattern_id,
                "pattern_name": getattr(p, "name", None),
                "pattern_kind": getattr(p, "kind", None) if p else None,
                "effective_from": a.effective_from.isoformat(),
                "effective_to": a.effective_to.isoformat() if a.effective_to else None,
            }
        )
    # personal (owned) pattern versions in time order
    personal_rows = list(
        db.scalars(
            select(Pattern)
            .where(Pattern.owner_staff_id == staff.id)
            .order_by(Pattern.created_at, Pattern.id)
        ).all()
    )
    base_ids = {p.based_on_pattern_id for p in personal_rows if getattr(p, "based_on_pattern_id", None)}
    base_names = {}
    if base_ids:
        base_names = {p.id: p.name for p in db.scalars(select(Pattern).where(Pattern.id.in_(base_ids))).all()}
    personal_versions = [
        {
            "id": p.id,
            "name": p.name,
            "kind": getattr(p, "kind", None),
            "based_on_pattern_id": getattr(p, "based_on_pattern_id", None),
            "based_on_name": base_names.get(getattr(p, "based_on_pattern_id", None)),
            "created_by": getattr(p, "created_by", None),
            "created_at": _iso_dt(getattr(p, "created_at", None)),
            "archived_at": _iso_dt(getattr(p, "archived_at", None)),
        }
        for p in personal_rows
    ]
    # exceptions in time order (with actor + reason)
    ex_rows = list(
        db.scalars(
            select(ShiftException)
            .where(ShiftException.staff_id == staff.id)
            .order_by(ShiftException.date, ShiftException.id)
        ).all()
    )
    exceptions = [
        {
            "id": e.id,
            "date": e.date.isoformat(),
            "kind": e.kind,
            "start_time": _tstr(getattr(e, "start_time", None)),
            "end_time": _tstr(getattr(e, "end_time", None)),
            "reason": e.reason,
            "created_by": getattr(e, "created_by", None),
            "created_at": _iso_dt(getattr(e, "created_at", None)),
        }
        for e in ex_rows
    ]
    # unified time-ordered event list (actor+reason where applicable)
    events = []
    for a in assignments:
        events.append({"type": "assignment", "at": a["effective_from"], "date": a["effective_from"], "id": a["id"], "detail": a, "actor": None, "reason": None})
    for p in personal_versions:
        at = (p.get("created_at") or "")[:10] or None
        events.append({"type": "personal_version", "at": at, "date": at, "id": p["id"], "detail": p, "actor": p.get("created_by"), "reason": None})
    for e in exceptions:
        events.append({"type": "exception", "at": e["date"], "date": e["date"], "id": e["id"], "detail": e, "actor": e.get("created_by"), "reason": e.get("reason")})
    events.sort(key=lambda x: (x.get("at") or "", x.get("type") or "", x.get("id") or 0))
    return {
        "staff_id": staff.id,
        "assignments": assignments,
        "personal_versions": personal_versions,
        "exceptions": exceptions,
        "events": events,
    }


@router.get("/staff/{staff_id}/resolved")
def resolved_shifts(
    staff_id: int,
    start: date_t = Query(...),
    end: date_t = Query(...),
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    staff = _staff_for_admin(db, admin, staff_id)
    if end < start or (end - start).days > 62:
        raise HTTPException(status_code=422, detail="invalid range (max 62 days)")
    data = _shifts_data(db)
    out = []
    cur = start
    while cur <= end:
        sh = resolve_shift(staff.id, cur, data)
        if sh is None:
            out.append({"date": cur.isoformat(), "shift": None})
        else:
            out.append({"date": cur.isoformat(), "shift": {
                "start": sh.start.isoformat(), "end": sh.end.isoformat(), "overnight": sh.overnight,
                "early_window": sh.early_window, "grace": sh.grace, "break_min": sh.break_min, "source": sh.source
            }})
        cur += timedelta(days=1)
    return {"staff_id": staff.id, "items": out}
