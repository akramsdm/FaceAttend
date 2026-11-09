from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.enrolment_checks import MIXED_PEOPLE_MESSAGE, check_same_person, find_duplicate_face
from app.main import app
from app.models import Admin, AuditLog, Consent, FaceEmbedding, Staff
from app.recognition import FrameAnalysis
from app.security import CSRF_HEADER_NAME, hash_password
from app.settings_store import set_values

ADMIN_PASSWORD = "correct horse battery staple"

V_A = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
V_A_NEAR = np.array([0.999, 0.0447, 0.0, 0.0], dtype=np.float32)
V_A_NEAR /= np.linalg.norm(V_A_NEAR)
V_B = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)


def _vec_at_cos(ref: np.ndarray, target_cos: float) -> np.ndarray:
    ref = ref / np.linalg.norm(ref)
    ortho = V_B if abs(float(np.dot(ref, V_B))) < 0.9 else np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    ortho = ortho - float(np.dot(ortho, ref)) * ref
    ortho /= np.linalg.norm(ortho)
    s = (1.0 - target_cos * target_cos) ** 0.5
    v = target_cos * ref + s * ortho
    return (v / np.linalg.norm(v)).astype(np.float32)


@dataclass
class _FakeFace:
    pass


class FakeRecognizer:
    def __init__(self, embeddings: list[np.ndarray]):
        self._emb = list(embeddings)
        self.model_name = "Fake"
        self.model_version = "test"

    def analyse_frame(self, image_bytes: bytes) -> FrameAnalysis:
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        return FrameAnalysis(status="ok", frame=frame, face_count=1, face=_FakeFace())

    def embed(self, frame, face) -> np.ndarray:
        if not self._emb:
            raise ValueError("no embedding")
        v = self._emb.pop(0)
        v = np.asarray(v, dtype=np.float32).reshape(-1)
        return v / np.linalg.norm(v)


class FakeGallery:
    def refresh(self) -> None:
        return None


def _add_staff(session: Session, code: str, name: str, active: bool = True, anonymised: bool = False, department: str | None = None) -> int:
    s = Staff(staff_code=code, name=name, active=active, anonymised=anonymised, department=department)
    session.add(s)
    session.commit()
    session.refresh(s)
    return int(s.id)


def _add_embedding(session: Session, staff_id: int, vec: np.ndarray) -> None:
    session.add(FaceEmbedding(staff_id=staff_id, vector=vec.astype(np.float32), model_name="Fake", model_version="test"))
    session.commit()


def _counts(session: Session, staff_id: int) -> tuple[int, int]:
    e = session.scalar(select(func.count()).select_from(FaceEmbedding).where(FaceEmbedding.staff_id == staff_id)) or 0
    c = session.scalar(select(func.count()).select_from(Consent).where(Consent.staff_id == staff_id)) or 0
    return int(e), int(c)


