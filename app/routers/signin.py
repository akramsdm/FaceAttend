"""Public kiosk sign-in endpoint."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import rules, signin
from app.config import get_settings
from app.db import get_session
from app.models import Attendance, Staff, utcnow
from app.recognition import GalleryCache, Recognizer
from app.settings_store import get_value

router = APIRouter(tags=["signin"])


class SigninResponse(BaseModel):
    status: str
    name: str | None = None
    action: Literal["in", "out"] | None = None
    time: datetime | None = None


def get_recognizer(request: Request) -> Recognizer:
    recognizer = getattr(request.app.state, "recognizer", None)
    if recognizer is None:
        raise HTTPException(
            status_code=503,
            detail="Face recognition is unavailable. Try again later or sign in manually.",
        )
    return recognizer


def get_gallery(request: Request) -> GalleryCache:
    gallery = getattr(request.app.state, "gallery", None)
    if gallery is None:
        raise HTTPException(
            status_code=503,
            detail="Face recognition is unavailable. Try again later or sign in manually.",
        )
    return gallery


def get_now() -> datetime:
    return utcnow()


def _eligible_staff(db: Session, staff_id: int | None) -> Staff | None:
    if staff_id is None:
        return None
    staff = db.get(Staff, staff_id)
    if staff is None or not staff.active or staff.anonymised:
        return None
    return staff


@router.post("/api/signin", response_model=SigninResponse)
def sign_in(
    image: UploadFile = File(...),
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    db: Session = Depends(get_session),
    recognizer: Recognizer = Depends(get_recognizer),
    gallery: GalleryCache = Depends(get_gallery),
    now: datetime = Depends(get_now),
) -> SigninResponse:
    settings = get_settings()
    kiosk = kiosk_id.strip()
    if not kiosk:
        raise HTTPException(status_code=422, detail="X-Kiosk-ID header is required")

    match_threshold = get_value(db, "match_threshold")
    ambiguity_margin = get_value(db, "ambiguity_margin")
    cooldown_seconds = get_value(db, "cooldown_seconds")


    image_bytes = image.file.read()

    try:
        analysis = recognizer.analyse_frame(image_bytes)
    except ValueError:
        signin.record_attempt(
            db,
            kiosk_id=kiosk,
            outcome="rejected",
            now=now,
            score=None,
        )
        db.commit()
        return SigninResponse(status="invalid_image")

    if analysis.status != "ok" or analysis.face is None:
        status = "not_recognised" if analysis.status == "ok" else analysis.status
        outcome = "no_face" if status == "no_face" else "rejected"
        signin.record_attempt(
            db,
            kiosk_id=kiosk,
            outcome=outcome,
            now=now,
            frame=analysis.frame,
        )
        db.commit()
        return SigninResponse(status=status)

    try:
        embedding = recognizer.embed(analysis.frame, analysis.face)
    except ValueError:
        signin.record_attempt(
            db,
            kiosk_id=kiosk,
            outcome="rejected",
            now=now,
            frame=analysis.frame,
        )
        db.commit()
        return SigninResponse(status="not_recognised")

    best_staff_id, best_score, second_score = gallery.match_embedding(embedding)
    decision = rules.decide(
        best_score,
        second_score,
        match_threshold,
        ambiguity_margin,
    )
    staff = _eligible_staff(db, best_staff_id)

    if decision == "matched" and staff is None:
        decision = "rejected"

    if decision == "matched" and staff is not None:
        recent = signin.attendance_within_cooldown(
            db, staff.id, now, cooldown_seconds
        )
        if recent is not None:
            signin.record_attempt(
                db,
                kiosk_id=kiosk,
                outcome="matched",
                now=now,
                best_staff_id=staff.id,
                score=best_score,
            )
            db.commit()
            return SigninResponse(
                status="already_recorded",
                name=staff.name,
                action=recent.type,
                time=signin.to_local_time(recent.timestamp, settings.timezone),
            )

        action = signin.next_action(db, staff.id, now, settings.timezone)
        db.add(
            Attendance(
                staff_id=staff.id,
                type=action,
                timestamp=now,
                method="face",
                distance=None if best_score is None else 1.0 - best_score,
                model_version=getattr(recognizer, "model_version", None),
            )
        )
        signin.record_attempt(
            db,
            kiosk_id=kiosk,
            outcome="matched",
            now=now,
            best_staff_id=staff.id,
            score=best_score,
        )
        db.commit()
        return SigninResponse(
            status="ok",
            name=staff.name,
            action=action,
            time=signin.to_local_time(now, settings.timezone),
        )

    attempt_outcome = "ambiguous" if decision == "ambiguous" else "rejected"
    signin.record_attempt(
        db,
        kiosk_id=kiosk,
        outcome=attempt_outcome,
        now=now,
        best_staff_id=staff.id if staff is not None else None,
        score=best_score,
        frame=analysis.frame,
    )
    db.commit()

    return SigninResponse(
        status="ambiguous" if attempt_outcome == "ambiguous" else "not_recognised"
    )
