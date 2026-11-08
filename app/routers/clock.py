"""Public kiosk clock endpoints (Phase 2 / Step G)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock_tokens import consume_clock_token, verify_clock_token
from app.config import get_settings
from app.db import get_session
from app.models import ApprovalRequest, Attendance, ClockToken, Staff, utcnow
from app.routers.shifts import _shifts_data
from app.shifts import evaluate, resolve_shift, shift_date_for

router = APIRouter(tags=["clock"])
UNDO_WINDOW_SECONDS = 10


class ClockRequest(BaseModel):
    token: str = Field(min_length=10)
    action: Literal["in", "out"]
    confirm: bool = False


class UndoRequest(BaseModel):
    token: str | None = None
    event_id: int | None = None


class ApprovalCreate(BaseModel):
    token: str | None = None
    staff_id: int | None = None
    action: Literal["in", "out"]
    reason_code: Literal["no_shift", "out_of_window", "early_out"]
    note: str | None = Field(default=None, max_length=500)


def get_now() -> datetime:
    return utcnow()


def _local_now(now: datetime, timezone_name: str) -> datetime:
    from datetime import timezone as _tz
    return now.replace(tzinfo=_tz.utc).astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)


def _has_open_clockin(db: Session, staff_id: int, now: datetime) -> bool:
    cutoff = now - timedelta(days=2)
    rows = db.scalars(
        select(Attendance)
        .where(Attendance.staff_id == staff_id, Attendance.undone_at.is_(None),
               Attendance.timestamp >= cutoff)
        .order_by(Attendance.timestamp.desc(), Attendance.id.desc())
    ).all()
    for r in rows:
        if r.type == "in":
            return True
        if r.type == "out":
            return False
    return False


def _resolve_today(db: Session, staff_id: int, local_now: datetime):
    data = _shifts_data(db)
    today = local_now.date()
    candidates = []
    for d in (today, today - timedelta(days=1)):
        sh = resolve_shift(staff_id, d, data)
        if sh is None:
            continue
        if d == today or (sh.overnight and local_now <= sh.end):
            candidates.append(sh)
    if not candidates:
        return None
    for sh in candidates:
        if sh.start - timedelta(minutes=sh.early_window) <= local_now <= sh.end + timedelta(hours=4):
            return sh
    return candidates[0]


def _jti_from_token(token: str) -> str | None:
    try:
        import base64
        body_b64 = token.rsplit(".", 1)[0]
        pad = "=" * (-len(body_b64) % 4)
        payload = base64.urlsafe_b64decode(body_b64 + pad).decode("utf-8")
        return payload.split(".", 1)[0]
    except Exception:
        return None


def _event_for_jti(db: Session, jti: str) -> Attendance | None:
    # store jti in model_version as marker? safer: use a deterministic lookup by token row's
    # created attendance via ClockToken -> no FK; instead mark Attendance.edit_reason prefix.
    marker = f"token:{jti}"
    return db.scalar(select(Attendance).where(Attendance.edit_reason == marker).order_by(Attendance.id.desc()))


def _att_out(att: Attendance) -> dict:
    return {"id": att.id, "staff_id": att.staff_id, "action": att.type,
            "timestamp": att.timestamp.isoformat().replace("+00:00", "Z"),
            "shift_id": att.shift_id, "shift_date": att.shift_date.isoformat() if att.shift_date else None,
            "flag": att.flag, "undone_at": att.undone_at.isoformat().replace("+00:00","Z") if att.undone_at else None}


def _reason_code_for_block(message: str | None) -> str | None:
    if message == "no_shift_request_approval":
        return "no_shift"
    if message == "before_window":
        return "out_of_window"
    if message == "clock_out_early_confirm":
        return "early_out"
    return None


@router.post("/api/clock")
def clock_action(
    payload: ClockRequest,
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    db: Session = Depends(get_session),
    now: datetime = Depends(get_now),
):
    settings = get_settings()
    kiosk = kiosk_id.strip()
    # pre-check jti idempotency (repeating same token -> same result, never 2nd row)
    jti = _jti_from_token(payload.token)
    if jti:
        existing = _event_for_jti(db, jti)
        if existing is not None:
            return {"status": "ok", "idempotent": True, "attendance": _att_out(existing)}
    # verify then consume (single-use). if used, treat as idempotent only if we had event - but
    # event marker is our source; consume will reject used; re-check after.
    st = verify_clock_token(db, payload.token, kiosk_id=kiosk, now=now)
    if not st.ok:
        if st.reason == "used" and jti:
            ex = _event_for_jti(db, jti)
            if ex is not None:
                return {"status": "ok", "idempotent": True, "attendance": _att_out(ex)}
        raise HTTPException(status_code=400, detail={"status": "invalid_token", "reason": st.reason})
    staff = db.get(Staff, st.staff_id)
    if staff is None or not staff.active or staff.anonymised:
        raise HTTPException(status_code=400, detail={"status": "invalid_token", "reason": "staff_invalid"})
    local_now = _local_now(now, settings.timezone)
    sh = _resolve_today(db, staff.id, local_now)
    open_session = _has_open_clockin(db, staff.id, now)
    ev = evaluate(local_now, sh, open_session, margin_overtime=0)
    allowed = list(ev.get("allowed_actions") or [])
    if payload.action not in allowed:
        raise HTTPException(status_code=409, detail={
            "status": "blocked", "message": ev.get("message"), "allowed_actions": allowed,
            "reason_code": _reason_code_for_block(ev.get("message")),
        })
    if ev.get("confirm_required") and payload.action == "out" and not payload.confirm:
        raise HTTPException(status_code=409, detail={
            "status": "confirm_required", "message": ev.get("message"),
            "flag": ev.get("flag"),
        })
    # consume token (single-use) before creating row
    cst = consume_clock_token(db, payload.token, kiosk_id=kiosk, now=now)
    if not cst.ok:
        if cst.reason == "used" and jti:
            ex = _event_for_jti(db, jti)
            if ex is not None:
                return {"status": "ok", "idempotent": True, "attendance": _att_out(ex)}
        raise HTTPException(status_code=400, detail={"status": "invalid_token", "reason": cst.reason})
    flag = ev.get("flag") or "none"
    shift_id = None
    # resolve pattern id if we can
    sdate = shift_date_for(local_now, sh) if sh is not None else local_now.date()
    att = Attendance(staff_id=staff.id, type=payload.action, timestamp=now, method="face",
                     shift_id=shift_id, shift_date=sdate,
                     flag=None if flag == "none" else flag,
                     edit_reason=f"token:{jti}" if jti else None)
    db.add(att)
    db.commit(); db.refresh(att)
    return {"status": "ok", "idempotent": False, "attendance": _att_out(att)}


@router.post("/api/clock/undo")
def clock_undo(
    payload: UndoRequest,
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    db: Session = Depends(get_session),
    now: datetime = Depends(get_now),
):
    kiosk = kiosk_id.strip()
    att: Attendance | None = None
    staff_id = None
    if payload.token:
        st = verify_clock_token(db, payload.token, kiosk_id=kiosk, now=now)
        # token may be used (normal after clock) - allow locating event; bind to its identity
        jti = _jti_from_token(payload.token)
        if jti:
            att = _event_for_jti(db, jti)
        if att is None and st.ok:
            staff_id = st.staff_id
    if att is None and payload.event_id is not None:
        att = db.get(Attendance, payload.event_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Event not found")
    # binding: if token identity known, must match
    if payload.token:
        try:
            import base64
            body_b64 = payload.token.rsplit(".", 1)[0]
            pad = "=" * (-len(body_b64) % 4)
            pl = base64.urlsafe_b64decode(body_b64 + pad).decode("utf-8")
            _, sid_s, kid, _ = pl.split(".", 3)
            if int(sid_s) != att.staff_id or kid != kiosk:
                raise HTTPException(status_code=403, detail="Token does not match event")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid token")
    if att.undone_at is not None:
        return {"status": "ok", "already_undone": True, "attendance": _att_out(att)}
    age = (now - att.timestamp).total_seconds()
    if age < 0:
        age = 0
    if age > UNDO_WINDOW_SECONDS:
        raise HTTPException(status_code=409, detail={"status": "undo_window_expired", "seconds": UNDO_WINDOW_SECONDS})
    att.undone_at = now
    db.commit(); db.refresh(att)
    return {"status": "ok", "already_undone": False, "attendance": _att_out(att)}


@router.post("/api/clock/request", status_code=201)
def clock_request(
    payload: ApprovalCreate,
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    db: Session = Depends(get_session),
    now: datetime = Depends(get_now),
):
    kiosk = kiosk_id.strip()
    staff_id = payload.staff_id
    if payload.token:
        st = verify_clock_token(db, payload.token, kiosk_id=kiosk, now=now)
        if not st.ok and st.reason not in ("used",):
            raise HTTPException(status_code=400, detail={"status": "invalid_token", "reason": st.reason})
        staff_id = st.staff_id or staff_id
    if staff_id is None:
        raise HTTPException(status_code=422, detail="staff_id or valid token required")
    req = ApprovalRequest(staff_id=staff_id, action=payload.action, reason_code=payload.reason_code,
                          requested_at=now, status="pending", decision_note=payload.note)
    db.add(req); db.commit(); db.refresh(req)
    return {"id": req.id, "status": req.status, "reason_code": req.reason_code, "action": req.action}
