"""Public kiosk sign-in/scan endpoints."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import attendance_service, rules, signin
from app.attendance_service import close_open_session, record_event, state_for_staff
from app.clock_tokens import TOKEN_TTL_SECONDS, issue_clock_token
from app.config import get_settings
from app.db import get_session
from app.models import Attendance, Staff, utcnow
from app.recognition import GalleryCache, Recognizer
from app.settings_store import get_value
from app.spoof import confirm_head_turn

MAX_IMAGE_BYTES = 5 * 1024 * 1024

router = APIRouter(tags=["signin"])


class SigninResponse(BaseModel):
    status: str
    name: str | None = None
    action: Literal["in", "out"] | None = None
    time: datetime | None = None
    challenge: Literal["left", "right"] | None = None


class ScanResponse(BaseModel):
    status: str
    name: str | None = None
    department: str | None = None
    shift_summary: str | None = None
    shift_start: str | None = None
    shift_end: str | None = None
    next_shift_summary: str | None = None
    next_shift_start: str | None = None
    next_shift_end: str | None = None
    allowed_actions: list[str] = []
    message: str | None = None
    state: str | None = None
    token: str | None = None
    token_expires_in: int | None = None
    challenge: Literal["left", "right"] | None = None


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


def _iso_local(dt: datetime | None) -> str | None:
    """Shift start/end are naive local wall-clock values; never tag them Z."""
    return dt.replace(tzinfo=None).isoformat(timespec="seconds") if dt else None


def _iso_utc(dt: datetime | None) -> str | None:
    """Serialise a stored naive-UTC datetime with an explicit ``Z`` suffix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _shift_summary(sh) -> str | None:
    if sh is None:
        return "No shift today"
    def hm(dt):
        return dt.strftime("%H:%M")
    return f"{hm(sh.start)}–{hm(sh.end)}{' (overnight)' if sh.overnight else ''}"


def _next_shift_summary(sh) -> str | None:
    if sh is None:
        return None
    return (
        f"{sh.start.strftime('%a %d %b %H:%M')}–{sh.end.strftime('%H:%M')}"
        f"{' (overnight)' if sh.overnight else ''}"
    )


def _recognise(image_bytes: bytes, *, kiosk: str, db: Session, recognizer: Recognizer,
                gallery: GalleryCache, now: datetime, settings, match_threshold, ambiguity_margin,
                spoof_on: bool, challenge: str | None, challenge_image: UploadFile | None):
    """Shared recognition/quality/logging. Returns (kind, payload_or_staff, analysis, best_score).
    kind: ok_match | challenge_required | status (rejection)."""
    try:
        analysis = recognizer.analyse_frame(image_bytes)
    except ValueError:
        signin.record_attempt(db, kiosk_id=kiosk, outcome="rejected", now=now, score=None)
        db.commit()
        return ("status", {"status": "invalid_image", "name": None}, None, None)

    if analysis.status != "ok" or analysis.face is None:
        status = "not_recognised" if analysis.status == "ok" else analysis.status
        outcome = "no_face" if status == "no_face" else "rejected"
        signin.record_attempt(db, kiosk_id=kiosk, outcome=outcome, now=now, frame=analysis.frame)
        db.commit()
        return ("status", {"status": status, "name": None}, analysis, None)

    try:
        embedding = recognizer.embed(analysis.frame, analysis.face)
    except ValueError:
        signin.record_attempt(db, kiosk_id=kiosk, outcome="rejected", now=now, frame=analysis.frame)
        db.commit()
        return ("status", {"status": "not_recognised", "name": None}, analysis, None)

    best_staff_id, best_score, second_score = gallery.match_embedding(embedding)
    decision = rules.decide(best_score, second_score, match_threshold, ambiguity_margin)
    staff = _eligible_staff(db, best_staff_id)
    if decision == "matched" and staff is None:
        decision = "rejected"

    if decision == "matched" and staff is not None:
        if spoof_on:
            if challenge and challenge in ("left", "right") and challenge_image is not None:
                chal_bytes = challenge_image.file.read()
                max_bytes = getattr(settings, "max_upload_bytes", MAX_IMAGE_BYTES)
                if len(chal_bytes) > max_bytes:
                    chal_analysis = None
                else:
                    try:
                        chal_analysis = recognizer.analyse_frame(chal_bytes)
                    except ValueError:
                        chal_analysis = None
                if (chal_analysis is None or chal_analysis.status != "ok" or chal_analysis.landmarks is None
                        or analysis.landmarks is None
                        or not confirm_head_turn(analysis.landmarks, chal_analysis.landmarks, challenge)):
                    signin.record_attempt(db, kiosk_id=kiosk, outcome="spoof", now=now,
                                          best_staff_id=staff.id, score=best_score, frame=analysis.frame)
                    db.commit()
                    return ("status", {"status": "spoof", "name": None}, analysis, best_score)
            else:
                import secrets as _secrets
                direction = "left" if int.from_bytes(_secrets.token_bytes(1), "big") % 2 == 0 else "right"
                return ("challenge_required", {"challenge": direction}, analysis, best_score)
        return ("ok_match", staff, analysis, best_score)

    attempt_outcome = "ambiguous" if decision == "ambiguous" else "rejected"
    signin.record_attempt(db, kiosk_id=kiosk, outcome=attempt_outcome, now=now,
                          best_staff_id=staff.id if staff is not None else None,
                          score=best_score, frame=analysis.frame)
    db.commit()
    st = "ambiguous" if attempt_outcome == "ambiguous" else "not_recognised"
    return ("status", {"status": st, "name": None}, analysis, best_score)


