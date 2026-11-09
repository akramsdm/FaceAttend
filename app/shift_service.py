"""Personal pattern copy-on-write/reset/GC service (Phase 3 / Step D).

Not in the router. Does not change app/shifts.py or clock/scan/attendance logic.
Rules:
- Assigning a shared pattern never copies.
- A saved week identical to the base is a reset.
- A different week creates a new personal version and assigns it from
  effective_from, ending the previous assignment the day before and archiving
  the old personal version.
- Archived versions referenced by assignments, exceptions or attendance are
  kept; others are deleted.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.audit import record_audit
from app.models import Assignment, Attendance, Pattern, PatternDay, Staff
from app.security import ROLE_ADMIN, ROLE_MANAGER, can_access_staff
from app.shifts import validate_pattern


def can_edit_pattern(admin, pattern, db: Session | None = None) -> bool:
    """Authorization for editing/deleting a pattern (Phase 3 Step E).

    - Admin may edit any.
    - The five shipped defaults (is_default) are admin-only (managers can duplicate).
    - Manager may edit shared patterns with created_by equal to themselves.
    - Manager may edit personal patterns of staff in their department.
    """
    role = getattr(admin, "role", None) or ROLE_ADMIN
    if role == ROLE_ADMIN:
        return True
    if role != ROLE_MANAGER:
        return False
    if pattern.is_default:
        return False
    is_personal = getattr(pattern, "kind", None) == "personal" or pattern.owner_staff_id is not None
    if is_personal:
        staff = getattr(pattern, "owner_staff", None)
        if staff is None and db is not None and pattern.owner_staff_id is not None:
            staff = db.get(Staff, pattern.owner_staff_id)
        return can_access_staff(admin, staff)
    # shared, non-default: editable only by its creator-manager
    created_by = getattr(pattern, "created_by", None)
    return created_by is not None and created_by == getattr(admin, "id", None)


def _as_date(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    if hasattr(val, "date") and not isinstance(val, str):
        try:
            return val.date()
        except Exception:
            pass
    try:
        return date.fromisoformat(str(val)[:10])
    except Exception:
        return None


def past_attendance_warning(db: Session, staff_ids: list[int], dates: list[date]) -> str | None:
    """Warn when affected dates already have non-undone attendance (Phase 3)."""
    if not staff_ids or not dates:
        return None
    from app.models import Attendance
    from sqlalchemy import func

    date_strs = [d.isoformat() for d in dates]
    rows = db.scalars(
        select(Attendance).where(
            Attendance.staff_id.in_(staff_ids),
            Attendance.undone_at.is_(None),
        )
    ).all()
    hit = set()
    ds = set(date_strs)
    for a in rows:
        ad = _as_date(getattr(a, "shift_date", None))
        if ad is None:
            ad = _as_date(getattr(a, "timestamp", None))
        if ad and ad.isoformat() in ds and a.staff_id in staff_ids:
            hit.add(a.staff_id)
    if not hit:
        return None
    return f"Warning: {len(hit)} person(s) already have attendance on affected date(s); reports for those days may differ."


def assignments_ending_for(db: Session, staff_ids: list[int], effective_from: date) -> list[dict]:
    """Assignments that would end (or be removed) when assigning from effective_from."""
    out = []
    rows = db.scalars(
        select(Assignment).where(
            Assignment.staff_id.in_(staff_ids),
            (Assignment.effective_to.is_(None)) | (Assignment.effective_to >= effective_from),
        )
    ).all()
    for a in rows:
        action = "delete" if a.effective_from >= effective_from else "close"
        closed_to = None if action == "delete" else (effective_from - timedelta(days=1)).isoformat()
        out.append(
            {
                "assignment_id": a.id,
                "staff_id": a.staff_id,
                "pattern_id": a.pattern_id,
                "effective_from": a.effective_from.isoformat(),
                "effective_to": a.effective_to.isoformat() if a.effective_to else None,
                "action": action,
                "closed_to": closed_to,
            }
        )
    return out


DEFAULT_WINDOWS = {"early_window_min": 30, "grace_min": 10, "break_min": 0}




def _day_sig(days: list[PatternDay] | list[dict[str, Any]]) -> list[tuple]:
    norm = []
    for d in days:
        if isinstance(d, PatternDay):
            wd = d.weekday
            st = d.start_time.strftime("%H:%M:%S") if d.start_time else None
            en = d.end_time.strftime("%H:%M:%S") if d.end_time else None
            ov = bool(d.overnight)
            enb = bool(d.enabled)
        else:
            wd = int(d["weekday"])
            st = _norm_t(d.get("start_time"))
            en = _norm_t(d.get("end_time"))
            ov = bool(d.get("overnight", False))
            enb = bool(d.get("enabled", True))
        norm.append((wd, st, en, ov, enb))
    return sorted(norm)


def _norm_t(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%H:%M:%S")
    s = str(val).strip()
    parts = s.split(":")
    if len(parts) == 2:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
    if len(parts) >= 3:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
    return s


def _to_time(val: Any):
    from datetime import time

    if hasattr(val, "hour") and not isinstance(val, str):
        return val
    parts = str(val).strip().split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


def pattern_signature(pattern: Pattern, *, windows: dict | None = None) -> tuple:
    w = windows or {
        "early_window_min": pattern.early_window_min,
        "grace_min": pattern.grace_min,
        "break_min": pattern.break_min,
    }
    return (int(w["early_window_min"]), int(w["grace_min"]), int(w["break_min"]), _day_sig(list(pattern.days)))


def week_signature(days: list[dict[str, Any]], windows: dict[str, Any]) -> tuple:
    w = {**DEFAULT_WINDOWS, **(windows or {})}
    return (int(w["early_window_min"]), int(w["grace_min"]), int(w["break_min"]), _day_sig(days))


def _staff(db: Session, staff: Staff | int) -> Staff:
    if isinstance(staff, Staff):
        return staff
    obj = db.get(Staff, staff)
    if obj is None or obj.anonymised:
        raise ValueError("Staff not found")
    return obj


def _active_assignment(db: Session, staff_id: int, on_date: date) -> Assignment | None:
    return db.scalar(
        select(Assignment)
        .where(
            Assignment.staff_id == staff_id,
            Assignment.effective_from <= on_date,
            (Assignment.effective_to.is_(None)) | (Assignment.effective_to >= on_date),
        )
        .order_by(Assignment.effective_from.desc(), Assignment.id.desc())
    )


def _close_assignments_from(db: Session, staff_id: int, effective_from: date) -> None:
    day_before = effective_from - timedelta(days=1)
    currents = db.scalars(
        select(Assignment).where(
            Assignment.staff_id == staff_id,
            (Assignment.effective_to.is_(None)) | (Assignment.effective_to >= effective_from),
        )
    ).all()
    for a in currents:
        if a.effective_from >= effective_from:
            db.delete(a)
        else:
            a.effective_to = day_before


def _assign(db: Session, staff_id: int, pattern_id: int, effective_from: date) -> Assignment:
    _close_assignments_from(db, staff_id, effective_from)
    a = Assignment(
        staff_id=staff_id,
        pattern_id=pattern_id,
        effective_from=effective_from,
        effective_to=None,
    )
    db.add(a)
    return a


def _refs_count(db: Session, pattern_id: int) -> tuple[int, int, int]:
    asn = db.scalar(select(func.count()).select_from(Assignment).where(Assignment.pattern_id == pattern_id)) or 0
    att = db.scalar(select(func.count()).select_from(Attendance).where(Attendance.shift_id == pattern_id)) or 0
    # shift exceptions are per (staff,date), not pattern; not a direct pattern ref
    return int(asn), 0, int(att)


def _active_personal(db: Session, staff_id: int) -> Pattern | None:
    """A non-archived personal pattern owned by staff (latest by id)."""
    return db.scalar(
        select(Pattern)
        .where(
            Pattern.owner_staff_id == staff_id,
            Pattern.kind == "personal",
            Pattern.archived_at.is_(None),
        )
        .order_by(Pattern.id.desc())
    )


def _personal_for_staff(db: Session, staff_id: int) -> list[Pattern]:
    return list(
        db.scalars(
            select(Pattern).where(Pattern.owner_staff_id == staff_id).order_by(Pattern.id)
        ).all()
    )


def _resolve_base(db: Session, staff_id: int, pattern_id: int | None = None) -> Pattern | None:
    # explicit base
    if pattern_id is not None:
        p = db.get(Pattern, pattern_id)
        if p is not None and (getattr(p, "kind", None) == "shared" or p.owner_staff_id is None):
            return p
        # if a personal was passed, try its base
        if p is not None and getattr(p, "based_on_pattern_id", None):
            return db.get(Pattern, p.based_on_pattern_id)
        return p
    # from current personal
    personal = _active_personal(db, staff_id)
    if personal is not None and getattr(personal, "based_on_pattern_id", None):
        base = db.get(Pattern, personal.based_on_pattern_id)
        if base is not None:
            return base
    # from active assignment to a shared pattern
    today = date.today()
    a = _active_assignment(db, staff_id, today)
    if a is not None:
        p = db.get(Pattern, a.pattern_id)
        if p is not None and (getattr(p, "kind", None) == "shared" or p.owner_staff_id is None):
            return p
    return None


def _archive_or_delete(db: Session, pattern: Pattern) -> str:
    asn, exc, att = _refs_count(db, pattern.id)
    if asn or exc or att:
        if hasattr(pattern, "archived_at"):
            from app.models import utcnow

            pattern.archived_at = utcnow()
        return "archived"
    db.delete(pattern)
    return "deleted"


def gc_personal_patterns(db: Session, *, staff_id: int | None = None) -> list[dict]:
    """Delete unreferenced personal copies; keep archived/referenced ones."""
    q = select(Pattern).where((Pattern.kind == "personal") | (Pattern.owner_staff_id.isnot(None)))
    if staff_id is not None:
        q = q.where(Pattern.owner_staff_id == staff_id)
    report = []
    for p in list(db.scalars(q).all()):
        asn, exc, att = _refs_count(db, p.id)
        if not (asn or exc or att):
            pid = p.id
            db.delete(p)
            report.append({"id": pid, "action": "deleted"})
        else:
            report.append({"id": p.id, "action": "kept", "refs": {"assignments": asn, "exceptions": exc, "attendance": att}})
    return report


def reset_to_schedule(
    db: Session,
    staff: Staff | int,
    pattern_id: int,
    effective_from: date,
    *,
    admin_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    st = _staff(db, staff)
    target = db.get(Pattern, pattern_id)
    if target is None:
        raise ValueError("Pattern not found")
    ending = assignments_ending_for(db, [st.id], effective_from)
    warn = past_attendance_warning(db, [st.id], [effective_from])
    personals = _personal_for_staff(db, st.id)
    personal_actions = [{"pattern_id": p.id, "action": _archive_or_delete.__name__ and ("keep_if_referenced" if _refs_count(db, p.id) != (0, 0, 0) else "delete")} for p in personals]
    if dry_run:
        return {
            "dry_run": True,
            "status": "reset",
            "staff_id": st.id,
            "pattern_id": target.id,
            "effective_from": effective_from,
            "affected_people": [st.id],
            "assignments_ending": ending,
            "personal_actions": personal_actions,
            "warning": warn,
        }
    # end personal/current and assign target
    _assign(db, st.id, target.id, effective_from)
    actions = []
    for p in _personal_for_staff(db, st.id):
        # end any assignment of this personal (covered by _assign closing) and archive/delete
        act = _archive_or_delete(db, p)
        actions.append({"pattern_id": p.id, "action": act})
    if admin_id is not None:
        record_audit(
            db, admin_id, "reset_to_schedule",
            f"staff:{st.id}", f"-> pattern:{target.id} from {effective_from}; personal={actions}",
        )
    return {"dry_run": False, "status": "reset", "staff_id": st.id, "pattern_id": target.id, "effective_from": effective_from, "personal_actions": actions}



def save_personal_week(
    db: Session,
    staff: Staff | int,
    days: list[dict[str, Any]],
    windows: dict[str, Any],
    effective_from: date,
    *,
    admin_id: int | None = None,
    base_pattern_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    st = _staff(db, staff)
    w = {**DEFAULT_WINDOWS, **(windows or {})}
    errs = validate_pattern({**w, "days": days})
    if errs:
        raise ValueError("; ".join(errs))
    base = _resolve_base(db, st.id, base_pattern_id)
    if base is None:
        raise ValueError("Base shared pattern not found for comparison")

    new_sig = week_signature(days, w)
    base_sig = pattern_signature(base)
    ending = assignments_ending_for(db, [st.id], effective_from)
    warn = past_attendance_warning(db, [st.id], [effective_from])
    if dry_run:
        action = "reset" if new_sig == base_sig else "save_personal"
        prev = _active_personal(db, st.id)
        return {
            "dry_run": True,
            "status": "reset" if action == "reset" else "personal_saved",
            "staff_id": st.id,
            "base_pattern_id": base.id,
            "effective_from": effective_from,
            "affected_people": [st.id],
            "assignments_ending": ending,
            "personal_archived": ({"pattern_id": prev.id} if (prev and action != "reset") else None),
            "warning": warn,
        }

    if new_sig == base_sig:
        res = reset_to_schedule(db, st, base.id, effective_from, admin_id=admin_id)
        res["dry_run"] = False
        return res

    # different week: archive active personal version, create+assign new personal
    prev = _active_personal(db, st.id)
    archived = None
    if prev is not None:
        act = _archive_or_delete(db, prev)
        archived = {"pattern_id": prev.id, "action": act}

    name = f"{base.name} (personal:{st.staff_code})"
    newp = Pattern(
        name=name,
        owner_staff_id=st.id,
        early_window_min=int(w["early_window_min"]),
        grace_min=int(w["grace_min"]),
        break_min=int(w["break_min"]),
        is_default=False,
        kind="personal",
        based_on_pattern_id=base.id,
        created_by=admin_id,
    )
    db.add(newp)
    db.flush()
    for d in days:
        db.add(
            PatternDay(
                pattern_id=newp.id,
                weekday=int(d["weekday"]),
                start_time=_to_time(d["start_time"]),
                end_time=_to_time(d["end_time"]),
                overnight=bool(d.get("overnight", False)),
                enabled=bool(d.get("enabled", True)),
            )
        )
    _assign(db, st.id, newp.id, effective_from)
    gc_personal_patterns(db, staff_id=st.id)
    if admin_id is not None:
        record_audit(
            db, admin_id, "save_personal_week",
            f"pattern:{newp.id}", f"staff {st.id} base {base.id} from {effective_from} archived={archived}",
        )
    return {
        "dry_run": False,
        "status": "personal_saved",
        "pattern_id": newp.id,
        "staff_id": st.id,
        "base_pattern_id": base.id,
        "effective_from": effective_from,
        "archived": archived,
    }
