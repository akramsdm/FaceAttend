"""Admin staff CRUD and face enrolment endpoints."""

from datetime import datetime

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.db import get_session
from app.enrolment_checks import check_same_person, find_duplicate_face
from app.models import Admin, Consent, FaceEmbedding, Staff, delete_staff_data
from app.recognition import MODEL_NAME, MODEL_VERSION, GalleryCache, Recognizer
from app.security import require_admin, require_admin_csrf
from app.settings_store import get_value
from app.signin import iso_utc

router = APIRouter(prefix="/api/admin/staff", tags=["staff"])

CONSENT_TEXT_VERSION = "v1"
MIN_IMAGES = 3
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024

REJECTION_REASONS = {
    "no_face": "No face detected.",
    "multiple_faces": "More than one face detected.",
    "too_small": "Face is too small.",
    "blurry": "Face is blurry.",
}


class StaffCreate(BaseModel):
    staff_code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    department: str | None = Field(default=None, max_length=100)
    shift: str | None = Field(default=None, max_length=50)


class StaffUpdate(BaseModel):
    staff_code: str | None = Field(default=None, min_length=1, max_length=50)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    department: str | None = Field(default=None, max_length=100)
    shift: str | None = Field(default=None, max_length=50)
    active: bool | None = None


class StaffRead(BaseModel):
    id: int
    staff_code: str
    name: str
    department: str | None
    shift: str | None
    active: bool
    anonymised: bool
    created_at: datetime
    enrolled: bool
    embedding_count: int

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str | None:
        return iso_utc(value)


class ImageResult(BaseModel):
    filename: str
    accepted: bool
    status: str
    reason: str | None = None


class EnrolmentResponse(BaseModel):
    staff_id: int
    consent_text_version: str
    accepted: int
    stored_embeddings: int
    images: list[ImageResult]


def get_recognizer(request: Request) -> Recognizer:
    recognizer = getattr(request.app.state, "recognizer", None)
    if recognizer is None:
        raise HTTPException(status_code=503, detail="Recognizer is not available")
    return recognizer


def get_gallery(request: Request) -> GalleryCache:
    gallery = getattr(request.app.state, "gallery", None)
    if gallery is None:
        raise HTTPException(status_code=503, detail="Gallery is not available")
    return gallery


def _embedding_count(db: Session, staff_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(FaceEmbedding)
            .where(FaceEmbedding.staff_id == staff_id)
        )
        or 0
    )


def _staff_payload(staff: Staff, embedding_count: int) -> StaffRead:
    return StaffRead(
        id=staff.id,
        staff_code=staff.staff_code,
        name=staff.name,
        department=staff.department,
        shift=staff.shift,
        active=staff.active,
        anonymised=staff.anonymised,
        created_at=staff.created_at,
        enrolled=embedding_count > 0,
        embedding_count=embedding_count,
    )


@router.get("", response_model=list[StaffRead])
def list_staff(
    q: str | None = None,
    active: bool | None = None,
    include_deleted: bool = False,
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[StaffRead]:
    statement = (
        select(Staff, func.count(FaceEmbedding.id))
        .outerjoin(FaceEmbedding, FaceEmbedding.staff_id == Staff.id)
        .group_by(Staff.id)
        .order_by(Staff.staff_code)
    )
    if not include_deleted:
        statement = statement.where(Staff.anonymised.is_(False))
    if active is not None:
        statement = statement.where(Staff.active.is_(active))
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Staff.staff_code.ilike(pattern),
                Staff.name.ilike(pattern),
                Staff.department.ilike(pattern),
            )
        )

    rows = db.execute(statement).all()
    return [_staff_payload(staff, count) for staff, count in rows]


@router.post("", response_model=StaffRead, status_code=201)
def create_staff(
    payload: StaffCreate,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
) -> StaffRead:
    staff = Staff(**payload.model_dump())
    db.add(staff)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Staff code already exists")

    record_audit(
        db,
        admin.id,
        "create_staff",
        f"staff:{staff.id}",
        f"code {staff.staff_code}",
    )
    db.commit()
    return _staff_payload(staff, 0)


@router.get("/{staff_id}", response_model=StaffRead)
def get_staff(
    staff_id: int,
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
) -> StaffRead:
    staff = db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=404, detail="Staff not found")
    return _staff_payload(staff, _embedding_count(db, staff_id))


@router.patch("/{staff_id}", response_model=StaffRead)
def update_staff(
    staff_id: int,
    payload: StaffUpdate,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
    gallery: GalleryCache = Depends(get_gallery),
) -> StaffRead:
    staff = db.get(Staff, staff_id)
    if staff is None or staff.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")

    updates = payload.model_dump(exclude_unset=True)
    for required_field in ("staff_code", "name", "active"):
        if required_field in updates and updates[required_field] is None:
            raise HTTPException(
                status_code=422,
                detail=f"{required_field} cannot be null",
            )

    for field, value in updates.items():
        setattr(staff, field, value)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Staff code already exists")

    if updates:
        record_audit(
            db,
            admin.id,
            "update_staff",
            f"staff:{staff.id}",
            f"fields: {', '.join(sorted(updates))}",
        )
    db.commit()

    if "active" in updates:
        gallery.refresh()

    return _staff_payload(staff, _embedding_count(db, staff_id))


