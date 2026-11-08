"""SQLAlchemy models and staff data-deletion logic."""

from datetime import datetime, timezone

import numpy as np
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    UniqueConstraint,
    delete,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC, so SQLite reads and writes compare consistently."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    role: Mapped[str] = mapped_column(String(20), default="admin", server_default="admin")
    department_scope: Mapped[str | None] = mapped_column(String(100))
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
    # Phase 2 (nullable for backward compatibility with existing rows/flows)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("patterns.id"), index=True)
    shift_date = mapped_column(Date, nullable=True)
    flag: Mapped[str | None] = mapped_column(String(30))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id"), index=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    staff: Mapped[Staff] = relationship(back_populates="attendance")
    editor: Mapped[Admin | None] = relationship(foreign_keys=[edited_by])
    approver: Mapped[Admin | None] = relationship(foreign_keys=[approved_by])


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


# --- Phase 2: shifts / clock / approvals ---

class Pattern(Base):
    __tablename__ = "patterns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    owner_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), index=True)
    early_window_min: Mapped[int] = mapped_column(Integer, default=30)
    grace_min: Mapped[int] = mapped_column(Integer, default=10)
    break_min: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    days: Mapped[list["PatternDay"]] = relationship(
        back_populates="pattern", cascade="all, delete-orphan"
    )


class PatternDay(Base):
    __tablename__ = "pattern_days"
    __table_args__ = (UniqueConstraint("pattern_id", "weekday", name="uq_pattern_weekday"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern_id: Mapped[int] = mapped_column(ForeignKey("patterns.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Mon .. 6=Sun
    start_time = mapped_column(Time, nullable=False)
    end_time = mapped_column(Time, nullable=False)
    overnight: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    pattern: Mapped[Pattern] = relationship(back_populates="days")


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    pattern_id: Mapped[int] = mapped_column(ForeignKey("patterns.id"), index=True)
    effective_from = mapped_column(Date, nullable=False)
    effective_to = mapped_column(Date, nullable=True)  # null = current


class ShiftException(Base):
    __tablename__ = "shift_exceptions"
    __table_args__ = (UniqueConstraint("staff_id", "date", name="uq_exception_staff_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    date = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(20))  # day_off | custom_hours
    start_time = mapped_column(Time, nullable=True)
    end_time = mapped_column(Time, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClockToken(Base):
    __tablename__ = "clock_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    kiosk_id: Mapped[str] = mapped_column(String(100), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    action: Mapped[str] = mapped_column(String(10))  # in | out
    reason_code: Mapped[str] = mapped_column(String(30))  # no_shift|out_of_window|early_out
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id"), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
