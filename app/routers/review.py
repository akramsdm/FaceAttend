"""Admin review queue: list unresolved attempts, resolve (assign/dismiss)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.attendance_service import close_open_session, record_event, state_for_staff
from app.audit import record_audit
from app.db import get_session
from app.models import Admin, RecognitionAttempt, Staff
from app.security import require_admin, require_admin_csrf
from app.signin import ROOT, iso_utc

router = APIRouter(prefix="/api/admin/review", tags=["review"])


class ReviewItem(BaseModel):
    id: int
    timestamp: datetime
    kiosk_id: str
    outcome: str
    best_staff_id: int | None
    best_staff_name: str | None
    score: float | None
    frame_path: str | None
    resolved: bool

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str | None:
        return iso_utc(value)


class ResolvePayload(BaseModel):
    action: Literal["assign", "dismiss"]
    staff_id: int | None = None
    reason: str = Field(min_length=1, max_length=500)


def _resolve_frame(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = ROOT / p
    return p


def _delete_frame(path_str: str | None) -> None:
    p = _resolve_frame(path_str)
    if p and p.exists():
        try:
            p.unlink()
        except OSError:
            pass


@router.get("", response_model=list[ReviewItem])
def list_review_queue(
    resolved: bool = False,
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[ReviewItem]:
    stmt = (
        select(RecognitionAttempt, Staff)
        .outerjoin(Staff, Staff.id == RecognitionAttempt.best_staff_id)
        .where(RecognitionAttempt.resolved.is_(resolved))
        .order_by(RecognitionAttempt.timestamp.asc(), RecognitionAttempt.id.asc())
    )
    out = []
    for attempt, staff in db.execute(stmt).all():
        out.append(
            ReviewItem(
                id=attempt.id,
                timestamp=attempt.timestamp,
                kiosk_id=attempt.kiosk_id,
                outcome=attempt.outcome,
                best_staff_id=attempt.best_staff_id,
                best_staff_name=staff.name if staff else None,
                score=attempt.score,
                frame_path=attempt.frame_path,
                resolved=attempt.resolved,
            )
        )
    return out


@router.get("/{attempt_id}/frame")
def get_review_frame(
    attempt_id: int,
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
):
    attempt = db.get(RecognitionAttempt, attempt_id)
    if attempt is None or not attempt.frame_path:
        raise HTTPException(status_code=404, detail="Frame not found")
    p = _resolve_frame(attempt.frame_path)
    if p is None or not p.exists():
        raise HTTPException(status_code=404, detail="Frame not found")
    return FileResponse(p, media_type="image/jpeg")


@router.post("/{attempt_id}/resolve")
def resolve_attempt(
    attempt_id: int,
    payload: ResolvePayload,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
) -> dict:
    attempt = db.get(RecognitionAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.resolved:
        raise HTTPException(status_code=409, detail="Attempt already resolved")

    result: dict = {"id": attempt.id, "action": payload.action}
    if payload.action == "assign":
        if payload.staff_id is None:
            raise HTTPException(status_code=422, detail="staff_id is required for assign")
        staff = db.get(Staff, payload.staff_id)
        if staff is None or staff.anonymised:
            raise HTTPException(status_code=404, detail="Staff not found")
        # The backend decides in/out from the authoritative lifecycle state at
        # the attempt time, so a review assignment can never create an orphan
        # clock-out or a duplicate open session.
        state = state_for_staff(db, staff, attempt.timestamp)
        reason = payload.reason.strip()
        if state["open_in"] is not None:
            open_in = state["open_in"]
            # A clock-out can never precede the clock-in it closes.
            if attempt.timestamp < open_in.timestamp:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "blocked",
                        "message": "attempt_before_open_session",
                        "reason_code": "out_of_window",
                    },
                )
            row = close_open_session(
                db,
                open_in,
                now_utc=attempt.timestamp,
                method="review",
                edited_by=admin.id,
                edit_reason=reason,
            )
        elif state["completed"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "blocked",
                    "message": "shift_already_completed",
                    "reason_code": "reentry",
                },
            )
        else:
            shift = state["shift"]
            row = record_event(
                db,
                staff_id=staff.id,
                action="in",
                now_utc=attempt.timestamp,
                shift_id=getattr(shift, "pattern_id", None),
                shift_date=state["shift_date"],
                flag=(state["evaluation"] or {}).get("flag"),
                method="review",
                edited_by=admin.id,
                edit_reason=reason,
            )
        result["attendance_id"] = row.id
        result["attendance_type"] = row.type
        record_audit(db, admin.id, "resolve_review_assign", f"attempt:{attempt.id}", reason)
    else:
        record_audit(db, admin.id, "resolve_review_dismiss", f"attempt:{attempt.id}", payload.reason.strip())

    _delete_frame(attempt.frame_path)
    attempt.resolved = True
    attempt.frame_path = None
    db.commit()
    return result
