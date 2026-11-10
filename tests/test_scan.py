from datetime import datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock_tokens import consume_clock_token, verify_clock_token
from app.main import app
from app.models import Attendance, ClockToken, Staff
from app.recognition import FrameAnalysis
from app.routers import signin as signin_router
from app.routers.shifts import seed_default_patterns
from app.settings_store import set_values


class FakeRecognizer:
    def __init__(self) -> None:
        self.model_name = "SFace"
        self.model_version = "2021dec"
        self.analysis: FrameAnalysis | None = None
        self.embedding: np.ndarray | None = None

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
    # 2026-01-05 is a Monday 09:00 UTC (after Office 08:00 start+grace 10 => late)
    return datetime(2026, 1, 5, 9, 0, 0)


@pytest.fixture()
def scan_env(
    client: TestClient,
    now: datetime,
    session_factory: sessionmaker[Session],
) -> tuple[TestClient, FakeRecognizer, FakeGallery]:
    recognizer = FakeRecognizer()
    gallery = FakeGallery()
    app.dependency_overrides[signin_router.get_recognizer] = lambda: recognizer
    app.dependency_overrides[signin_router.get_gallery] = lambda: gallery
    app.dependency_overrides[signin_router.get_now] = lambda: now
    # ensure default patterns exist (init_db may have seeded on lifespan; be explicit)
    with session_factory() as s:
        seed_default_patterns(s)
        set_values(s, {"spoof_challenge": False})
        s.commit()
    return client, recognizer, gallery


def add_staff(session_factory: sessionmaker[Session], *, name="Ada Lovelace", code="S001",
              active=True, department=None) -> int:
    with session_factory() as session:
        staff = Staff(staff_code=code, name=name, active=active, department=department)
        session.add(staff)
        session.commit()
        return staff.id


def post_scan(client: TestClient, kiosk_id: str = "kiosk-1"):
    return client.post(
        "/api/scan",
        headers={"X-Kiosk-ID": kiosk_id},
        files={"image": ("face.jpg", b"fake-image-bytes", "image/jpeg")},
    )


def assert_no_score(body_text: str, payload: dict):
    b = body_text.lower()
    assert "score" not in b and "distance" not in b
    for k in payload:
        assert "score" not in k and "distance" not in k


def test_token_issued_only_on_match(scan_env, session_factory):
    client, _, gallery = scan_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20

    r = post_scan(client)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["name"] == "Ada Lovelace"
    assert data["token"]
    assert data["token_expires_in"] == 20
    assert isinstance(data["allowed_actions"], list)
    assert_no_score(r.text, data)
    with session_factory() as s:
        rows = list(s.scalars(select(ClockToken).where(ClockToken.staff_id == staff_id)))
        assert len(rows) == 1
        assert rows[0].kiosk_id == "kiosk-1"
        assert rows[0].used_at is None


def test_no_token_on_rejected_or_ambiguous(scan_env, session_factory):
    client, _, gallery = scan_env
    add_staff(session_factory)
    gallery.best_id = None
    gallery.best_score = None
    gallery.second_score = None
    r = post_scan(client)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "not_recognised"
    assert data["token"] is None
    assert data["name"] is None
    assert_no_score(r.text, data)

    staff_id = add_staff(session_factory, name="B", code="S002")
    gallery.best_id = staff_id
    gallery.best_score = 0.90
    gallery.second_score = 0.88  # ambiguous
    r2 = post_scan(client)
    d2 = r2.json()
    assert d2["status"] == "ambiguous"
    assert d2["token"] is None
    assert_no_score(r2.text, d2)
    with session_factory() as s:
        assert s.scalar(select(ClockToken).where(ClockToken.staff_id == staff_id)) is None


def test_token_bound_to_person_and_kiosk(scan_env, session_factory):
    client, _, gallery = scan_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20
    token = post_scan(client, kiosk_id="kiosk-A").json()["token"]
    with session_factory() as s:
        st = verify_clock_token(s, token, kiosk_id="kiosk-B", now=datetime(2026, 1, 5, 9, 0, 5))
        assert not st.ok and st.reason == "wrong_kiosk"
        st2 = verify_clock_token(s, token, kiosk_id="kiosk-A", now=datetime(2026, 1, 5, 9, 0, 5))
        assert st2.ok and st2.staff_id == staff_id and st2.kiosk_id == "kiosk-A"


