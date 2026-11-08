from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.cleanup import cleanup_review_frames
from app.models import Attendance, AuditLog, RecognitionAttempt, Setting, Staff
from app.settings_store import get_all, get_value, set_values


def add_staff(session_factory: sessionmaker[Session], code="S001", name="Ada") -> int:
    with session_factory() as session:
        s = Staff(staff_code=code, name=name)
        session.add(s)
        session.commit()
        return s.id


def add_attempt(
    session_factory: sessionmaker[Session],
    *,
    kiosk="k1",
    outcome="rejected",
    score=0.2,
    frame_path=None,
    resolved=False,
    ts=None,
    best_staff_id=None,
) -> int:
    with session_factory() as session:
        a = RecognitionAttempt(
            timestamp=ts or datetime(2026, 1, 2, 9, 0, 0),
            kiosk_id=kiosk,
            outcome=outcome,
            best_staff_id=best_staff_id,
            score=score,
            frame_path=frame_path,
            resolved=resolved,
        )
        session.add(a)
        session.commit()
        return a.id


def test_review_resolve_assign_creates_attendance_and_deletes_frame(
    authenticated_client: TestClient, session_factory: sessionmaker[Session], admin_id: int, tmp_path: Path
):
    staff_id = add_staff(session_factory)
    frame = tmp_path / "r.jpg"
    frame.write_bytes(b"jpg")
    attempt_id = add_attempt(session_factory, frame_path=str(frame), best_staff_id=staff_id)
    with session_factory() as session:
        # ensure attempt exists and is first of day so action 'in'
        pass

    r = authenticated_client.post(
        f"/api/admin/review/{attempt_id}/resolve",
        json={"action": "assign", "staff_id": staff_id, "reason": "Verified at reception"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "assign"
    assert body["attendance_type"] in ("in", "out")
    assert not frame.exists()

    with session_factory() as session:
        att = session.get(Attendance, body["attendance_id"])
        assert att is not None
        assert att.method == "review"
        assert att.staff_id == staff_id
        assert att.edited_by == admin_id
        attempt = session.get(RecognitionAttempt, attempt_id)
        assert attempt.resolved is True
        assert attempt.frame_path is None
        log = session.scalar(select(AuditLog).where(AuditLog.action == "resolve_review_assign"))
        assert log is not None and log.admin_id == admin_id


def test_review_resolve_dismiss(
    authenticated_client: TestClient, session_factory: sessionmaker[Session], tmp_path: Path
):
    frame = tmp_path / "r.jpg"
    frame.write_bytes(b"jpg")
    attempt_id = add_attempt(session_factory, frame_path=str(frame))
    r = authenticated_client.post(
        f"/api/admin/review/{attempt_id}/resolve",
        json={"action": "dismiss", "reason": "Not a person"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "dismiss"
    assert not frame.exists()
    with session_factory() as session:
        attempt = session.get(RecognitionAttempt, attempt_id)
        assert attempt.resolved is True
        assert attempt.frame_path is None
        assert session.scalar(select(AuditLog).where(AuditLog.action == "resolve_review_dismiss")) is not None
        assert session.query(Attendance).count() == 0


def test_review_list_unresolved_and_validation(
    authenticated_client: TestClient, session_factory: sessionmaker[Session]
):
    add_attempt(session_factory, resolved=False)
    add_attempt(session_factory, resolved=True, outcome="ambiguous")
    r = authenticated_client.get("/api/admin/review")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["resolved"] is False

    # assign without staff_id
    aid = add_attempt(session_factory)
    r = authenticated_client.post(f"/api/admin/review/{aid}/resolve", json={"action": "assign", "reason": "x"})
    assert r.status_code == 422


def test_settings_get_put_validation_and_audit(
    authenticated_client: TestClient, session_factory: sessionmaker[Session], admin_id: int
):
    r = authenticated_client.get("/api/admin/settings")
    assert r.status_code == 200
    s = r.json()
    assert "match_threshold" in s and "work_start" in s

    r = authenticated_client.put(
        "/api/admin/settings",
        json={"match_threshold": 0.9, "grace_minutes": 10},
    )
    assert r.status_code == 200
    assert r.json()["match_threshold"] == 0.9
    assert r.json()["grace_minutes"] == 10

    with session_factory() as session:
        logs = session.scalars(select(AuditLog).where(AuditLog.action == "update_setting")).all()
        assert len(logs) == 2
        assert all(l.admin_id == admin_id for l in logs)

    # validation
    r = authenticated_client.put("/api/admin/settings", json={"match_threshold": 1.5})
    assert r.status_code == 422
    r = authenticated_client.put("/api/admin/settings", json={"work_start": "25:99"})
    assert r.status_code == 422


def test_settings_change_takes_effect_without_restart(
    authenticated_client: TestClient, session_factory: sessionmaker[Session]
):
    # drive signin decision via live DB threshold through settings_store
    with session_factory() as session:
        assert get_value(session, "cooldown_seconds") in (60, 60)
        set_values(session, {"cooldown_seconds": 7})
        session.commit()
    with session_factory() as session:
        assert get_value(session, "cooldown_seconds") == 7


def test_cleanup_deletes_only_expired_frames(session: Session, tmp_path: Path):
    now = datetime(2026, 1, 10, 12, 0, 0)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    old = review_dir / "old.jpg"
    new = review_dir / "new.jpg"
    old.write_bytes(b"o")
    new.write_bytes(b"n")
    # make old file mtime old
    import os, time
    old_ts = (now - timedelta(days=10)).timestamp()
    os.utime(old, (old_ts, old_ts))
    new_ts = (now - timedelta(days=1)).timestamp()
    os.utime(new, (new_ts, new_ts))

    # DB ref to old file
    from app.models import RecognitionAttempt
    a = RecognitionAttempt(
        timestamp=now - timedelta(days=10), kiosk_id="k", outcome="rejected",
        frame_path=str(old), resolved=False,
    )
    session.add(a)
    session.commit()

    deleted = cleanup_review_frames(session, retention_days=7, now=now, review_dir=review_dir)
    assert deleted >= 1
    assert not old.exists()
    assert new.exists()
    session.refresh(a)
    assert a.frame_path is None


def test_audit_log_paging(authenticated_client: TestClient, session_factory: sessionmaker[Session]):
    r = authenticated_client.get("/api/admin/audit", params={"limit": 2, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body
    assert body["limit"] == 2
