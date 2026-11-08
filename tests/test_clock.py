from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.clock_tokens import issue_clock_token
from app.main import app
from app.models import ApprovalRequest, Assignment, Attendance, Pattern, Staff
from app.routers import clock as clock_router
from app.routers.shifts import seed_default_patterns
from app.settings_store import set_values


@pytest.fixture()
def now() -> datetime:
    # Monday 2026-01-05 09:00 UTC (late for Office 08:00+10 grace)
    return datetime(2026, 1, 5, 9, 0, 0)


@pytest.fixture()
def clock_env(client: TestClient, now: datetime, session_factory: sessionmaker[Session]):
    app.dependency_overrides[clock_router.get_now] = lambda: now
    with session_factory() as s:
        seed_default_patterns(s)
        set_values(s, {"spoof_challenge": False})
        s.commit()
    return client


def add_staff(session_factory: sessionmaker[Session], *, name="Ada Lovelace", code="S001", active=True) -> int:
    with session_factory() as session:
        staff = Staff(staff_code=code, name=name, active=active)
        session.add(staff); session.commit()
        return staff.id


def assign_office(session_factory: sessionmaker[Session], staff_id: int):
    with session_factory() as s:
        office = s.scalar(select(Pattern).where(Pattern.name == "Office hours", Pattern.is_default.is_(True)))
        assert office is not None
        s.add(Assignment(staff_id=staff_id, pattern_id=office.id,
                         effective_from=datetime(2026, 1, 1).date(), effective_to=None))
        s.commit()


def make_token(session_factory: sessionmaker[Session], staff_id: int, kiosk="kiosk-1",
               issued_at=datetime(2026, 1, 5, 9, 0, 0), ttl=20) -> str:
    with session_factory() as s:
        token, _ = issue_clock_token(s, staff_id=staff_id, kiosk_id=kiosk, now=issued_at, ttl_seconds=ttl)
        s.commit()
        return token


def post_clock(client: TestClient, token: str, action="in", confirm=False, kiosk="kiosk-1"):
    return client.post("/api/clock", json={"token": token, "action": action, "confirm": confirm},
                       headers={"X-Kiosk-ID": kiosk})


def post_undo(client: TestClient, *, token=None, event_id=None, kiosk="kiosk-1"):
    body = {}
    if token is not None: body["token"] = token
    if event_id is not None: body["event_id"] = event_id
    return client.post("/api/clock/undo", json=body, headers={"X-Kiosk-ID": kiosk})


def post_request(client: TestClient, *, token=None, staff_id=None, action="in", reason_code="no_shift", kiosk="kiosk-1"):
    body = {"action": action, "reason_code": reason_code}
    if token is not None: body["token"] = token
    if staff_id is not None: body["staff_id"] = staff_id
    return client.post("/api/clock/request", json=body, headers={"X-Kiosk-ID": kiosk})


def att_count(session_factory, staff_id) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Attendance).where(Attendance.staff_id == staff_id)) or 0


# --- allowed cases ---
def test_clock_in_allowed_late(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    token = make_token(session_factory, sid)
    r = post_clock(client, token, "in")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok" and d["idempotent"] is False
    att = d["attendance"]
    assert att["action"] == "in" and att["staff_id"] == sid
    assert att["shift_date"] == "2026-01-05"
    assert att["flag"] == "late"
    assert att_count(session_factory, sid) == 1


def test_clock_out_allowed_after_end(clock_env, session_factory):
    # 17:30 after 17:00 end, margin 0 => overtime flag; clocked in
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    with session_factory() as s:
        s.add(Attendance(staff_id=sid, type="in", timestamp=datetime(2026, 1, 5, 8, 0, 0), method="manual"))
        s.commit()
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 17, 30, 0)
    token = make_token(session_factory, sid, issued_at=datetime(2026, 1, 5, 17, 30, 0))
    r = post_clock(client, token, "out")
    assert r.status_code == 200
    att = r.json()["attendance"]
    assert att["action"] == "out" and att["flag"] == "overtime"


# --- blocked cases ---
def test_blocked_action_not_in_allowed(clock_env, session_factory):
    # 09:00 late, not clocked in => allowed [in]; trying out is blocked
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    token = make_token(session_factory, sid)
    r = post_clock(client, token, "out")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["status"] == "blocked"
    assert "out" not in detail["allowed_actions"]
    assert att_count(session_factory, sid) == 0


def test_blocked_before_window_no_shift(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 7, 20, 0)
    token = make_token(session_factory, sid, issued_at=datetime(2026, 1, 5, 7, 20, 0))
    r = post_clock(client, token, "in")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "before_window"
    assert detail["reason_code"] == "out_of_window"


