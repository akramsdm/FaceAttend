from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


from app.models import Admin, Assignment, AuditLog, Pattern, PatternDay, ShiftException, Staff
from app.security import CSRF_HEADER_NAME, hash_password

MGR_PW = "manager horse battery staple"


def hdrs(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


@pytest.fixture()
def admin_headers(auth_headers):
    return auth_headers


@pytest.fixture()
def manager(session_factory: sessionmaker[Session]) -> Admin:
    with session_factory() as s:
        m = Admin(username="mgr", password_hash=hash_password(MGR_PW), role="manager", department_scope="Eng")
        s.add(m); s.commit(); s.refresh(m); return m


@pytest.fixture()
def manager_headers(client: TestClient, manager: Admin) -> dict[str, str]:
    return hdrs(client, "mgr", MGR_PW)


def mk_staff(session_factory, code, name, dept):
    with session_factory() as s:
        st = Staff(staff_code=code, name=name, department=dept)
        s.add(st); s.commit(); return st.id


def office_payload(name="Test Office"):
    return {"name": name, "early_window_min": 30, "grace_min": 10, "break_min": 60,
            "days": [{"weekday": wd, "start_time": "08:00", "end_time": "17:00", "overnight": False, "enabled": True}
                     for wd in range(0, 5)]}


# --- role scoping ---

def test_role_scoping_manager_sees_only_dept_staff(
    authenticated_client: TestClient, manager_headers, session_factory, manager
):
    eng = mk_staff(session_factory, "E1", "Eng One", "Eng")
    hr = mk_staff(session_factory, "H1", "HR One", "HR")
    # resolved for eng staff ok
    r = authenticated_client.get(f"/api/admin/shifts/staff/{eng}/resolved", params={"start": "2026-01-05", "end": "2026-01-05"}, headers=admin_headers if False else None)
    # (authenticated_client fixture already has admin csrf headers)
    r = authenticated_client.get(f"/api/admin/shifts/staff/{eng}/resolved", params={"start": "2026-01-05", "end": "2026-01-05"})
    assert r.status_code == 200
    # manager can access Eng, not HR
    client = authenticated_client
    r = client.get(f"/api/admin/shifts/staff/{eng}/resolved", params={"start": "2026-01-05", "end": "2026-01-05"}, headers=manager_headers)
    assert r.status_code == 200
    r = client.get(f"/api/admin/shifts/staff/{hr}/resolved", params={"start": "2026-01-05", "end": "2026-01-05"}, headers=manager_headers)
    assert r.status_code == 403

    # manager can create patterns (shared) and assign within scope
    r = client.post("/api/admin/shifts/patterns", json=office_payload("Mgr Pattern"), headers=manager_headers)
    assert r.status_code == 201
    pat_id = r.json()["id"]
    # assign HR staff forbidden
    r = client.post("/api/admin/shifts/assignments", json={"pattern_id": pat_id, "staff_ids": [hr], "effective_from": "2026-01-05"}, headers=manager_headers)
    assert r.status_code == 403
    # assign Eng ok
    r = client.post("/api/admin/shifts/assignments", json={"pattern_id": pat_id, "staff_ids": [eng], "effective_from": "2026-01-05"}, headers=manager_headers)
    assert r.status_code == 200
    assert r.json()["assigned"] == 1


# --- bulk assign closes old assignments ---

def test_bulk_assign_closes_old_assignments(authenticated_client: TestClient, session_factory):
    s1 = mk_staff(session_factory, "B1", "A", "Eng")
    s2 = mk_staff(session_factory, "B2", "B", "Eng")
    r = authenticated_client.post("/api/admin/shifts/patterns", json=office_payload("P1"))
    assert r.status_code == 201
    p1 = r.json()["id"]
    r = authenticated_client.post("/api/admin/shifts/patterns", json=office_payload("P2"))
    p2 = r.json()["id"]

    r = authenticated_client.post("/api/admin/shifts/assignments",
        json={"pattern_id": p1, "staff_ids": [s1, s2], "effective_from": "2026-01-01"})
    assert r.status_code == 200 and r.json()["assigned"] == 2

    # reassign effective 2026-02-01 -> old ends 2026-01-31
    r = authenticated_client.post("/api/admin/shifts/assignments",
        json={"pattern_id": p2, "staff_ids": [s1, s2], "effective_from": "2026-02-01"})
    assert r.status_code == 200

    with session_factory() as s:
        rows = s.scalars(select(Assignment).where(Assignment.staff_id == s1).order_by(Assignment.id)).all()
        assert len(rows) == 2
        old, new = rows[0], rows[1]
        assert old.pattern_id == p1
        assert str(old.effective_to) == "2026-01-31"
        assert new.pattern_id == p2
        assert str(new.effective_from) == "2026-02-01"
        assert new.effective_to is None
        # audit
        logs = s.scalars(select(AuditLog).where(AuditLog.action == "assign_pattern")).all()
        assert len(logs) >= 2


# --- customise via copy-on-write service (Phase 3 Step D) ---

def test_customise_identical_is_reset_and_assigns(authenticated_client: TestClient, session_factory):
    """Customising with an unchanged week is a reset (no new personal row); assigns base."""
    sid = mk_staff(session_factory, "C1", "C", "Eng")
    r = authenticated_client.post("/api/admin/shifts/patterns", json=office_payload("SharedX"))
    shared = r.json()
    with session_factory() as s:
        before = s.scalar(select(func.count()).select_from(Pattern)) or 0
    r = authenticated_client.post(f"/api/admin/shifts/patterns/{shared['id']}/customise/{sid}")
    assert r.status_code == 200
    body = r.json()
    # identical to base => reset: effective pattern is the shared base, assigned
    assert body["id"] == shared["id"]
    assert body["owner_staff_id"] is None
    with session_factory() as s:
        after = s.scalar(select(func.count()).select_from(Pattern)) or 0
        personal = s.scalars(select(Pattern).where(Pattern.owner_staff_id.isnot(None))).all()
        a = s.scalar(select(Assignment).where(Assignment.staff_id == sid, Assignment.effective_to.is_(None)))
    assert after == before  # no new personal row
    assert personal == []
    assert a is not None and a.pattern_id == shared["id"]


def test_customise_different_week_creates_personal_and_assigns(authenticated_client: TestClient, session_factory):
    """Saving a week different from base creates one personal row, assigned from effective date."""
    from app.shift_service import save_personal_week

    sid = mk_staff(session_factory, "C2", "C2", "Eng")
    r = authenticated_client.post("/api/admin/shifts/patterns", json=office_payload("SharedY"))
    base = r.json()
    days = [{"weekday": wd, "start_time": "07:00", "end_time": "15:00", "overnight": False, "enabled": True}
            for wd in range(0, 5)]
    windows = {"early_window_min": 30, "grace_min": 10, "break_min": 60}
    with session_factory() as s:
        res = save_personal_week(s, sid, days, windows, date(2026, 1, 5), base_pattern_id=base["id"])
        s.commit()
    assert res["status"] == "personal_saved"
    with session_factory() as s:
        personals = s.scalars(select(Pattern).where(Pattern.owner_staff_id == sid)).all()
        a = s.scalar(select(Assignment).where(Assignment.staff_id == sid, Assignment.effective_to.is_(None)))
        # shared base unchanged
        shared = s.get(Pattern, base["id"])
    assert len(personals) == 1 and personals[0].based_on_pattern_id == base["id"]
    assert a is not None and a.pattern_id == personals[0].id
    assert shared.early_window_min == 30 and shared.name == "SharedY"
    # GET /patterns returns only shared
    r = authenticated_client.get("/api/admin/shifts/patterns")
    assert all(p.get("owner_staff_id") is None for p in r.json())



# --- exception wins over pattern (resolved view) ---

def test_exception_wins_over_pattern(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "D1", "D", "Eng")
    r = authenticated_client.post("/api/admin/shifts/patterns", json=office_payload("PX"))
    pid = r.json()["id"]
    authenticated_client.post("/api/admin/shifts/assignments",
        json={"pattern_id": pid, "staff_ids": [sid], "effective_from": "2026-01-01"})
    # Mon 2026-01-05 would normally resolve
    r = authenticated_client.get(f"/api/admin/shifts/staff/{sid}/resolved",
        params={"start": "2026-01-05", "end": "2026-01-05"})
    assert r.status_code == 200
    assert r.json()["items"][0]["shift"] is not None
    # add day_off exception
    r = authenticated_client.post("/api/admin/shifts/exceptions",
        json={"staff_id": sid, "date": "2026-01-05", "kind": "day_off", "reason": "leave"})
    assert r.status_code == 201
    r = authenticated_client.get(f"/api/admin/shifts/staff/{sid}/resolved",
        params={"start": "2026-01-05", "end": "2026-01-05"})
    assert r.json()["items"][0]["shift"] is None

    # custom hours wins with custom time
    r = authenticated_client.post("/api/admin/shifts/exceptions",
        json={"staff_id": sid, "date": "2026-01-06", "kind": "custom_hours",
              "start_time": "10:00", "end_time": "14:00", "reason": "appt"})
    assert r.status_code == 201
    r = authenticated_client.get(f"/api/admin/shifts/staff/{sid}/resolved",
        params={"start": "2026-01-06", "end": "2026-01-06"})
    sh = r.json()["items"][0]["shift"]
    assert sh is not None and sh["source"] == "exception"
    assert "10:00:00" in sh["start"]


# --- validation errors ---

def test_validation_errors(authenticated_client: TestClient, session_factory):
    # end before start, not overnight
    bad = {"name": "Bad", "early_window_min": 30, "grace_min": 10, "break_min": 0,
           "days": [{"weekday": 0, "start_time": "17:00", "end_time": "08:00", "overnight": False, "enabled": True}]}
    r = authenticated_client.post("/api/admin/shifts/patterns", json=bad)
    assert r.status_code == 422
    # break not shorter
    bad2 = office_payload("Bad2"); bad2["break_min"] = 600
    r = authenticated_client.post("/api/admin/shifts/patterns", json=bad2)
    assert r.status_code == 422
    # exception missing reason
    sid = mk_staff(session_factory, "V1", "V", "Eng")
    r = authenticated_client.post("/api/admin/shifts/exceptions",
        json={"staff_id": sid, "date": "2026-01-05", "kind": "day_off", "reason": ""})
    assert r.status_code == 422
    # custom hours missing times
    r = authenticated_client.post("/api/admin/shifts/exceptions",
        json={"staff_id": sid, "date": "2026-01-05", "kind": "custom_hours", "reason": "x"})
    assert r.status_code == 422
