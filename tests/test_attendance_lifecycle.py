"""Lifecycle integrity tests for clock-in / clock-out (Phase 4).

These tests exercise the public kiosk API and the shared attendance service so
that every path (scan, clock, legacy signin, approvals, review) obeys the same
PENDING -> CLOCKED_IN -> COMPLETED rules.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.attendance_service import state_for_staff
from app.approvals import close_missing_sessions
from app.clock_tokens import issue_clock_token
from app.main import app
from app.models import (
    Admin,
    ApprovalRequest,
    Assignment,
    Attendance,
    Pattern,
    PatternDay,
    Staff,
)
from app.routers import clock as clock_router
from app.routers.attendance import _pair_sessions
from app.routers.shifts import seed_default_patterns
from app.security import CSRF_HEADER_NAME, hash_password
from app.settings_store import set_values

ADMIN_PW = "correct horse battery staple"
KIOSK = "kiosk-1"


def _set_now(when: datetime) -> None:
    app.dependency_overrides[clock_router.get_now] = lambda: when


@pytest.fixture()
def seeded(client: TestClient, session_factory: sessionmaker[Session]):
    with session_factory() as s:
        seed_default_patterns(s)
        set_values(s, {"spoof_challenge": False, "allow_reentry_same_shift": False})
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.commit()
    _set_now(datetime(2026, 1, 5, 9, 0, 0))
    return client


def add_staff(session_factory, *, code="S1", name="Ada") -> int:
    with session_factory() as s:
        staff = Staff(staff_code=code, name=name, department="Eng", active=True)
        s.add(staff)
        s.commit()
        return staff.id


def office_pattern_id(session_factory) -> int:
    with session_factory() as s:
        office = s.scalar(
            select(Pattern).where(Pattern.name == "Office hours", Pattern.is_default.is_(True))
        )
        assert office is not None
        return office.id


def assign_office(session_factory, staff_id: int) -> int:
    pid = office_pattern_id(session_factory)
    with session_factory() as s:
        s.add(
            Assignment(
                staff_id=staff_id,
                pattern_id=pid,
                effective_from=date(2026, 1, 1),
                effective_to=None,
            )
        )
        s.commit()
    return pid


def make_token(session_factory, staff_id: int, *, when: datetime, kiosk=KIOSK) -> str:
    with session_factory() as s:
        token, _ = issue_clock_token(s, staff_id=staff_id, kiosk_id=kiosk, now=when, ttl_seconds=20)
        s.commit()
        return token


def clock(client, token, action, *, confirm=False, kiosk=KIOSK):
    return client.post(
        "/api/clock",
        json={"token": token, "action": action, "confirm": confirm},
        headers={"X-Kiosk-ID": kiosk},
    )


def rows_for(session_factory, staff_id) -> list[Attendance]:
    with session_factory() as s:
        return list(
            s.scalars(
                select(Attendance)
                .where(Attendance.staff_id == staff_id)
                .order_by(Attendance.timestamp, Attendance.id)
            ).all()
        )


def admin_headers(client) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": "alice", "password": ADMIN_PW})
    assert r.status_code == 200
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


# ---------------------------------------------------------------------------
# Happy path: PENDING -> CLOCKED_IN -> COMPLETED
# ---------------------------------------------------------------------------


def test_full_lifecycle_in_then_out_completes(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)

    # Monday 09:00 is late (Office 08:00 + 10 grace) but still inside the shift.
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 9, 0, 0))
    r = clock(client, token, "in")
    assert r.status_code == 200, r.text
    att_in = r.json()["attendance"]
    assert att_in["action"] == "in"
    assert att_in["flag"] == "late"
    assert att_in["shift_id"] == pid
    assert att_in["shift_date"] == "2026-01-05"
    assert att_in["timestamp"].endswith("Z")
    assert att_in["local_time"]

    _set_now(datetime(2026, 1, 5, 17, 30, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 17, 30, 0))
    r = clock(client, token, "out")
    assert r.status_code == 200, r.text
    att_out = r.json()["attendance"]
    assert att_out["action"] == "out"
    assert att_out["flag"] == "overtime"
    assert att_out["shift_id"] == pid
    assert att_out["shift_date"] == "2026-01-05"
    assert att_out["paired_in_id"] == att_in["id"]

    rows = rows_for(session_factory, sid)
    assert len(rows) == 2
    assert rows[0].open_key is None  # session closed
    assert rows[1].paired_in_id == rows[0].id

    # Completed shift: no further events without approval.
    _set_now(datetime(2026, 1, 5, 18, 0, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 18, 0, 0))
    r = clock(client, token, "in")
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "shift_already_completed"
    assert len(rows_for(session_factory, sid)) == 2

    r = clock(client, token, "out")
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "shift_already_completed"
    assert len(rows_for(session_factory, sid)) == 2


# ---------------------------------------------------------------------------
# One open session per employee
# ---------------------------------------------------------------------------


def test_second_clockin_blocked_while_session_open(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)

    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 9, 0, 0))
    assert clock(client, token, "in").status_code == 200

    token2 = make_token(session_factory, sid, when=datetime(2026, 1, 5, 9, 0, 1))
    r = clock(client, token2, "in")
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "session_already_open"
    assert len(rows_for(session_factory, sid)) == 1


def test_open_key_unique_index_rejects_duplicate(seeded, session_factory):
    sid = add_staff(session_factory)
    with session_factory() as s:
        s.add(Attendance(staff_id=sid, type="in", timestamp=datetime(2026, 1, 5, 8, 0, 0), method="manual", open_key=str(sid)))
        s.commit()
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with session_factory() as s:
            s.add(Attendance(staff_id=sid, type="in", timestamp=datetime(2026, 1, 5, 9, 0, 0), method="manual", open_key=str(sid)))
            s.commit()


# ---------------------------------------------------------------------------
# Orphan clock-out
# ---------------------------------------------------------------------------


def test_orphan_clockout_rejected_without_writing_a_row(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)

    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 12, 0, 0))
    r = clock(client, token, "out")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "no_open_session"
    assert detail["reason_code"] == "out_of_window"
    assert rows_for(session_factory, sid) == []


# ---------------------------------------------------------------------------
# Clock-out inherits the open session's shift (never today's)
# ---------------------------------------------------------------------------


def test_clockout_inherits_open_session_shift_not_todays(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)

    # Simulate an open session anchored to Friday 2026-01-02, then clock out on
    # Monday 2026-01-05. The out must inherit Friday's shift identity.
    with session_factory() as s:
        s.add(
            Attendance(
                staff_id=sid,
                type="in",
                timestamp=datetime(2026, 1, 2, 9, 0, 0),
                method="manual",
                shift_id=pid,
                shift_date=date(2026, 1, 2),
                open_key=str(sid),
            )
        )
        s.commit()

    _set_now(datetime(2026, 1, 5, 10, 0, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 10, 0, 0))
    r = clock(client, token, "out")
    assert r.status_code == 200, r.text
    att = r.json()["attendance"]
    assert att["shift_id"] == pid
    assert att["shift_date"] == "2026-01-02"


def test_night_shift_clockout_keeps_start_date(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    with session_factory() as s:
        p = Pattern(
            name="NightT",
            kind="shared",
            early_window_min=30,
            grace_min=10,
            break_min=30,
            is_default=False,
        )
        s.add(p)
        s.flush()
        for wd in range(7):
            s.add(
                PatternDay(
                    pattern_id=p.id,
                    weekday=wd,
                    start_time=time(22, 0),
                    end_time=time(6, 0),
                    overnight=True,
                    enabled=True,
                )
            )
        s.add(
            Assignment(
                staff_id=sid,
                pattern_id=p.id,
                effective_from=date(2026, 1, 1),
                effective_to=None,
            )
        )
        s.commit()
        pid = p.id
        s.add(
            Attendance(
                staff_id=sid,
                type="in",
                timestamp=datetime(2026, 1, 5, 22, 0, 0),
                method="face",
                shift_id=pid,
                shift_date=date(2026, 1, 5),
                open_key=str(sid),
            )
        )
        s.commit()

    _set_now(datetime(2026, 1, 6, 6, 10, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 6, 6, 10, 0))
    r = clock(client, token, "out")
    assert r.status_code == 200, r.text
    att = r.json()["attendance"]
    assert att["shift_date"] == "2026-01-05"
    assert att["shift_id"] == pid


def test_out_of_window_null_shift_session_stays_null(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)
    with session_factory() as s:
        s.add(
            Attendance(
                staff_id=sid,
                type="in",
                timestamp=datetime(2026, 1, 4, 22, 0, 0),
                method="manual",
                shift_id=None,
                shift_date=date(2026, 1, 4),
                flag="out_of_window",
                open_key=str(sid),
            )
        )
        s.commit()

    _set_now(datetime(2026, 1, 5, 6, 0, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 6, 0, 0))
    r = clock(client, token, "out", confirm=True)
    assert r.status_code == 200, r.text
    att = r.json()["attendance"]
    assert att["shift_id"] is None
    assert att["shift_date"] == "2026-01-04"
    assert att["flag"] is None


# ---------------------------------------------------------------------------
# Stale token revalidation
# ---------------------------------------------------------------------------


def test_stale_token_after_completion_revalidates_and_409(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)

    # Token minted while no session existed.
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 9, 0, 0))

    # Meanwhile the shift is completed.
    with session_factory() as s:
        s.add(
            Attendance(
                staff_id=sid,
                type="in",
                timestamp=datetime(2026, 1, 5, 8, 0, 0),
                method="face",
                shift_id=pid,
                shift_date=date(2026, 1, 5),
            )
        )
        s.add(
            Attendance(
                staff_id=sid,
                type="out",
                timestamp=datetime(2026, 1, 5, 17, 0, 0),
                method="face",
                shift_id=pid,
                shift_date=date(2026, 1, 5),
            )
        )
        s.commit()

    _set_now(datetime(2026, 1, 5, 18, 0, 0))
    r = clock(client, token, "in")
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "shift_already_completed"
    # Nothing written for the stale token.
    assert len(rows_for(session_factory, sid)) == 2


def test_expired_token_without_open_session_returns_400(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 8, 59, 0))
    _set_now(datetime(2026, 1, 5, 9, 0, 0))
    r = clock(client, token, "in")
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "expired"
    assert rows_for(session_factory, sid) == []


# ---------------------------------------------------------------------------
# No minimum-duration restriction
# ---------------------------------------------------------------------------


def test_short_session_can_clock_out_immediately(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)

    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 9, 0, 0))
    assert clock(client, token, "in").status_code == 200

    # Five seconds later - the old min-duration rule would have blocked this.
    _set_now(datetime(2026, 1, 5, 9, 0, 5))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 9, 0, 5))
    r = clock(client, token, "out", confirm=True)
    assert r.status_code == 200, r.text
    assert r.json()["attendance"]["action"] == "out"


# ---------------------------------------------------------------------------
# Late / after-shift-end policy
# ---------------------------------------------------------------------------


def test_late_clockin_inside_shift_allowed_and_flagged(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 9, 0, 0))
    r = clock(client, token, "in")
    assert r.status_code == 200
    att = r.json()["attendance"]
    assert att["flag"] == "late"
    # Actual timestamp preserved (09:00 UTC), not rewritten to shift start.
    assert att["timestamp"] == "2026-01-05T09:00:00Z"


def test_clockin_after_shift_end_blocked_with_policy(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)
    _set_now(datetime(2026, 1, 5, 18, 0, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 18, 0, 0))
    r = clock(client, token, "in")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "after_shift_end"
    assert detail["reason_code"] == "out_of_window"
    assert rows_for(session_factory, sid) == []


def test_after_shift_end_reports_next_shift(seeded, session_factory):
    """The kiosk needs the next shift start to tell people when to return."""
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)
    with session_factory() as s:
        staff = s.get(Staff, sid)
        state = state_for_staff(s, staff, datetime(2026, 1, 5, 18, 0, 0))
    assert state["state"] == "after_shift_end"
    nxt = state["next_shift"]
    assert nxt is not None
    assert nxt.start == datetime(2026, 1, 6, 8, 0, 0)
    assert nxt.end == datetime(2026, 1, 6, 17, 0, 0)


# ---------------------------------------------------------------------------
# Re-entry requires setting + manager approval
# ---------------------------------------------------------------------------


def _complete_shift(session_factory, sid, pid):
    with session_factory() as s:
        s.add(
            Attendance(
                staff_id=sid,
                type="in",
                timestamp=datetime(2026, 1, 5, 8, 0, 0),
                method="face",
                shift_id=pid,
                shift_date=date(2026, 1, 5),
            )
        )
        s.add(
            Attendance(
                staff_id=sid,
                type="out",
                timestamp=datetime(2026, 1, 5, 17, 0, 0),
                method="face",
                shift_id=pid,
                shift_date=date(2026, 1, 5),
            )
        )
        s.commit()


def test_reentry_requires_setting_then_approval(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)
    _complete_shift(session_factory, sid, pid)

    # Default OFF: scan advertises no action and clock-in is rejected.
    with session_factory() as s:
        staff = s.get(Staff, sid)
        state = state_for_staff(s, staff, datetime(2026, 1, 5, 18, 0, 0))
        assert state["state"] == "completed"
        assert state["evaluation"]["message"] == "shift_already_completed"

    # Setting ON: scan advertises re-entry approval required.
    with session_factory() as s:
        set_values(s, {"allow_reentry_same_shift": True})
        s.commit()
        staff = s.get(Staff, sid)
        state = state_for_staff(s, staff, datetime(2026, 1, 5, 18, 0, 0))
        assert state["state"] == "reentry_required"
        assert state["evaluation"]["message"] == "reentry_approval_required"

    _set_now(datetime(2026, 1, 5, 18, 0, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 18, 0, 0))
    r = clock(client, token, "in")
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "reentry_approval_required"
    assert len(rows_for(session_factory, sid)) == 2

    # Manager approval creates exactly one new open session.
    with session_factory() as s:
        req = ApprovalRequest(
            staff_id=sid,
            action="in",
            reason_code="reentry",
            requested_at=datetime(2026, 1, 5, 18, 0, 0),
            status="pending",
        )
        s.add(req)
        s.commit()
        rid = req.id

    h = admin_headers(client)
    r = client.post(f"/api/admin/approvals/{rid}/approve", json={"note": "ok"}, headers=h)
    assert r.status_code == 200, r.text
    rows = rows_for(session_factory, sid)
    assert len(rows) == 3
    assert rows[-1].type == "in"
    assert rows[-1].open_key == str(sid)
    assert rows[-1].approved_by is not None


def test_completed_shift_can_request_manager_approval_with_setting_off(seeded, session_factory):
    """The kiosk offers approval even when allow_reentry_same_shift is OFF.

    A request is not a re-entry: it records nothing. The manager decision is
    the explicit override, and only approval creates the new open session.
    """
    client = seeded
    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)
    _complete_shift(session_factory, sid, pid)

    _set_now(datetime(2026, 1, 5, 18, 0, 0))
    token = make_token(session_factory, sid, when=datetime(2026, 1, 5, 18, 0, 0))
    r = client.post(
        "/api/clock/request",
        json={"token": token, "action": "in", "reason_code": "reentry"},
        headers={"X-Kiosk-ID": KIOSK},
    )
    assert r.status_code == 201, r.text
    assert r.json()["reason_code"] == "reentry"
    assert len(rows_for(session_factory, sid)) == 2  # request writes no attendance

    with session_factory() as s:
        req = s.scalars(select(ApprovalRequest).order_by(ApprovalRequest.id.desc())).first()
        assert req is not None
        assert req.status == "pending"
        rid = req.id

    h = admin_headers(client)
    r = client.post(
        f"/api/admin/approvals/{rid}/approve",
        json={"note": "early clock-out"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    rows = rows_for(session_factory, sid)
    assert len(rows) == 3
    assert rows[-1].type == "in"
    assert rows[-1].open_key == str(sid)
    assert rows[-1].approved_by is not None


def test_reentry_approval_out_without_open_session_rejected(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)
    with session_factory() as s:
        req = ApprovalRequest(
            staff_id=sid,
            action="out",
            reason_code="early_out",
            requested_at=datetime(2026, 1, 5, 12, 0, 0),
            status="pending",
        )
        s.add(req)
        s.commit()
        rid = req.id
    h = admin_headers(client)
    r = client.post(f"/api/admin/approvals/{rid}/approve", json={"note": "ok"}, headers=h)
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "no_open_session"
    assert rows_for(session_factory, sid) == []


# ---------------------------------------------------------------------------
# Approval-created out closes the real open session
# ---------------------------------------------------------------------------


def test_approved_out_closes_open_session_and_inherits_shift(seeded, session_factory):
    client = seeded
    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)
    with session_factory() as s:
        in_row = Attendance(
            staff_id=sid,
            type="in",
            timestamp=datetime(2026, 1, 5, 8, 0, 0),
            method="face",
            shift_id=pid,
            shift_date=date(2026, 1, 5),
            open_key=str(sid),
        )
        s.add(in_row)
        s.flush()
        req = ApprovalRequest(
            staff_id=sid,
            action="out",
            reason_code="early_out",
            requested_at=datetime(2026, 1, 5, 12, 0, 0),
            status="pending",
        )
        s.add(req)
        s.commit()
        in_id, rid = in_row.id, req.id
    h = admin_headers(client)
    r = client.post(f"/api/admin/approvals/{rid}/approve", json={"note": "ok"}, headers=h)
    assert r.status_code == 200, r.text
    rows = rows_for(session_factory, sid)
    assert len(rows) == 2
    assert rows[-1].type == "out"
    assert rows[-1].paired_in_id == in_id
    assert rows[-1].shift_id == pid
    assert rows[-1].shift_date == date(2026, 1, 5)
    assert rows[-1].flag == "early_departure"
    assert rows[0].open_key is None


# ---------------------------------------------------------------------------
# Missing clock-out auto-close
# ---------------------------------------------------------------------------


def test_close_missing_sessions_inherits_shift_and_pairs(seeded, session_factory):
    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)
    with session_factory() as s:
        in_row = Attendance(
            staff_id=sid,
            type="in",
            timestamp=datetime(2026, 1, 5, 8, 0, 0),
            method="face",
            shift_id=pid,
            shift_date=date(2026, 1, 5),
            open_key=str(sid),
        )
        s.add(in_row)
        s.commit()
        in_id = in_row.id

    # Tuesday 12:00 is well past Monday 17:00 + 4h.
    with session_factory() as s:
        closed = close_missing_sessions(s, now=datetime(2026, 1, 6, 12, 0, 0))
    assert len(closed) == 1
    rows = rows_for(session_factory, sid)
    assert len(rows) == 2
    out = rows[-1]
    assert out.flag == "missing_clock_out"
    assert out.paired_in_id == in_id
    assert out.shift_id == pid
    assert out.shift_date == date(2026, 1, 5)
    assert rows[0].open_key is None

    # Idempotent on a second run.
    with session_factory() as s:
        assert close_missing_sessions(s, now=datetime(2026, 1, 6, 13, 0, 0)) == []
    assert len(rows_for(session_factory, sid)) == 2


def test_close_missing_sessions_never_closes_before_the_in(seeded, session_factory):
    """A legacy in recorded after the policy cut must not produce out < in."""

    sid = add_staff(session_factory)
    pid = assign_office(session_factory, sid)  # Office 08:00-17:00, cut = 21:00
    with session_factory() as s:
        in_row = Attendance(
            staff_id=sid,
            type="in",
            timestamp=datetime(2026, 1, 5, 22, 0, 0),  # after 17:00 + 4h
            method="face",
            shift_id=pid,
            shift_date=date(2026, 1, 5),
            open_key=str(sid),
        )
        s.add(in_row)
        s.commit()
        in_id = in_row.id

    with session_factory() as s:
        closed = close_missing_sessions(s, now=datetime(2026, 1, 6, 12, 0, 0))
    assert closed == []
    rows = rows_for(session_factory, sid)
    assert len(rows) == 1
    assert rows[0].id == in_id
    assert rows[0].open_key == str(sid)  # left open for manual correction


# ---------------------------------------------------------------------------
# Pairing honours paired_in_id
# ---------------------------------------------------------------------------


def test_pair_sessions_honours_paired_in_id():
    zone = ZoneInfo("UTC")
    earlier = Attendance(
        id=1,
        staff_id=7,
        type="in",
        timestamp=datetime(2026, 1, 5, 8, 0, 0),
        shift_date=date(2026, 1, 5),
    )
    later_in = Attendance(
        id=2,
        staff_id=7,
        type="in",
        timestamp=datetime(2026, 1, 5, 11, 0, 0),
        shift_date=date(2026, 1, 5),
    )
    out = Attendance(
        id=3,
        staff_id=7,
        type="out",
        timestamp=datetime(2026, 1, 5, 12, 0, 0),
        shift_date=date(2026, 1, 5),
        paired_in_id=2,
    )
    paired = _pair_sessions([earlier, later_in, out], zone)
    by_in = {in_r.id if in_r else None: out_r.id if out_r else None for _, _, in_r, out_r in paired}
    assert by_in[2] == 3  # explicit link wins
    assert by_in[1] is None  # earlier open session does not steal the out


# ---------------------------------------------------------------------------
# Settings: no minimum-duration knob
# ---------------------------------------------------------------------------


def test_min_clockout_setting_removed(authenticated_client):
    r = authenticated_client.get("/api/admin/settings")
    assert r.status_code == 200
    assert "min_clockout_seconds" not in r.json()
