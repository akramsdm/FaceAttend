from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import signin as signin_module
from app.main import app
from app.models import Attendance, RecognitionAttempt, Staff
from app.recognition import FrameAnalysis
from app.routers import signin as signin_router


class FakeRecognizer:
    def __init__(self) -> None:
        self.model_name = "SFace"
        self.model_version = "2021dec"
        self.analysis: FrameAnalysis | None = None
        self.embedding: np.ndarray | None = None
        self.embed_error = False

    def analyse_frame(self, image_bytes: bytes) -> FrameAnalysis:
        if self.analysis is not None:
            return self.analysis
        return FrameAnalysis(
            status="ok",
            frame=np.zeros((16, 16, 3), dtype=np.uint8),
            face_count=1,
            face=np.zeros(15, dtype=np.float32),
            box=(0, 0, 16, 16),
            landmarks=np.zeros((5, 2), dtype=np.float32),
        )

    def embed(self, frame: np.ndarray, face: np.ndarray) -> np.ndarray:
        if self.embed_error:
            raise ValueError("embedding failed")
        if self.embedding is not None:
            return self.embedding
        return np.array([1.0, 0.0], dtype=np.float32)


class FakeGallery:
    def __init__(self) -> None:
        self.best_id: int | None = None
        self.best_score: float | None = None
        self.second_score: float | None = None

    def match_embedding(self, embedding: np.ndarray):
        return self.best_id, self.best_score, self.second_score


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 1, 2, 9, 0, 0)