def test_blocked_no_shift(clock_env, session_factory):
    # Sunday, no assignment
    client = clock_env
    sid = add_staff(session_factory, name="Sun", code="S2")
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 4, 10, 0, 0)
    token = make_token(session_factory, sid, issued_at=datetime(2026, 1, 4, 10, 0, 0))
    r = post_clock(client, token, "in")
    assert r.status_code == 409
    assert r.json()["detail"]["reason_code"] == "no_shift"


# --- confirm needed for early out ---
def test_early_out_requires_confirm_then_succeeds(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    with session_factory() as s:
        s.add(Attendance(staff_id=sid, type="in", timestamp=datetime(2026, 1, 5, 8, 0, 0), method="manual"))
        s.commit()
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 12, 0, 0)
    token = make_token(session_factory, sid, issued_at=datetime(2026, 1, 5, 12, 0, 0))
    r = post_clock(client, token, "out", confirm=False)
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "confirm_required"
    assert att_count(session_factory, sid) == 1  # only the manual in
    r2 = post_clock(client, token, "out", confirm=True)
    assert r2.status_code == 200
    att = r2.json()["attendance"]
    assert att["action"] == "out" and att["flag"] == "early_departure"
    assert att_count(session_factory, sid) == 2


# --- double press / idempotent ---
def test_double_press_same_token_idempotent_no_second_row(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    token = make_token(session_factory, sid)
    r1 = post_clock(client, token, "in")
    assert r1.status_code == 200 and r1.json()["idempotent"] is False
    att_id = r1.json()["attendance"]["id"]
    r2 = post_clock(client, token, "in")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["idempotent"] is True
    assert d2["attendance"]["id"] == att_id
    assert att_count(session_factory, sid) == 1


# --- expired token ---
def test_expired_token_rejected(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    token = make_token(session_factory, sid, issued_at=datetime(2026, 1, 5, 8, 59, 0), ttl=20)
    # now is 09:00:00 in fixture but token issued 08:59:00 => expired (20s)
    r = post_clock(client, token, "in")
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "expired"
    assert att_count(session_factory, sid) == 0


# --- undo ---
def test_undo_within_10_seconds(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    token = make_token(session_factory, sid)
    r = post_clock(client, token, "in")
    att_id = r.json()["attendance"]["id"]
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 9, 0, 8)
    u = post_undo(client, token=token)
    assert u.status_code == 200
    d = u.json()
    assert d["status"] == "ok" and d["already_undone"] is False
    assert d["attendance"]["undone_at"] is not None
    assert d["attendance"]["id"] == att_id
    # row still exists (never deleted)
    assert att_count(session_factory, sid) == 1
    with session_factory() as s:
        row = s.get(Attendance, att_id)
        assert row is not None and row.undone_at is not None


def test_undo_after_10_seconds_rejected_and_not_deleted(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    token = make_token(session_factory, sid)
    r = post_clock(client, token, "in")
    att_id = r.json()["attendance"]["id"]
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 9, 0, 11)
    u = post_undo(client, token=token)
    assert u.status_code == 409
    assert u.json()["detail"]["status"] == "undo_window_expired"
    # never deleted
    assert att_count(session_factory, sid) == 1
    with session_factory() as s:
        row = s.get(Attendance, att_id)
        assert row is not None and row.undone_at is None


def test_undo_by_event_id_within_window(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    token = make_token(session_factory, sid)
    att_id = post_clock(client, token, "in").json()["attendance"]["id"]
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 9, 0, 5)
    u = post_undo(client, event_id=att_id)
    assert u.status_code == 200 and u.json()["attendance"]["undone_at"] is not None
    assert att_count(session_factory, sid) == 1


# --- request creation ---
def test_request_created_for_blocked_cases(clock_env, session_factory):
    client = clock_env
    sid = add_staff(session_factory); assign_office(session_factory, sid)
    # no_shift
    sid2 = add_staff(session_factory, name="NoShift", code="S9")
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 4, 10, 0, 0)
    t_ns = make_token(session_factory, sid2, issued_at=datetime(2026, 1, 4, 10, 0, 0))
    rr = post_request(client, token=t_ns, action="in", reason_code="no_shift")
    assert rr.status_code == 201
    assert rr.json()["reason_code"] == "no_shift" and rr.json()["status"] == "pending"

    # out_of_window
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 7, 20, 0)
    t_ow = make_token(session_factory, sid, issued_at=datetime(2026, 1, 5, 7, 20, 0))
    rr2 = post_request(client, token=t_ow, action="in", reason_code="out_of_window")
    assert rr2.status_code == 201 and rr2.json()["reason_code"] == "out_of_window"

    # early_out
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026, 1, 5, 12, 0, 0)
    t_eo = make_token(session_factory, sid, issued_at=datetime(2026, 1, 5, 12, 0, 0))
    rr3 = post_request(client, token=t_eo, action="out", reason_code="early_out")
    assert rr3.status_code == 201 and rr3.json()["reason_code"] == "early_out"

    with session_factory() as s:
        n = s.scalar(select(func.count()).select_from(ApprovalRequest)) or 0
        assert n >= 3
