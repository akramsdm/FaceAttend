"""Patterns/assignments/exceptions API with require_role (Phase 2 / Step D)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_session
from app.models import Admin, Assignment, Pattern, PatternDay, ShiftException, Staff
from app.security import ROLE_ADMIN, ROLE_MANAGER, can_access_staff, require_role
from app.shifts import resolve_shift, validate_pattern

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
    owner_staff_id: int | None
    early_window_min: int
    grace_min: int
    break_min: int
    is_default: bool
    days: list[dict]


class AssignmentIn(BaseModel):
    pattern_id: int
    staff_ids: list[int] = Field(default_factory=list)
    department: str | None = None
    effective_from: date


class ExceptionIn(BaseModel):
    staff_id: int
    date: date
    kind: Literal["day_off", "custom_hours"]
    start_time: str | None = None
    end_time: str | None = None
    reason: str = Field(min_length=1, max_length=500)


def _role(admin: Admin) -> str:
    return getattr(admin, "role", None) or ROLE_ADMIN


def _is_admin(admin: Admin) -> bool:
    return _role(admin) == ROLE_ADMIN


def _pattern_to_dict(p: Pattern) -> dict:
    return {
        "id": p.id, "name": p.name, "owner_staff_id": p.owner_staff_id,
        "early_window_min": p.early_window_min, "grace_min": p.grace_min,
        "break_min": p.break_min, "is_default": p.is_default,
        "days": [{"weekday": d.weekday, "start_time": d.start_time.strftime("%H:%M"),
                  "end_time": d.end_time.strftime("%H:%M"), "overnight": d.overnight,
                  "enabled": d.enabled} for d in sorted(p.days, key=lambda x: x.weekday)],
    }


def _visible_patterns_query(db: Session, admin: Admin):
    q = select(Pattern)
    if not _is_admin(admin):
        # managers: shared patterns + patterns they own context? they "own" patterns they created
        # - treat manager-owned shared patterns as not in model (no owner admin field) except personal copies;
        # simplest scoping: managers see shared (owner_staff_id null) + personal patterns for staff in scope.
        # keep it permissive for shared, restrictive for personal:
        # handled by filtering in list; here return all and filter.
        pass
    return q


def _ensure_pattern_access(db: Session, admin: Admin, pattern: Pattern, *, write: bool = False) -> None:
    if _is_admin(admin):
        return
    # managers can write patterns they own? patterns have owner_staff_id (personal copy) or null (shared).
    # spec: "Managers: create, edit and delete patterns they own; create a personal copy..."
    if pattern.owner_staff_id is None:
        # shared pattern: managers may read; writes only to "own" - shared defaults/admin-owned not writable by manager
        if write:
            raise HTTPException(status_code=403, detail="Managers can only modify patterns they own")
        return
    # personal pattern owned for a staff member: manager must have scope over that staff
    staff = db.get(Staff, pattern.owner_staff_id)
    if staff is None or not can_access_staff(admin, staff):
        raise HTTPException(status_code=403, detail="Not in department scope")


def _staff_for_admin(db: Session, admin: Admin, staff_id: int) -> Staff:
    staff = db.get(Staff, staff_id)
    if staff is None or staff.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")
    if not can_access_staff(admin, staff):
        raise HTTPException(status_code=403, detail="Not in department scope")
    return staff


def _load_pattern(db: Session, pattern_id: int) -> Pattern:
    p = db.get(Pattern, pattern_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    # eager-ish days access (relationship lazy ok in session)
    _ = p.days
    return p


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
                  break_min=payload.break_min, is_default=False)
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
    pats = db.scalars(select(Pattern).order_by(Pattern.is_default.desc(), Pattern.name)).all()
    out = []
    for p in pats:
        if p.owner_staff_id is not None and not _is_admin(admin):
            st = db.get(Staff, p.owner_staff_id)
            if st is None or not can_access_staff(admin, st):
                continue
        out.append(_pattern_to_dict(p))
    return out


@router.put("/patterns/{pattern_id}", response_model=PatternOut)
def update_pattern(
    pattern_id: int,
    payload: PatternIn,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    pat = _load_pattern(db, pattern_id)
    _ensure_pattern_access(db, admin, pat, write=True)
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
    _ensure_pattern_access(db, admin, pat, write=True)
    # prevent delete if referenced? keep simple: null/forbid not specified; delete if owned
    record_audit(db, admin.id, "delete_pattern", f"pattern:{pat.id}", pat.name)
    db.delete(pat); db.commit()
    return {"status": "ok"}


@router.post("/patterns/{pattern_id}/customise/{staff_id}", response_model=PatternOut, status_code=201)
def customise_pattern_for_staff(
    pattern_id: int,
    staff_id: int,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    src = _load_pattern(db, pattern_id)
    staff = _staff_for_admin(db, admin, staff_id)
    # copy: personal pattern owned by this staff (owner_staff_id = staff.id)
    copy = Pattern(name=f"{src.name} (personal:{staff.staff_code})", owner_staff_id=staff.id,
                   early_window_min=src.early_window_min, grace_min=src.grace_min,
                   break_min=src.break_min, is_default=False)
    db.add(copy); db.flush()
    for d in src.days:
        db.add(PatternDay(pattern_id=copy.id, weekday=d.weekday, start_time=d.start_time,
                          end_time=d.end_time, overnight=d.overnight, enabled=d.enabled))
    record_audit(db, admin.id, "customise_pattern", f"pattern:{copy.id}", f"from {src.id} for staff {staff.id}")
    db.commit(); db.refresh(copy)
    return _pattern_to_dict(copy)


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
    return {"status": "ok", "assigned": created}


@router.post("/exceptions", status_code=201)
def create_exception(
    payload: ExceptionIn,
    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
    db: Session = Depends(get_session),
):
    staff = _staff_for_admin(db, admin, payload.staff_id)
    if not payload.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if payload.kind == "custom_hours" and (not payload.start_time or not payload.end_time):
        raise HTTPException(status_code=422, detail="custom_hours requires start_time and end_time")
    # upsert by (staff_id,date)
    existing = db.scalar(select(ShiftException).where(
        ShiftException.staff_id == staff.id, ShiftException.date == payload.date))
    st = _t(payload.start_time) if payload.start_time else None
    en = _t(payload.end_time) if payload.end_time else None
    if existing:
        existing.kind = payload.kind; existing.start_time = st; existing.end_time = en
        existing.reason = payload.reason.strip(); existing.created_by = admin.id
        ex = existing
    else:
        ex = ShiftException(staff_id=staff.id, date=payload.date, kind=payload.kind,
                            start_time=st, end_time=en, reason=payload.reason.strip(), created_by=admin.id)
        db.add(ex)
    record_audit(db, admin.id, "set_exception", f"staff:{staff.id}", f"{payload.date} {payload.kind}")
    db.commit(); db.refresh(ex)
    return {"id": ex.id, "staff_id": ex.staff_id, "date": ex.date.isoformat(), "kind": ex.kind, "reason": ex.reason}


def _shifts_data(db: Session):
    pats = db.scalars(select(Pattern)).all()
    patterns = []
    for p in pats:
        patterns.append({
            "id": p.id, "owner_staff_id": p.owner_staff_id,
            "early_window_min": p.early_window_min, "grace_min": p.grace_min, "break_min": p.break_min,
            "days": [{"weekday": d.weekday, "start_time": d.start_time, "end_time": d.end_time,
                      "overnight": d.overnight, "enabled": d.enabled} for d in p.days],
        })
    assigns = [{"staff_id": a.staff_id, "pattern_id": a.pattern_id,
                "effective_from": a.effective_from, "effective_to": a.effective_to}
               for a in db.scalars(select(Assignment)).all()]
    exs = [{"staff_id": e.staff_id, "date": e.date, "kind": e.kind,
            "start_time": e.start_time, "end_time": e.end_time} for e in db.scalars(select(ShiftException)).all()]
    return {"patterns": patterns, "assignments": assigns, "exceptions": exs}


@router.get("/staff/{staff_id}/resolved")
def resolved_shifts(
    staff_id: int,
    start: date = Query(...),
    end: date = Query(...),
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