@pytest.fixture()
def signin_env(
    client: TestClient,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[TestClient, FakeRecognizer, FakeGallery]:
    recognizer = FakeRecognizer()
    gallery = FakeGallery()
    monkeypatch.setattr(signin_module, "REVIEW_DIR", tmp_path / "review")
    app.dependency_overrides[signin_router.get_recognizer] = lambda: recognizer
    app.dependency_overrides[signin_router.get_gallery] = lambda: gallery
    app.dependency_overrides[signin_router.get_now] = lambda: now
    return client, recognizer, gallery


def add_staff(
    session_factory: sessionmaker[Session],
    *,
    name: str = "Ada Lovelace",
    code: str = "S001",
    active: bool = True,
) -> int:
    with session_factory() as session:
        staff = Staff(staff_code=code, name=name, active=active)
        session.add(staff)
        session.commit()
        return staff.id


def post_signin(client: TestClient, kiosk_id: str = "kiosk-1"):
    return client.post(
        "/api/signin",
        headers={"X-Kiosk-ID": kiosk_id},
        files={"image": ("face.jpg", b"fake-image-bytes", "image/jpeg")},
    )


def count_rows(session_factory: sessionmaker[Session], model: type) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_in_then_out(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, _, gallery = signin_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20

    first = post_signin(client)
    assert first.status_code == 200
    assert first.json() == {
        "status": "ok",
        "name": "Ada Lovelace",
        "action": "in",
        "time": "2026-01-02T09:00:00Z",
    }

    app.dependency_overrides[signin_router.get_now] = (
        lambda: datetime(2026, 1, 2, 17, 0, 0)
    )
    second = post_signin(client)
    assert second.status_code == 200
    assert second.json()["status"] == "ok"
    assert second.json()["action"] == "out"

    assert count_rows(session_factory, Attendance) == 2
    with session_factory() as session:
        attempts = list(session.scalars(select(RecognitionAttempt)))
    assert [attempt.outcome for attempt in attempts] == ["matched", "matched"]
    assert all(attempt.score == pytest.approx(0.95) for attempt in attempts)
    assert all(attempt.frame_path is None for attempt in attempts)


def test_cooldown_returns_already_recorded_without_new_row(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, _, gallery = signin_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20

    assert post_signin(client).json()["action"] == "in"

    app.dependency_overrides[signin_router.get_now] = (
        lambda: datetime(2026, 1, 2, 9, 0, 30)
    )
    second = post_signin(client)
    assert second.status_code == 200
    assert second.json() == {
        "status": "already_recorded",
        "name": "Ada Lovelace",
        "action": "in",
        "time": "2026-01-02T09:00:00Z",
    }
    assert count_rows(session_factory, Attendance) == 1
    assert count_rows(session_factory, RecognitionAttempt) == 2


def test_ambiguous_attempt_is_logged(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, _, gallery = signin_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.90
    gallery.second_score = 0.88  # within 0.05 margin

    response = post_signin(client)
    assert response.status_code == 200
    assert response.json() == {"status": "ambiguous", "name": None, "action": None, "time": None}
    assert count_rows(session_factory, Attendance) == 0

    with session_factory() as session:
        attempt = session.scalar(select(RecognitionAttempt))
    assert attempt is not None
    assert attempt.outcome == "ambiguous"
    assert attempt.best_staff_id == staff_id
    assert attempt.score == pytest.approx(0.90)


def test_unknown_person_is_rejected_and_logged(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, _, gallery = signin_env
    gallery.best_id = None
    gallery.best_score = None
    gallery.second_score = None

    response = post_signin(client)
    assert response.status_code == 200
    assert response.json() == {
        "status": "not_recognised",
        "name": None,
        "action": None,
        "time": None,
    }
    assert count_rows(session_factory, Attendance) == 0

    with session_factory() as session:
        attempt = session.scalar(select(RecognitionAttempt))
    assert attempt is not None
    assert attempt.outcome == "rejected"
    assert attempt.best_staff_id is None
    assert attempt.score is None


def test_three_rejections_create_a_review_item(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, _, gallery = signin_env
    gallery.best_id = None
    gallery.best_score = None
    gallery.second_score = None

    for _ in range(3):
        response = post_signin(client)
        assert response.status_code == 200
        assert response.json()["status"] == "not_recognised"

    with session_factory() as session:
        attempts = list(
            session.scalars(select(RecognitionAttempt).order_by(RecognitionAttempt.id))
        )
    assert len(attempts) == 3
    assert [attempt.outcome for attempt in attempts] == ["rejected", "rejected", "rejected"]
    assert attempts[0].frame_path is None
    assert attempts[1].frame_path is None
    assert attempts[2].frame_path is not None
    assert attempts[2].resolved is False
    assert Path(attempts[2].frame_path).exists()


def test_inactive_staff_is_never_matched(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, _, gallery = signin_env
    staff_id = add_staff(session_factory, active=False)
    gallery.best_id = staff_id
    gallery.best_score = 0.99
    gallery.second_score = 0.10

    response = post_signin(client)
    assert response.status_code == 200
    assert response.json() == {
        "status": "not_recognised",
        "name": None,
        "action": None,
        "time": None,
    }
    assert count_rows(session_factory, Attendance) == 0

    with session_factory() as session:
        attempt = session.scalar(select(RecognitionAttempt))
    assert attempt is not None
    assert attempt.outcome == "rejected"
    assert attempt.best_staff_id is None
    assert attempt.score == pytest.approx(0.99)


def test_response_contains_no_score(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, _, gallery = signin_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20

    response = post_signin(client)
    payload = response.json()
    body = response.text.lower()

    assert set(payload) == {"status", "name", "action", "time"}
    assert "score" not in body
    assert "distance" not in body
    assert "0.95" not in body


def test_analyse_frame_problem_returns_short_message_code(
    signin_env: tuple[TestClient, FakeRecognizer, FakeGallery],
    session_factory: sessionmaker[Session],
) -> None:
    client, recognizer, _ = signin_env
    recognizer.analysis = FrameAnalysis(
        status="no_face",
        frame=np.zeros((16, 16, 3), dtype=np.uint8),
        face_count=0,
    )

    response = post_signin(client)
    assert response.status_code == 200
    assert response.json()["status"] == "no_face"
    assert count_rows(session_factory, Attendance) == 0

    with session_factory() as session:
        attempt = session.scalar(select(RecognitionAttempt))
    assert attempt is not None
    assert attempt.outcome == "no_face"


def test_recognizer_unavailable_returns_503(client: TestClient) -> None:
    app.dependency_overrides.pop(signin_router.get_recognizer, None)
    app.dependency_overrides.pop(signin_router.get_gallery, None)
    if hasattr(app.state, "recognizer"):
        delattr(app.state, "recognizer")
    if hasattr(app.state, "gallery"):
        delattr(app.state, "gallery")

    response = post_signin(client)
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()