@router.post("/{staff_id}/deactivate", response_model=StaffRead)
def deactivate_staff(
    staff_id: int,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
    gallery: GalleryCache = Depends(get_gallery),
) -> StaffRead:
    staff = db.get(Staff, staff_id)
    if staff is None or staff.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")

    staff.active = False
    record_audit(
        db,
        admin.id,
        "deactivate_staff",
        f"staff:{staff.id}",
        f"code {staff.staff_code}",
    )
    db.commit()
    gallery.refresh()
    return _staff_payload(staff, _embedding_count(db, staff_id))


@router.delete("/{staff_id}")
def delete_staff(
    staff_id: int,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
    gallery: GalleryCache = Depends(get_gallery),
) -> dict[str, int | str]:
    staff = db.get(Staff, staff_id)
    if staff is None or staff.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")

    staff_code = staff.staff_code
    if not delete_staff_data(db, staff_id):
        raise HTTPException(status_code=404, detail="Staff not found")

    record_audit(
        db,
        admin.id,
        "delete_staff",
        f"staff:{staff_id}",
        f"code {staff_code}",
    )
    db.commit()
    gallery.refresh()
    return {"status": "deleted", "staff_id": staff_id}


@router.post("/{staff_id}/enrol", response_model=EnrolmentResponse)
def enrol_staff(
    staff_id: int,
    consent: bool = Form(...),
    images: list[UploadFile] = File(...),
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
    recognizer: Recognizer = Depends(get_recognizer),
    gallery: GalleryCache = Depends(get_gallery),
) -> EnrolmentResponse:
    if not consent:
        raise HTTPException(
            status_code=400,
            detail="Consent is required before enrolment",
        )
    if not MIN_IMAGES <= len(images) <= MAX_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Provide between {MIN_IMAGES} and {MAX_IMAGES} images",
        )

    staff = db.get(Staff, staff_id)
    if staff is None or staff.anonymised:
        raise HTTPException(status_code=404, detail="Staff not found")
    if not staff.active:
        raise HTTPException(
            status_code=409,
            detail="Staff is inactive; reactivate before enrolling",
        )

    results: list[ImageResult] = []
    embeddings: list[np.ndarray] = []
    max_bytes = getattr(get_settings(), "max_upload_bytes", MAX_IMAGE_BYTES)

    for image in images:
        filename = image.filename or "unnamed"
        image_bytes = image.file.read()
        if len(image_bytes) > max_bytes:
            results.append(
                ImageResult(
                    filename=filename,
                    accepted=False,
                    status="too_large",
                    reason="Image exceeds the upload size limit.",
                )
            )
            continue
        try:
            analysis = recognizer.analyse_frame(image_bytes)
        except ValueError:
            results.append(
                ImageResult(
                    filename=filename,
                    accepted=False,
                    status="invalid_image",
                    reason="Could not decode image.",
                )
            )
            continue

        if analysis.status != "ok" or analysis.face is None:
            results.append(
                ImageResult(
                    filename=filename,
                    accepted=False,
                    status=analysis.status,
                    reason=REJECTION_REASONS.get(analysis.status, analysis.status),
                )
            )
            continue

        try:
            vector = recognizer.embed(analysis.frame, analysis.face)
        except ValueError:
            results.append(
                ImageResult(
                    filename=filename,
                    accepted=False,
                    status="embedding_failed",
                    reason="Could not compute an embedding.",
                )
            )
            continue

        embeddings.append(vector)
        results.append(ImageResult(filename=filename, accepted=True, status="ok"))

    if len(embeddings) < MIN_IMAGES:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"At least {MIN_IMAGES} acceptable images are required; "
                    f"got {len(embeddings)}."
                ),
                "images": [result.model_dump() for result in results],
            },
        )

    # Safety checks BEFORE deleting the target's existing embeddings.
    # On any failure: no delete/store/consent/commit, gallery unchanged.
    match_threshold = float(get_value(db, "match_threshold"))
    ok_same, same_msg = check_same_person(embeddings, match_threshold)
    if not ok_same:
        record_audit(
            db,
            admin.id,
            "enrol_blocked_mixed_people",
            f"staff:{staff_id}",
            "rejected mixed-person batch; nothing stored",
        )
        db.commit()
        raise HTTPException(status_code=422, detail=same_msg)

    duplicate_threshold = float(get_value(db, "duplicate_threshold"))
    matched_id, dup_msg = find_duplicate_face(
        db,
        embeddings,
        exclude_staff_id=staff_id,
        threshold=duplicate_threshold,
        acting_admin=admin,
    )
    if matched_id is not None:
        record_audit(
            db,
            admin.id,
            "enrol_blocked_duplicate",
            f"staff:{staff_id}",
            f"matched staff:{matched_id}; nothing stored",
        )
        db.commit()
        raise HTTPException(status_code=409, detail=dup_msg)

    db.execute(delete(FaceEmbedding).where(FaceEmbedding.staff_id == staff_id))
    for vector in embeddings:
        db.add(
            FaceEmbedding(
                staff_id=staff_id,
                vector=vector,
                model_name=getattr(recognizer, "model_name", MODEL_NAME),
                model_version=getattr(recognizer, "model_version", MODEL_VERSION),
            )
        )
    db.add(
        Consent(
            staff_id=staff_id,
            text_version=CONSENT_TEXT_VERSION,
            recorded_by_admin_id=admin.id,
        )
    )
    record_audit(
        db,
        admin.id,
        "enrol_staff",
        f"staff:{staff_id}",
        f"accepted {len(embeddings)} of {len(images)} images",
    )
    db.commit()
    gallery.refresh()

    return EnrolmentResponse(
        staff_id=staff_id,
        consent_text_version=CONSENT_TEXT_VERSION,
        accepted=len(embeddings),
        stored_embeddings=len(embeddings),
        images=results,
    )
