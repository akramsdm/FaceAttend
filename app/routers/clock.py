"""Public kiosk clock endpoints (Phase 2 / Step G)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.attendance_service import (
    ATTENDANCE_LOCK,
    blocked_detail,
    close_open_session,
    completed_session_for_shift,
    find_open_session,
    record_event,
    state_for_staff,
)
from app.clock_tokens import consume_clock_token, verify_clock_token
from app.config import get_settings
from app.db import get_session
from app.models import ApprovalRequest, Attendance, Staff, utcnow

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
    reason_code: Literal["no_shift", "out_of_window", "early_out", "reentry"]
    note: str | None = Field(default=None, max_length=500)


def get_now() -> datetime:
    return utcnow()


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
    return db.scalar(
        select(Attendance)
        .where(
            or_(
                Attendance.token_jti == jti,
                Attendance.edit_reason == f"token:{jti}",
            )
        )
        .order_by(Attendance.id.desc())
        .limit(1)
    )


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _att_out(att: Attendance) -> dict:
    settings = get_settings()
    ts_utc = att.timestamp
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=timezone.utc)
    else:
        ts_utc = ts_utc.astimezone(timezone.utc)
    local = ts_utc.astimezone(ZoneInfo(settings.timezone))
    return {
        "id": att.id,
        "staff_id": att.staff_id,
        "action": att.type,
        # Explicit UTC (never a bare naive value) plus a display value in the
        # configured kiosk timezone.
        "timestamp": _iso_utc(ts_utc),
        "local_time": local.strftime("%Y-%m-%d %H:%M:%S"),
        "local_iso": local.isoformat(timespec="seconds"),
        "shift_id": att.shift_id,
        "shift_date": att.shift_date.isoformat() if att.shift_date else None,
        "flag": att.flag,
        "paired_in_id": att.paired_in_id,
        "undone_at": _iso_utc(att.undone_at),
    }


def _raise_blocked(detail: dict) -> None:
    raise HTTPException(status_code=409, detail=detail)


@router.post("/api/clock")
def clock_action(
    payload: ClockRequest,
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    db: Session = Depends(get_session),
    now: datetime = Depends(get_now),
):
    kiosk = kiosk_id.strip()
    with ATTENDANCE_LOCK:
        # Repeating the same token returns the original row, never a second row.
        jti = _jti_from_token(payload.token)
        if jti:
            existing = _event_for_jti(db, jti)
            if existing is not None:
                return {
                    "status": "ok",
                    "idempotent": True,
                    "attendance": _att_out(existing),
                }

        status = verify_clock_token(db, payload.token, kiosk_id=kiosk, now=now)
        if not status.ok:
            if status.reason == "used" and jti:
                existing = _event_for_jti(db, jti)
                if existing is not None:
                    return {
                        "status": "ok",
                        "idempotent": True,
                        "attendance": _att_out(existing),
                    }
            # A stale/expired token must revalidate the current state and must
            # never authorise an invalid action.
            if status.reason in ("expired", "used") and status.staff_id is not None:
                staff = db.get(Staff, status.staff_id)
                if staff is not None and staff.active and not staff.anonymised:
                    state = state_for_staff(db, staff, now)
                    detail = blocked_detail(state, payload.action)
                    if detail is not None:
                        _raise_blocked(detail)
            raise HTTPException(
                status_code=400,
                detail={"status": "invalid_token", "reason": status.reason},
            )

        staff = db.get(Staff, status.staff_id)
        if staff is None or not staff.active or staff.anonymised:
            raise HTTPException(
                status_code=400,
                detail={"status": "invalid_token", "reason": "staff_invalid"},
            )

        # Revalidate the authoritative state on every request; the scan token
        # only proves identity, it does not authorise an action.
        state = state_for_staff(db, staff, now)
        detail = blocked_detail(state, payload.action)
        if detail is not None:
            _raise_blocked(detail)

        evaluation = state["evaluation"]
        if (
            payload.action == "out"
            and evaluation.get("confirm_required")
            and not payload.confirm
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "confirm_required",
                    "message": evaluation.get("message"),
                    "flag": evaluation.get("flag"),
                },
            )

        flag = evaluation.get("flag")

        # Consume the token only once the action is known to be legitimate.
        consumed = consume_clock_token(db, payload.token, kiosk_id=kiosk, now=now)
        if not consumed.ok:
            if consumed.reason == "used" and jti:
                existing = _event_for_jti(db, jti)
                if existing is not None:
                    return {
                        "status": "ok",
                        "idempotent": True,
                        "attendance": _att_out(existing),
                    }
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail={"status": "invalid_token", "reason": consumed.reason},
            )

        if payload.action == "out":
            open_in = state["open_in"]
            current = find_open_session(db, staff.id)
            if current is None:
                db.rollback()
                _raise_blocked(
                    {
                        "status": "blocked",
                        "message": "no_open_session",
                        "allowed_actions": [],
                        "reason_code": "out_of_window",
                    }
                )
            if open_in is None or current.id != open_in.id:
                db.rollback()
                _raise_blocked(
                    {
                        "status": "blocked",
                        "message": "session_already_open",
                        "allowed_actions": ["out"],
                        "reason_code": "reentry",
                    }
                )
            att = close_open_session(
                db,
                current,
                now_utc=now,
                flag=flag,
                token_jti=jti,
            )
        else:
            shift = state["shift"]
            shift_date = state["shift_date"]
            # Concurrency re-check inside the same transaction.
            if find_open_session(db, staff.id) is not None:
                db.rollback()
                _raise_blocked(
                    {
                        "status": "blocked",
                        "message": "session_already_open",
                        "allowed_actions": ["out"],
                        "reason_code": "reentry",
                    }
                )
            if completed_session_for_shift(db, staff.id, shift_date):
                db.rollback()
                _raise_blocked(
                    {
                        "status": "blocked",
                        "message": "shift_already_completed",
                        "allowed_actions": [],
                        "reason_code": "reentry",
                    }
                )
            att = record_event(
                db,
                staff_id=staff.id,
                action="in",
                now_utc=now,
                shift_id=shift.pattern_id if shift is not None else None,
                shift_date=shift_date,
                flag=flag,
                token_jti=jti,
            )

        try:
            db.commit()
        except IntegrityError:
            # Unique open_key / token_jti index caught a concurrent duplicate.
            db.rollback()
            # If another worker committed the same token first, return its row
            # instead of misreporting a blocked session.
            if jti:
                existing = _event_for_jti(db, jti)
                if existing is not None:
                    return {
                        "status": "ok",
                        "idempotent": True,
                        "attendance": _att_out(existing),
                    }
            _raise_blocked(
                {
                    "status": "blocked",
                    "message": "session_already_open",
                    "allowed_actions": ["out"],
                    "reason_code": "reentry",
                }
            )
        db.refresh(att)
        return {"status": "ok", "idempotent": False, "attendance": _att_out(att)}


@router.post("/api/clock/undo")
def clock_undo(
    payload: UndoRequest,
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    db: Session = Depends(get_session),
    now: datetime = Depends(get_now),
):
    kiosk = kiosk_id.strip()
    with ATTENDANCE_LOCK:
        att: Attendance | None = None
        if payload.token:
            jti = _jti_from_token(payload.token)
            if jti:
                att = _event_for_jti(db, jti)
        if att is None and payload.event_id is not None:
            att = db.get(Attendance, payload.event_id)
        if att is None:
            raise HTTPException(status_code=404, detail="Event not found")

        # Binding: when a token is supplied, its identity must match the event.
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
            return {
                "status": "ok",
                "already_undone": True,
                "attendance": _att_out(att),
            }
        age = (now - att.timestamp).total_seconds()
        if age < 0:
            age = 0
        if age > UNDO_WINDOW_SECONDS:
            raise HTTPException(
                status_code=409,
                detail={"status": "undo_window_expired", "seconds": UNDO_WINDOW_SECONDS},
            )
        if att.type == "in":
            paired_out = db.scalar(
                select(Attendance.id)
                .where(
                    Attendance.paired_in_id == att.id,
                    Attendance.undone_at.is_(None),
                )
                .limit(1)
            )
            if paired_out is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "undo_not_allowed",
                        "message": "Undo the clock-out before undoing the clock-in.",
                    },
                )
            att.open_key = None
        elif att.type == "out" and att.paired_in_id is not None:
            open_in = db.get(Attendance, att.paired_in_id)
            if open_in is not None and open_in.undone_at is None:
                # Re-open the session that this clock-out closed, unless another
                # session is already open.
                if find_open_session(db, att.staff_id) is None:
                    open_in.open_key = str(att.staff_id)
        att.undone_at = now
        db.commit()
        db.refresh(att)
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
        status = verify_clock_token(db, payload.token, kiosk_id=kiosk, now=now)
        if not status.ok and status.reason not in ("used",):
            raise HTTPException(
                status_code=400,
                detail={"status": "invalid_token", "reason": status.reason},
            )
        staff_id = status.staff_id or staff_id
    if staff_id is None:
        raise HTTPException(status_code=422, detail="staff_id or valid token required")
    req = ApprovalRequest(
        staff_id=staff_id,
        action=payload.action,
        reason_code=payload.reason_code,
        requested_at=now,
        status="pending",
        decision_note=payload.note,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return {
        "id": req.id,
        "status": req.status,
        "reason_code": req.reason_code,
        "action": req.action,
    }
