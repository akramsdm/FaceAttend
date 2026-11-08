"""Manager approval queue and missing clock-out admin (Phase 2 / Step I)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approvals import create_attendance_for_approval
from app.audit import record_audit
from app.db import get_session
from app.models import Admin, ApprovalRequest, Attendance, Staff, utcnow
from app.routers.shifts import _shifts_data
from app.security import ROLE_ADMIN, ROLE_MANAGER, can_access_staff, require_role
from app.shifts import resolve_shift

router = APIRouter(prefix="/api/admin/approvals", tags=["approvals"])


class ApprovalOut(BaseModel):
    id: int
    staff_id: int
    staff_name: str | None
    department: str | None
    action: str
    reason_code: str
    requested_at: datetime
    status: str
    decided_at: datetime | None
    decision_note: str | None
    today_shift: str | None


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class CloseTimeIn(BaseModel):
    timestamp: datetime
    reason: str = Field(min_length=1, max_length=500)


def _visible_staff(db: Session, admin: Admin, staff: Staff | None) -> bool:
    if staff is None:
        return False
    if (getattr(admin, "role", None) or ROLE_ADMIN) == ROLE_ADMIN:
        return True
    return can_access_staff(admin, staff)


def _today_shift_summary(db: Session, staff_id: int) -> str | None:
    from app.config import get_settings
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo
    local = datetime.now(_tz.utc).astimezone(ZoneInfo(get_settings().timezone)).date()
    sh = resolve_shift(staff_id, local, _shifts_data(db))
    if sh is None:
        return "No shift today"
    return f"{sh.start.strftime('%H:%M')}–{sh.end.strftime('%H:%M')}{' (overnight)' if sh.overnight else ''}"


@router.get("", response_model=list[ApprovalOut])
def list_approvals(status: str = "pending",
                   admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
                   db: Session = Depends(get_session)):
    rows = db.scalars(select(ApprovalRequest).where(ApprovalRequest.status == status)
                      .order_by(ApprovalRequest.requested_at.asc())).all()
    out = []
    for r in rows:
        st = db.get(Staff, r.staff_id)
        if not _visible_staff(db, admin, st):
            continue
        out.append(ApprovalOut(id=r.id, staff_id=r.staff_id, staff_name=st.name if st else None,
                               department=st.department if st else None, action=r.action, reason_code=r.reason_code,
                               requested_at=r.requested_at, status=r.status, decided_at=r.decided_at,
                               decision_note=r.decision_note, today_shift=_today_shift_summary(db, r.staff_id)))
    return out


def _load_req(db: Session, admin: Admin, req_id: int) -> ApprovalRequest:
    r = db.get(ApprovalRequest, req_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Request not found")
    st = db.get(Staff, r.staff_id)
    if not _visible_staff(db, admin, st):
        raise HTTPException(status_code=403, detail="Not in department scope")
    return r


@router.post("/{req_id}/approve")
def approve_request(req_id: int, payload: DecisionIn,
                    admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
                    db: Session = Depends(get_session)):
    r = _load_req(db, admin, req_id)
    if r.status != "pending":
        raise HTTPException(status_code=409, detail="Request already decided")
    now = utcnow()
    att = create_attendance_for_approval(db, r, admin_id=admin.id, now=now)
    r.status = "approved"; r.decided_by = admin.id; r.decided_at = now; r.decision_note = payload.note
    record_audit(db, admin.id, "approve_request", f"approval:{r.id}",
                 f"staff:{r.staff_id} {r.action} {r.reason_code} -> attendance:{att.id}")
    db.commit()
    return {"status": "approved", "attendance_id": att.id, "flag": att.flag}


@router.post("/{req_id}/reject")
def reject_request(req_id: int, payload: DecisionIn,
                   admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
                   db: Session = Depends(get_session)):
    r = _load_req(db, admin, req_id)
    if r.status != "pending":
        raise HTTPException(status_code=409, detail="Request already decided")
    r.status = "rejected"; r.decided_by = admin.id; r.decided_at = utcnow(); r.decision_note = payload.note
    record_audit(db, admin.id, "reject_request", f"approval:{r.id}",
                 f"staff:{r.staff_id} {r.action} {r.reason_code} note={payload.note or ''}")
    db.commit()
    return {"status": "rejected"}


@router.get("/missing-clockout")
def list_missing_clockouts(admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
                           db: Session = Depends(get_session)):
    rows = db.scalars(select(Attendance).where(
        Attendance.type == "out", Attendance.flag == "missing_clock_out", Attendance.undone_at.is_(None)
    ).order_by(Attendance.timestamp.desc())).all()
    out = []
    for a in rows:
        st = db.get(Staff, a.staff_id)
        if not _visible_staff(db, admin, st):
            continue
        out.append({"id": a.id, "staff_id": a.staff_id, "staff_name": st.name if st else None,
                    "department": st.department if st else None, "shift_date": str(a.shift_date) if a.shift_date else None,
                    "timestamp": a.timestamp.isoformat().replace("+00:00","Z"), "edit_reason": a.edit_reason})
    return out


@router.post("/missing-clockout/{attendance_id}/close")
def correct_missing_clockout(attendance_id: int, payload: CloseTimeIn,
                             admin: Admin = Depends(require_role(ROLE_ADMIN, ROLE_MANAGER)),
                             db: Session = Depends(get_session)):
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo
    from app.config import get_settings
    a = db.get(Attendance, attendance_id)
    if a is None or a.flag != "missing_clock_out":
        raise HTTPException(status_code=404, detail="Missing clock-out record not found")
    st = db.get(Staff, a.staff_id)
    if not _visible_staff(db, admin, st):
        raise HTTPException(status_code=403, detail="Not in department scope")
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")
    ts = payload.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo(get_settings().timezone))
    a.timestamp = ts.astimezone(_tz.utc).replace(tzinfo=None)
    a.flag = "missing_clock_out_corrected"
    a.edited_by = admin.id
    a.edit_reason = reason
    record_audit(db, admin.id, "correct_missing_clockout", f"attendance:{a.id}", reason)
    db.commit()
    return {"status": "ok", "id": a.id}
