"""Admin settings (live, DB-backed) and audit log."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_session
from app.models import Admin, AuditLog
from app.security import require_admin, require_admin_csrf
from app.settings_store import DEFINITIONS, get_all, set_values

router = APIRouter(prefix="/api/admin", tags=["settings"])


class SettingsUpdate(BaseModel):
    match_threshold: float | None = None
    ambiguity_margin: float | None = None
    cooldown_seconds: int | None = None
    work_start: str | None = None
    grace_minutes: int | None = None
    review_retention_days: int | None = None
    spoof_challenge: bool | None = None


@router.get("/settings")
def read_settings(
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
):
    return get_all(db)


@router.put("/settings")
def update_settings(
    payload: SettingsUpdate,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
):
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return get_all(db)
    try:
        changes = set_values(db, updates)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    for key, old, new in changes:
        record_audit(db, admin.id, "update_setting", key, f"{old} -> {new}")
    db.commit()
    return get_all(db)


@router.get("/audit")
def read_audit_log(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: Admin = Depends(require_admin),
    db: Session = Depends(get_session),
):
    total = db.scalar(select(func.count()).select_from(AuditLog)) or 0
    rows = db.scalars(
        select(AuditLog).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "admin_id": r.admin_id,
                "action": r.action,
                "target": r.target,
                "detail": r.detail,
                "timestamp": r.timestamp,
            }
            for r in rows
        ],
    }
