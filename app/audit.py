"""Shared audit-log writing helper."""

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(
    db: Session,
    admin_id: int | None,
    action: str,
    target: str | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        AuditLog(
            admin_id=admin_id,
            action=action,
            target=target,
            detail=detail,
        )
    )
