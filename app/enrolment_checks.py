"""Enrolment safety checks: mixed-person batch and cross-staff duplicate face.

Pure/service helpers (no HTTP) so they can be unit-tested without a model.
Uses the same cosine-similarity convention as app.rules (normalised vectors;
higher score = more similar). Never logs or returns embedding vectors.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FaceEmbedding, Staff
from app.rules import _cosine_similarity, _normalised
from app.security import ROLE_ADMIN, ROLE_MANAGER

MIXED_PEOPLE_MESSAGE = (
    "The photos appear to show different people. Retake all photos of one person."
)


def check_same_person(embeddings: list[np.ndarray], threshold: float) -> tuple[bool, str | None]:
    if len(embeddings) < 2:
        return True, None
    first = _normalised(embeddings[0])
    for vec in embeddings[1:]:
        if _cosine_similarity(first, vec) < threshold:
            return False, MIXED_PEOPLE_MESSAGE
    return True, None


def _visible_name(meta: dict[str, Any], acting_admin: Any) -> str:
    code = meta.get("staff_code") or f"id:{meta.get('staff_id')}"
    if acting_admin is None:
        name = meta.get("name") or "another staff member"
        suffix = ", inactive" if not meta.get("active", True) else ""
        return f"{name} ({code}){suffix}"
    role = getattr(acting_admin, "role", None) or ROLE_ADMIN
    if role == ROLE_MANAGER:
        scope = getattr(acting_admin, "department_scope", None)
        if scope is not None and meta.get("department") != scope:
            return f"staff code {code}"
    name = meta.get("name") or "another staff member"
    suffix = ", inactive" if not meta.get("active", True) else ""
    return f"{name} ({code}){suffix}"


def find_duplicate_face(
    db: Session,
    embeddings: list[np.ndarray],
    *,
    exclude_staff_id: int,
    threshold: float,
    acting_admin: Any = None,
) -> tuple[int | None, str | None]:
    if not embeddings:
        return None, None
    rows = db.execute(
        select(
            FaceEmbedding.staff_id,
            FaceEmbedding.vector,
            Staff.name,
            Staff.staff_code,
            Staff.active,
            Staff.department,
        )
        .join(Staff, Staff.id == FaceEmbedding.staff_id)
        .where(
            FaceEmbedding.staff_id != exclude_staff_id,
            Staff.anonymised.is_(False),
        )
    ).all()
    if not rows:
        return None, None
    by_staff: dict[int, dict[str, Any]] = {}
    for staff_id, vector, name, staff_code, active, department in rows:
        entry = by_staff.setdefault(
            int(staff_id),
            {
                "staff_id": int(staff_id),
                "name": name,
                "staff_code": staff_code,
                "active": bool(active),
                "department": department,
                "vectors": [],
            },
        )
        entry["vectors"].append(np.asarray(vector, dtype=np.float32))
    normed = [_normalised(v) for v in embeddings]
    best_sid: int | None = None
    best_score = -1.0
    for sid, meta in by_staff.items():
        staff_best = -1.0
        for vec in meta["vectors"]:
            sv = _normalised(vec)
            for nv in normed:
                s = float(np.dot(nv, sv))
                if s > staff_best:
                    staff_best = s
        if staff_best > best_score:
            best_score = staff_best
            best_sid = sid
    if best_sid is not None and best_score >= threshold:
        msg = f"This face already appears to be enrolled for {_visible_name(by_staff[best_sid], acting_admin)}."
        return best_sid, msg
    return None, None
