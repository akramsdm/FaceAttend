import numpy as np
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

import app.db as db_module
from app.db import create_db_engine
from app.models import (
    Admin,
    Attendance,
    AuditLog,
    Consent,
    FaceEmbedding,
    RecognitionAttempt,
    Setting,
    Staff,
    delete_staff_data,
)


def add_admin_and_staff(session: Session) -> tuple[Admin, Staff]:
    admin = Admin(username="admin", password_hash="hashed")
    staff = Staff(
        staff_code="S001",
        name="Ada Lovelace",
        department="Engineering",
        shift="day",
    )
    session.add_all([admin, staff])
    session.flush()
    return admin, staff


def count_rows(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_create_and_read_each_model(session: Session) -> None:
    admin, staff = add_admin_and_staff(session)

    consent = Consent(
        staff_id=staff.id,
        text_version="v1",
        recorded_by_admin_id=admin.id,
    )
    embedding = FaceEmbedding(
        staff_id=staff.id,
        vector=np.array([0.1, 0.2], dtype=np.float32),
        model_name="SFace",
        model_version="2021dec",
    )
    attendance = Attendance(
        staff_id=staff.id,
        type="in",
        method="face",
        distance=0.12,
        model_version="2021dec",
    )
    attempt = RecognitionAttempt(
        kiosk_id="kiosk-1",
        outcome="matched",
        best_staff_id=staff.id,
        score=0.88,
    )
    audit = AuditLog(
        admin_id=admin.id,
        action="create_staff",
        target=f"staff:{staff.id}",
        detail="created during test",
    )
    setting = Setting(key="match_threshold", value="0.363")
    session.add_all([consent, embedding, attendance, attempt, audit, setting])
    session.commit()

    saved_staff = session.get(Staff, staff.id)
    assert saved_staff is not None
    assert saved_staff.staff_code == "S001"
    assert saved_staff.name == "Ada Lovelace"
    assert saved_staff.department == "Engineering"
    assert saved_staff.shift == "day"
    assert saved_staff.active is True
    assert saved_staff.anonymised is False
    assert saved_staff.created_at is not None

    saved_consent = session.get(Consent, consent.id)
    assert saved_consent is not None
    assert saved_consent.text_version == "v1"
    assert saved_consent.recorded_by_admin_id == admin.id
    assert saved_consent.given_at is not None

    saved_embedding = session.get(FaceEmbedding, embedding.id)
    assert saved_embedding is not None
    assert saved_embedding.model_name == "SFace"
    assert saved_embedding.model_version == "2021dec"
    assert saved_embedding.vector.size == 2

    saved_attendance = session.get(Attendance, attendance.id)
    assert saved_attendance is not None
    assert saved_attendance.type == "in"
    assert saved_attendance.method == "face"
    assert saved_attendance.distance == 0.12
    assert saved_attendance.timestamp is not None

    saved_attempt = session.get(RecognitionAttempt, attempt.id)
    assert saved_attempt is not None
    assert saved_attempt.kiosk_id == "kiosk-1"
    assert saved_attempt.outcome == "matched"
    assert saved_attempt.best_staff_id == staff.id
    assert saved_attempt.score == 0.88
    assert saved_attempt.resolved is False

    saved_audit = session.get(AuditLog, audit.id)
    assert saved_audit is not None
    assert saved_audit.action == "create_staff"
    assert saved_audit.admin_id == admin.id
    assert saved_audit.timestamp is not None

    saved_setting = session.get(Setting, "match_threshold")
    assert saved_setting is not None
    assert saved_setting.value == "0.363"

    assert session.get(Admin, admin.id).username == "admin"


def test_embedding_round_trip_to_numpy(session: Session) -> None:
    _, staff = add_admin_and_staff(session)
    vector = np.array([0.25, -0.5, 0.75, 0.0], dtype=np.float32)
    embedding = FaceEmbedding(
        staff_id=staff.id,
        vector=vector,
        model_name="SFace",
        model_version="2021dec",
    )
    session.add(embedding)
    session.commit()
    embedding_id = embedding.id
    session.expire_all()

    loaded = session.get(FaceEmbedding, embedding_id)
    assert loaded is not None
    assert isinstance(loaded.vector, np.ndarray)
    assert loaded.vector.dtype == np.float32
    np.testing.assert_array_equal(loaded.vector, vector)

    raw = session.execute(
        text("SELECT vector FROM face_embeddings WHERE id = :id"),
        {"id": embedding_id},
    ).scalar_one()
    assert isinstance(raw, bytes)
    assert len(raw) == vector.size * np.dtype(np.float32).itemsize


def test_delete_staff_data_removes_biometrics_and_keeps_history(
    session: Session,
) -> None:
    admin, staff = add_admin_and_staff(session)
    consent = Consent(
        staff_id=staff.id,
        text_version="v1",
        recorded_by_admin_id=admin.id,
    )
    embeddings = [
        FaceEmbedding(
            staff_id=staff.id,
            vector=np.array([float(index), 0.5], dtype=np.float32),
            model_name="SFace",
            model_version="2021dec",
        )
        for index in range(2)
    ]
    attendance = Attendance(staff_id=staff.id, type="in", method="face")
    attempt = RecognitionAttempt(
        kiosk_id="kiosk-1",
        outcome="rejected",
        best_staff_id=staff.id,
        score=0.2,
        resolved=True,
    )
    session.add_all([consent, *embeddings, attendance, attempt])
    session.commit()
    staff_id = staff.id

    assert delete_staff_data(session, staff_id) is True
    session.expire_all()

    anonymised = session.get(Staff, staff_id)
    assert anonymised is not None
    assert anonymised.staff_code == f"deleted-{staff_id}"
    assert anonymised.name == ""
    assert anonymised.department is None
    assert anonymised.shift is None
    assert anonymised.active is False
    assert anonymised.anonymised is True

    assert count_rows(session, Consent) == 0
    assert count_rows(session, FaceEmbedding) == 0
    assert count_rows(session, Attendance) == 1
    assert count_rows(session, RecognitionAttempt) == 1
    assert session.get(Attendance, attendance.id) is not None

    assert delete_staff_data(session, 999) is False


def test_init_db_creates_all_tables(tmp_path, monkeypatch) -> None:
    database_path = (tmp_path / "tables.db").as_posix()
    test_engine = create_db_engine(f"sqlite+pysqlite:///{database_path}")
    monkeypatch.setattr(db_module, "engine", test_engine)

    db_module.init_db()

    assert set(inspect(test_engine).get_table_names()) == {
        "admins",
        "staff",
        "consents",
        "face_embeddings",
        "attendance",
        "recognition_attempts",
        "audit_logs",
        "settings",
        # Phase 2
        "patterns",
        "pattern_days",
        "assignments",
        "shift_exceptions",
        "clock_tokens",
        "approval_requests",
    }
    test_engine.dispose()
