"""SQLAlchemy models and staff data-deletion logic."""

from datetime import datetime, timezone

import numpy as np
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    delete,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EmbeddingVector(TypeDecorator[np.ndarray]):
    """Store a float32 embedding as bytes and read it back as a numpy array."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return np.asarray(value, dtype=np.float32).reshape(-1).tobytes()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return np.frombuffer(value, dtype=np.float32).copy()


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(100))
    shift: Mapped[str | None] = mapped_column(String(50))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    anonymised: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    consents: Mapped[list["Consent"]] = relationship(
        back_populates="staff", cascade="all, delete-orphan"
    )
    embeddings: Mapped[list["FaceEmbedding"]] = relationship(
        back_populates="staff", cascade="all, delete-orphan"
    )
    attendance: Mapped[list["Attendance"]] = relationship(back_populates="staff")


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    text_version: Mapped[str] = mapped_column(String(50))
    given_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    recorded_by_admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"), index=True)

    staff: Mapped[Staff] = relationship(back_populates="consents")
    recorded_by: Mapped[Admin] = relationship()


class FaceEmbedding(Base):
    __tablename__ = "face_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    vector: Mapped[np.ndarray] = mapped_column(EmbeddingVector)
    model_name: Mapped[str] = mapped_column(String(100))
    model_version: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    staff: Mapped[Staff] = relationship(back_populates="embeddings")


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (
        CheckConstraint("type IN ('in', 'out')", name="ck_attendance_type"),
        CheckConstraint(
            "method IN ('face', 'manual', 'review')", name="ck_attendance_method"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    type: Mapped[str] = mapped_column(String(10))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    method: Mapped[str] = mapped_column(String(10))
    distance: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str | None] = mapped_column(String(100))
    edited_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id"), index=True)
    edit_reason: Mapped[str | None] = mapped_column(Text)

    staff: Mapped[Staff] = relationship(back_populates="attendance")
    editor: Mapped[Admin | None] = relationship()


class RecognitionAttempt(Base):
    __tablename__ = "recognition_attempts"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('matched', 'rejected', 'ambiguous', 'no_face', 'spoof')",
            name="ck_attempt_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    kiosk_id: Mapped[str] = mapped_column(String(100), index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    best_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), index=True)
    score: Mapped[float | None] = mapped_column(Float)
    frame_path: Mapped[str | None] = mapped_column(String(500))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    best_staff: Mapped[Staff | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    admin: Mapped[Admin | None] = relationship()


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def delete_staff_data(session: Session, staff_id: int) -> bool:
    """Remove biometric data and personal fields while keeping attendance history."""

    staff = session.get(Staff, staff_id)
    if staff is None:
        return False

    session.execute(delete(FaceEmbedding).where(FaceEmbedding.staff_id == staff_id))
    session.execute(delete(Consent).where(Consent.staff_id == staff_id))

    staff.staff_code = f"deleted-{staff.id}"
    staff.name = ""
    staff.department = None
    staff.shift = None
    staff.active = False
    staff.anonymised = True
    session.commit()
    return True