def test_expired_token_rejected(scan_env, session_factory):
    client, _, gallery = scan_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20
    token = post_scan(client, kiosk_id="kiosk-1").json()["token"]
    with session_factory() as s:
        st = verify_clock_token(s, token, kiosk_id="kiosk-1", now=datetime(2026, 1, 5, 9, 0, 25))
        assert not st.ok and st.reason == "expired"


def test_used_token_rejected(scan_env, session_factory):
    client, _, gallery = scan_env
    staff_id = add_staff(session_factory)
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20
    token = post_scan(client, kiosk_id="kiosk-1").json()["token"]
    with session_factory() as s:
        st1 = consume_clock_token(s, token, kiosk_id="kiosk-1", now=datetime(2026, 1, 5, 9, 0, 5))
        assert st1.ok
        s.commit()
    with session_factory() as s:
        st2 = verify_clock_token(s, token, kiosk_id="kiosk-1", now=datetime(2026, 1, 5, 9, 0, 6))
        assert not st2.ok and st2.reason == "used"


def _scan_at(client, recognizer_dep_time_setter, when: datetime):
    app.dependency_overrides[signin_router.get_now] = lambda: when
    return post_scan(client)


def test_allowed_actions_per_situation(scan_env, session_factory):
    """Office Mon-Fri 08:00-17:00 (early 30, grace 10). Monday 2026-01-05."""
    from app.models import Assignment, Pattern
    client, _, gallery = scan_env
    staff_id = add_staff(session_factory)
    with session_factory() as s:
        office = s.scalar(select(Pattern).where(Pattern.name == "Office hours", Pattern.is_default.is_(True)))
        assert office is not None
        s.add(Assignment(staff_id=staff_id, pattern_id=office.id, effective_from=datetime(2026,1,1).date(), effective_to=None))
        s.commit()
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20

    # before window (07:20): no actions
    r = _scan_at(client, None, datetime(2026, 1, 5, 7, 20, 0))
    d = r.json(); assert d["status"] == "ok"
    assert d["allowed_actions"] == []
    assert d["message"] == "before_window"

    # window open, not clocked in (07:45): can clock in
    r = _scan_at(client, None, datetime(2026, 1, 5, 7, 45, 0))
    d = r.json(); assert "in" in d["allowed_actions"]; assert "out" not in d["allowed_actions"]

    # late arrival (09:00, after grace 08:10): can clock in, flagged late
    r = _scan_at(client, None, datetime(2026, 1, 5, 9, 0, 0))
    d = r.json()
    assert d["allowed_actions"] == ["in"]
    assert d["message"] == "late_arrival"

    # clocked in, before end (12:00): can clock out (early departure confirm)
    with session_factory() as s:
        s.add(Attendance(staff_id=staff_id, type="in", timestamp=datetime(2026, 1, 5, 8, 0, 0),
                         method="manual", shift_id=office.id, shift_date=datetime(2026,1,5).date()))
        s.commit()
    r = _scan_at(client, None, datetime(2026, 1, 5, 12, 0, 0))
    d = r.json()
    assert d["allowed_actions"] == ["out"]
    assert d["message"] == "clock_out_early_confirm"

    # after clock out (end session) - shift already completed => no actions (manager approval/reentry only)
    with session_factory() as s:
        s.add(Attendance(staff_id=staff_id, type="out", timestamp=datetime(2026, 1, 5, 17, 0, 0), method="manual",
                         shift_id=office.id, shift_date=datetime(2026,1,5).date()))
        s.commit()
    r = _scan_at(client, None, datetime(2026, 1, 5, 17, 30, 0))
    d = r.json()
    assert d["allowed_actions"] == []
    assert d["message"] == "shift_already_completed"


def test_no_shift_situation(scan_env, session_factory):
    # Sunday 2026-01-04: Office pattern (Mon-Fri default personal/shared? seeded defaults are shared;
    # staff without assignment gets? Office is Mon-Fri - resolve_shift finds no assignment match except
    # defaults are patterns but assignment required? check: resolve checks exception->personal->active assignment.
    # No assignment => None. So Sunday/no assignment => no shift.)
    client, _, gallery = scan_env
    staff_id = add_staff(session_factory, name="C", code="S003")
    gallery.best_id = staff_id
    gallery.best_score = 0.95
    gallery.second_score = 0.20
    r = _scan_at(client, None, datetime(2026, 1, 4, 10, 0, 0))  # Sunday
    d = r.json()
    assert d["status"] == "ok"
    assert d["allowed_actions"] == []
    assert d["message"] == "no_shift_request_approval"
    assert d["token"]  # still issued on match