def _login_as(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


def _post_enrol(client: TestClient, headers: dict[str, str], staff_id: int, embeddings: list[np.ndarray], n_images: int = 3):
    app.state.recognizer = FakeRecognizer(list(embeddings)[:n_images])
    app.state.gallery = FakeGallery()
    files = [("images", (f"f{i}.jpg", b"\xff\xd8fake", "image/jpeg")) for i in range(n_images)]
    return client.post(
        f"/api/admin/staff/{staff_id}/enrol",
        data={"consent": "true"},
        files=files,
        headers=headers,
    )


def test_same_person_check_rejects_mixed() -> None:
    ok, msg = check_same_person([V_A, V_A_NEAR, V_B], threshold=0.8)
    assert ok is False
    assert msg == MIXED_PEOPLE_MESSAGE


def test_same_person_check_accepts_consistent() -> None:
    ok, msg = check_same_person([V_A, V_A_NEAR, V_A], threshold=0.8)
    assert ok is True and msg is None


def test_duplicate_threshold_boundary(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        other = _add_staff(session, "O1", "Other")
        target = _add_staff(session, "T1", "Target")
        _add_embedding(session, other, V_A)
        thr = 0.8
        at = _vec_at_cos(V_A, thr)
        sid, _ = find_duplicate_face(session, [at], exclude_staff_id=target, threshold=thr)
        assert sid == other
        below = _vec_at_cos(V_A, thr - 0.0005)
        sid2, msg2 = find_duplicate_face(session, [below], exclude_staff_id=target, threshold=thr)
        assert sid2 is None and msg2 is None


def test_duplicate_inactive_blocks_anonymised_does_not(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        target = _add_staff(session, "T2", "Target")
        inactive = _add_staff(session, "I1", "Inactive One", active=False)
        _add_embedding(session, inactive, V_A)
        sid, msg = find_duplicate_face(session, [V_A], exclude_staff_id=target, threshold=0.8)
        assert sid == inactive
        assert "inactive" in (msg or "").lower()
        anon = _add_staff(session, "A1", "Anon", active=False, anonymised=True)
        _add_embedding(session, anon, V_A_NEAR)
        session.query(FaceEmbedding).filter(FaceEmbedding.staff_id == inactive).delete()
        session.commit()
        sid2, msg2 = find_duplicate_face(session, [V_A], exclude_staff_id=target, threshold=0.8)
        assert sid2 is None and msg2 is None


def test_unique_face_passes(client: TestClient, session_factory: sessionmaker[Session], admin_id: int, auth_headers: dict[str, str]) -> None:
    with session_factory() as session:
        sid = _add_staff(session, "U1", "Unique")
    r = _post_enrol(client, auth_headers, sid, [V_A, V_A_NEAR, V_A])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stored_embeddings"] == 3
    with session_factory() as session:
        e, c = _counts(session, sid)
        assert e == 3 and c == 1
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "enrol_staff")) >= 1


def test_same_face_under_second_staff_returns_409_naming_first(
    client: TestClient, session_factory: sessionmaker[Session], admin_id: int, auth_headers: dict[str, str]
) -> None:
    with session_factory() as session:
        first = _add_staff(session, "F1", "First Person")
        second = _add_staff(session, "S1", "Second Person")
        _add_embedding(session, first, V_A)
        before_e, before_c = _counts(session, second)
    r = _post_enrol(client, auth_headers, second, [V_A, V_A_NEAR, V_A])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "First Person" in detail
    assert "F1" in detail
    assert "0." not in detail and "score" not in detail.lower()
    with session_factory() as session:
        e, c = _counts(session, second)
        assert e == before_e and c == before_c
        fe = session.scalar(select(func.count()).select_from(FaceEmbedding).where(FaceEmbedding.staff_id == first))
        assert fe == 1
        log = session.scalar(select(AuditLog).where(AuditLog.action == "enrol_blocked_duplicate"))
        assert log is not None
        assert f"staff:{second}" == log.target
        assert "matched staff:" in (log.detail or "") and "vector" not in (log.detail or "").lower()


def test_inactive_staff_blocks(client: TestClient, session_factory: sessionmaker[Session], auth_headers: dict[str, str]) -> None:
    with session_factory() as session:
        inactive = _add_staff(session, "I9", "Dormant User", active=False)
        _add_embedding(session, inactive, V_A)
        target = _add_staff(session, "T9", "Target")
    r = _post_enrol(client, auth_headers, target, [V_A, V_A_NEAR, V_A])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Dormant User" in detail and "inactive" in detail.lower()
    with session_factory() as session:
        e, c = _counts(session, target)
        assert e == 0 and c == 0


def test_anonymised_staff_does_not_block(client: TestClient, session_factory: sessionmaker[Session], auth_headers: dict[str, str]) -> None:
    with session_factory() as session:
        anon = _add_staff(session, "X1", "Gone", active=False, anonymised=True)
        _add_embedding(session, anon, V_A)
        target = _add_staff(session, "T8", "Target")
    r = _post_enrol(client, auth_headers, target, [V_A, V_A_NEAR, V_A])
    assert r.status_code == 200, r.text
    with session_factory() as session:
        e, _ = _counts(session, target)
        assert e == 3


def test_mixed_people_batch_returns_422_and_stores_nothing(
    client: TestClient, session_factory: sessionmaker[Session], auth_headers: dict[str, str]
) -> None:
    with session_factory() as session:
        target = _add_staff(session, "M1", "Target")
        _add_embedding(session, target, V_B)
        before_e, before_c = _counts(session, target)
    r = _post_enrol(client, auth_headers, target, [V_A, V_A_NEAR, V_B])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail == MIXED_PEOPLE_MESSAGE
    with session_factory() as session:
        e, c = _counts(session, target)
        assert e == before_e and c == before_c
        log = session.scalar(select(AuditLog).where(AuditLog.action == "enrol_blocked_mixed_people"))
        assert log is not None and f"staff:{target}" == log.target


def test_reenrol_same_staff_replaces_embeddings(
    client: TestClient, session_factory: sessionmaker[Session], auth_headers: dict[str, str]
) -> None:
    with session_factory() as session:
        sid = _add_staff(session, "R1", "Re Enrol")
    r1 = _post_enrol(client, auth_headers, sid, [V_A, V_A_NEAR, V_A])
    assert r1.status_code == 200, r1.text
    r2 = _post_enrol(client, auth_headers, sid, [V_B, V_B, V_B])
    assert r2.status_code == 200, r2.text
    with session_factory() as session:
        e, c = _counts(session, sid)
        assert e == 3
        assert c == 2


def test_threshold_boundary_http_blocks_equal_allows_below(
    client: TestClient, session_factory: sessionmaker[Session], auth_headers: dict[str, str]
) -> None:
    with session_factory() as session:
        other = _add_staff(session, "B1", "Boundary")
        _add_embedding(session, other, V_A)
        t_eq = _add_staff(session, "TE", "TargetEq")
        t_below = _add_staff(session, "TB", "TargetBelow")
        thr = 0.75
        set_values(session, {"duplicate_threshold": thr})
        session.commit()
    at = _vec_at_cos(V_A, thr)
    below = _vec_at_cos(V_A, thr - 0.0005)
    r_eq = _post_enrol(client, auth_headers, t_eq, [at, at, at])
    assert r_eq.status_code == 409, r_eq.text
    r_below = _post_enrol(client, auth_headers, t_below, [below, below, below])
    assert r_below.status_code == 200, r_below.text
    with session_factory() as session:
        assert _counts(session, t_eq)[0] == 0
        assert _counts(session, t_below)[0] == 3


def test_manager_scope_shows_code_only(
    client: TestClient, session_factory: sessionmaker[Session], admin_password: str
) -> None:
    with session_factory() as session:
        mgr = Admin(username="mgr", password_hash=hash_password(admin_password), role="manager", department_scope="Sales")
        session.add(mgr)
        session.commit()
        other = _add_staff(session, "ENG1", "Eng Person", department="Engineering")
        _add_embedding(session, other, V_A)
        target = _add_staff(session, "SAL1", "Sales Target", department="Sales")
    headers = _login_as(client, "mgr", admin_password)
    r = _post_enrol(client, headers, target, [V_A, V_A_NEAR, V_A])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "ENG1" in detail
    assert "Eng Person" not in detail
    with session_factory() as session:
        log = session.scalar(select(AuditLog).where(AuditLog.action == "enrol_blocked_duplicate").order_by(AuditLog.id.desc()))
        assert log is not None and f"matched staff:{other}" in (log.detail or "")


def test_failure_leaves_no_embeddings_or_consent(
    client: TestClient, session_factory: sessionmaker[Session], auth_headers: dict[str, str]
) -> None:
    with session_factory() as session:
        other = _add_staff(session, "OF", "Other")
        _add_embedding(session, other, V_A)
        target = _add_staff(session, "TF", "Target")
    r = _post_enrol(client, auth_headers, target, [V_A, V_A, V_A])
    assert r.status_code == 409
    with session_factory() as session:
        e, c = _counts(session, target)
        assert e == 0 and c == 0