@router.post("/api/scan", response_model=ScanResponse)
def scan_face(
    image: UploadFile = File(...),
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    challenge: str | None = Form(default=None),
    challenge_image: UploadFile | None = File(default=None),
    db: Session = Depends(get_session),
    recognizer: Recognizer = Depends(get_recognizer),
    gallery: GalleryCache = Depends(get_gallery),
    now: datetime = Depends(get_now),
) -> ScanResponse:
    settings = get_settings()
    kiosk = kiosk_id.strip()
    if not kiosk:
        raise HTTPException(status_code=422, detail="X-Kiosk-ID header is required")
    match_threshold = get_value(db, "match_threshold")
    ambiguity_margin = get_value(db, "ambiguity_margin")
    image_bytes = image.file.read()
    max_bytes = getattr(settings, "max_upload_bytes", MAX_IMAGE_BYTES)
    if len(image_bytes) > max_bytes:
        return ScanResponse(status="invalid_image")
    spoof_on = get_value(db, "spoof_challenge")
    kind, val, analysis, best_score = _recognise(
        image_bytes, kiosk=kiosk, db=db, recognizer=recognizer, gallery=gallery, now=now,
        settings=settings, match_threshold=match_threshold, ambiguity_margin=ambiguity_margin,
        spoof_on=spoof_on, challenge=challenge, challenge_image=challenge_image)
    if kind == "challenge_required":
        return ScanResponse(status="challenge_required", challenge=val["challenge"])
    if kind == "status":
        return ScanResponse(status=val["status"])
    staff = val
    # matched: log attempt, issue token + shift/evaluate (no attendance row recorded here)
    signin.record_attempt(db, kiosk_id=kiosk, outcome="matched", now=now,
                          best_staff_id=staff.id, score=best_score)
    # The backend is the single source of truth for what is allowed now.
    state = state_for_staff(db, staff, now)
    shift = state["shift"]
    next_shift = state.get("next_shift")
    evaluation = state["evaluation"]
    token, _row = issue_clock_token(db, staff_id=staff.id, kiosk_id=kiosk, now=now, ttl_seconds=TOKEN_TTL_SECONDS)
    db.commit()
    return ScanResponse(
        status="ok",
        name=staff.name,
        department=staff.department,
        shift_summary=_shift_summary(shift),
        shift_start=_iso_local(shift.start) if shift is not None else None,
        shift_end=_iso_local(shift.end) if shift is not None else None,
        next_shift_summary=_next_shift_summary(next_shift),
        next_shift_start=_iso_local(next_shift.start) if next_shift is not None else None,
        next_shift_end=_iso_local(next_shift.end) if next_shift is not None else None,
        allowed_actions=list(evaluation.get("allowed_actions") or []),
        message=evaluation.get("message"),
        state=state["state"],
        token=token,
        token_expires_in=TOKEN_TTL_SECONDS,
    )


# --- legacy /api/signin kept working for existing tests (step 8 in/out behavior) ---
def _resp(payload: dict[str, object]) -> JSONResponse:
    return JSONResponse(payload)


@router.post("/api/signin", response_model=SigninResponse)
def sign_in(
    image: UploadFile = File(...),
    kiosk_id: str = Header(alias="X-Kiosk-ID", min_length=1, max_length=100),
    challenge: str | None = Form(default=None),
    challenge_image: UploadFile | None = File(default=None),
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
    max_bytes = getattr(settings, "max_upload_bytes", MAX_IMAGE_BYTES)
    if len(image_bytes) > max_bytes:
        return _resp({"status": "invalid_image", "name": None, "action": None, "time": None})
    spoof_on = get_value(db, "spoof_challenge")
    kind, val, analysis, best_score = _recognise(
        image_bytes, kiosk=kiosk, db=db, recognizer=recognizer, gallery=gallery, now=now,
        settings=settings, match_threshold=match_threshold, ambiguity_margin=ambiguity_margin,
        spoof_on=spoof_on, challenge=challenge, challenge_image=challenge_image)
    if kind == "challenge_required":
        return {"status":"challenge_required","name":None,"action":None,"time":None,"challenge":val["challenge"]}
    if kind == "status":
        return _resp({"status":val["status"],"name":None,"action":None,"time":None})
    staff = val
    recent = signin.attendance_within_cooldown(db, staff.id, now, cooldown_seconds)
    if recent is not None:
        signin.record_attempt(db, kiosk_id=kiosk, outcome="matched", now=now, best_staff_id=staff.id, score=best_score)
        db.commit()
        return _resp({
            "status": "already_recorded",
            "name": staff.name,
            "action": recent.type,
            "time": _iso_utc(recent.timestamp),
        })

    # Legacy endpoint: still enforce the lifecycle (no orphan clock-outs, no
    # duplicate clock-ins, no reopening a completed shift).
    state = state_for_staff(db, staff, now)
    if state["open_in"] is not None:
        flag = (state["evaluation"] or {}).get("flag")
        att = close_open_session(db, state["open_in"], now_utc=now, flag=flag, method="face")
        action = "out"
    else:
        if state["completed"]:
            signin.record_attempt(db, kiosk_id=kiosk, outcome="matched", now=now,
                                  best_staff_id=staff.id, score=best_score)
            db.commit()
            return JSONResponse(
                status_code=409,
                content={
                    "status": "blocked",
                    "name": staff.name,
                    "action": None,
                    "time": None,
                    "code": "shift_already_completed",
                    "message": "This employee has already completed their shift. Manager approval is required for re-entry.",
                },
            )
        evaluation = state["evaluation"] or {}
        if (
            state["shift"] is not None
            and evaluation.get("message") in ("before_window", "after_shift_end")
        ):
            signin.record_attempt(db, kiosk_id=kiosk, outcome="matched", now=now,
                                  best_staff_id=staff.id, score=best_score)
            db.commit()
            return JSONResponse(
                status_code=409,
                content={
                    "status": "blocked",
                    "name": staff.name,
                    "action": None,
                    "time": None,
                    "code": evaluation.get("message"),
                    "message": "Clock-in is outside the permitted window. Manager approval is required.",
                },
            )
        shift = state["shift"]
        att = record_event(
            db,
            staff_id=staff.id,
            action="in",
            now_utc=now,
            shift_id=shift.pattern_id if shift is not None else None,
            shift_date=state["shift_date"],
            flag=evaluation.get("flag"),
            method="face",
        )
        action = "in"

    att.distance = None if best_score is None else 1.0 - best_score
    att.model_version = getattr(recognizer, "model_version", None)
    signin.record_attempt(db, kiosk_id=kiosk, outcome="matched", now=now, best_staff_id=staff.id, score=best_score)
    db.commit()
    return _resp({
        "status": "ok",
        "name": staff.name,
        "action": action,
        "time": _iso_utc(now),
    })
