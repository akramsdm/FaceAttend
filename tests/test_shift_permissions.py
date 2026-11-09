"""Phase 3 Step E: can_edit_pattern authorization + delete 409 in-use guard."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Admin, Assignment, Pattern, PatternDay, Staff
from app.security import CSRF_HEADER_NAME, hash_password

MGR_PW = "manager horse battery staple"
MGR2_PW = "manager2 horse battery staple"


def hdrs(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


def office_payload(name="Sched"):
    return {
        "name": name,
        "early_window_min": 30,
        "grace_min": 10,
        "break_min": 60,
        "days": [
            {"weekday": wd, "start_time": "08:00", "end_time": "17:00", "overnight": False, "enabled": True}
            for wd in range(0, 5)
        ],
    }


def mk_mgr(session_factory, username="mgr", dept="Eng", pw=MGR_PW) -> Admin:
    with session_factory() as s:
        m = Admin(username=username, password_hash=hash_password(pw), role="manager", department_scope=dept)
        s.add(m)
        s.commit()
        s.refresh(m)
        return m


def mk_staff(session_factory, code="S1", dept="Eng"):
    with session_factory() as s:
        st = Staff(staff_code=code, name=code, department=dept)
        s.add(st)
        s.commit()
        return st.id


def mk_shared(session_factory, name, created_by=None, is_default=False):
    with session_factory() as s:
        p = Pattern(
            name=name,
            owner_staff_id=None,
            early_window_min=30,
            grace_min=10,
            break_min=60,
            is_default=is_default,
            kind="shared",
            created_by=created_by,
        )
        s.add(p)
        s.flush()
        for wd in range(0, 5):
            from datetime import time

            s.add(PatternDay(pattern_id=p.id, weekday=wd, start_time=time(8, 0), end_time=time(17, 0), overnight=False, enabled=True))
        s.commit()
        return p.id


# ---- can_edit_pattern unit cases (direct, one place) ----

def test_can_edit_pattern_admin_any(session_factory):
    from app.shift_service import can_edit_pattern

    with session_factory() as s:
        admin = Admin(username="a", password_hash="x", role="admin")
        mgr = Admin(username="m", password_hash="x", role="manager", department_scope="Eng")
        s.add_all([admin, mgr])
        s.flush()
        default = Pattern(name="D", owner_staff_id=None, early_window_min=30, grace_min=10, break_min=0, is_default=True, kind="shared")
        mgr_shared = Pattern(name="M", owner_staff_id=None, early_window_min=30, grace_min=10, break_min=0, is_default=False, kind="shared", created_by=mgr.id)
        s.add_all([default, mgr_shared])
        s.commit()
        assert can_edit_pattern(admin, default, s) is True
        assert can_edit_pattern(admin, mgr_shared, s) is True


def test_can_edit_pattern_manager_owns_created_shared(session_factory):
    from app.shift_service import can_edit_pattern

    with session_factory() as s:
        mgr = Admin(username="m1", password_hash="x", role="manager", department_scope="Eng")
        other = Admin(username="m2", password_hash="x", role="manager", department_scope="Eng")
        s.add_all([mgr, other])
        s.flush()
        mine = Pattern(name="mine", owner_staff_id=None, early_window_min=30, grace_min=10, break_min=0, is_default=False, kind="shared", created_by=mgr.id)
        theirs = Pattern(name="theirs", owner_staff_id=None, early_window_min=30, grace_min=10, break_min=0, is_default=False, kind="shared", created_by=other.id)
        nobody = Pattern(name="nobody", owner_staff_id=None, early_window_min=30, grace_min=10, break_min=0, is_default=False, kind="shared", created_by=None)
        s.add_all([mine, theirs, nobody])
        s.commit()
        assert can_edit_pattern(mgr, mine, s) is True
        assert can_edit_pattern(mgr, theirs, s) is False
        assert can_edit_pattern(mgr, nobody, s) is False


def test_can_edit_pattern_default_admin_only(session_factory):
    from app.shift_service import can_edit_pattern

    with session_factory() as s:
        mgr = Admin(username="m1", password_hash="x", role="manager", department_scope="Eng")
        admin = Admin(username="a1", password_hash="x", role="admin")
        s.add_all([mgr, admin])
        s.flush()
        default = Pattern(name="Office hours", owner_staff_id=None, early_window_min=30, grace_min=10, break_min=0, is_default=True, kind="shared")
        s.add(default)
        s.commit()
        assert can_edit_pattern(mgr, default, s) is False
        assert can_edit_pattern(admin, default, s) is True


def test_can_edit_pattern_personal_in_scope(session_factory):
    from app.shift_service import can_edit_pattern

    with session_factory() as s:
        eng = Admin(username="me", password_hash="x", role="manager", department_scope="Eng")
        hr = Admin(username="mh", password_hash="x", role="manager", department_scope="HR")
        staff_eng = Staff(staff_code="E1", name="E", department="Eng")
        s.add_all([eng, hr, staff_eng])
        s.flush()
        personal = Pattern(name="p", owner_staff_id=staff_eng.id, early_window_min=30, grace_min=10, break_min=0, is_default=False, kind="personal")
        s.add(personal)
        s.commit()
        assert can_edit_pattern(eng, personal, s) is True
        assert can_edit_pattern(hr, personal, s) is False


# ---- router uses function everywhere ----

def test_manager_creator_can_edit_and_delete_shared(client: TestClient, session_factory):
    mgr = mk_mgr(session_factory, "mgr", "Eng")
    h = hdrs(client, "mgr", MGR_PW)
    r = client.post("/api/admin/shifts/patterns", json=office_payload("MgrMade"), headers=h)
    assert r.status_code == 201
    pid = r.json()["id"]
    with session_factory() as s:
        p = s.get(Pattern, pid)
        assert p.created_by == mgr.id and p.kind == "shared"
    # creator manager can edit
    body = office_payload("MgrMade2")
    body["grace_min"] = 15
    r = client.put(f"/api/admin/shifts/patterns/{pid}", json=body, headers=h)
    assert r.status_code == 200 and r.json()["grace_min"] == 15
    # creator manager can delete (unused)
    r = client.delete(f"/api/admin/shifts/patterns/{pid}", headers=h)
    assert r.status_code == 200


def test_other_manager_cannot_edit_shared(client: TestClient, session_factory):
    """Manager who did NOT create a shared pattern cannot edit/delete it."""
    mk_mgr(session_factory, "mgr1", "Eng", MGR_PW)
    mk_mgr(session_factory, "mgr2", "Eng", MGR2_PW)
    # mgr1 creates
    r = client.post("/api/admin/login", json={"username": "mgr1", "password": MGR_PW})
    assert r.status_code == 200
    h1 = {CSRF_HEADER_NAME: r.json()["csrf_token"]}
    cookie1 = dict(client.cookies)
    r = client.post("/api/admin/shifts/patterns", json=office_payload("M1"), headers=h1)
    assert r.status_code == 201
    pid = r.json()["id"]
    # log in as mgr2 (separate session)
    r = client.post("/api/admin/login", json={"username": "mgr2", "password": MGR2_PW})
    assert r.status_code == 200
    h2 = {CSRF_HEADER_NAME: r.json()["csrf_token"]}
    r = client.put(f"/api/admin/shifts/patterns/{pid}", json=office_payload("M1x"), headers=h2)
    assert r.status_code == 403
    r = client.delete(f"/api/admin/shifts/patterns/{pid}", headers=h2)
    assert r.status_code == 403
    # creator (mgr1) still can
    client.cookies.clear()
    client.cookies.update(cookie1)
    r = client.delete(f"/api/admin/shifts/patterns/{pid}", headers=h1)
    assert r.status_code == 200


def test_manager_cannot_edit_default_admin_can(client: TestClient, session_factory, admin_id):
    mk_mgr(session_factory, "mgr", "Eng")
    # get manager session, then log in as admin (alice) in the same client
    r = client.post("/api/admin/login", json={"username": "mgr", "password": MGR_PW})
    assert r.status_code == 200
    mh = {CSRF_HEADER_NAME: r.json()["csrf_token"]}
    # a shipped default (admin-only)
    did = mk_shared(session_factory, "Office hours", created_by=None, is_default=True)
    body = office_payload("Office hours")
    r = client.put(f"/api/admin/shifts/patterns/{did}", json=body, headers=mh)
    assert r.status_code == 403
    assert "admin-only" in r.json().get("detail", "")
    # switch to admin (alice, created by admin_id fixture)
    r = client.post("/api/admin/login", json={"username": "alice", "password": "correct horse battery staple"})
    assert r.status_code == 200
    ah = {CSRF_HEADER_NAME: r.json()["csrf_token"]}
    r = client.put(f"/api/admin/shifts/patterns/{did}", json=body, headers=ah)
    assert r.status_code == 200


def test_delete_in_use_returns_409_with_count(client: TestClient, session_factory):
    mk_mgr(session_factory, "mgr", "Eng")
    h = hdrs(client, "mgr", MGR_PW)
    sid = mk_staff(session_factory, "E1", "Eng")
    r = client.post("/api/admin/shifts/patterns", json=office_payload("InUse"), headers=h)
    pid = r.json()["id"]
    # current/future assignment
    r = client.post("/api/admin/shifts/assignments", json={"pattern_id": pid, "staff_ids": [sid], "effective_from": str(date.today() + timedelta(days=1))}, headers=h)
    assert r.status_code == 200
    r = client.delete(f"/api/admin/shifts/patterns/{pid}", headers=h)
    assert r.status_code == 409
    detail = r.json().get("detail", "")
    assert "1" in detail and "current/future" in detail


def test_delete_past_only_assignment_blocked_by_fk_refs(client: TestClient, session_factory):
    """Current/future count drives the 409 message; past-only refs still block delete (FK/history)."""
    mk_mgr(session_factory, "mgr", "Eng")
    h = hdrs(client, "mgr", MGR_PW)
    sid = mk_staff(session_factory, "E2", "Eng")
    r = client.post("/api/admin/shifts/patterns", json=office_payload("Past"), headers=h)
    pid = r.json()["id"]
    # closed entirely in the past (0 current/future) but still a FK/history ref
    with session_factory() as s:
        s.add(Assignment(staff_id=sid, pattern_id=pid, effective_from=date(2020, 1, 1), effective_to=date(2020, 12, 31)))
        s.commit()
    r = client.delete(f"/api/admin/shifts/patterns/{pid}", headers=h)
    assert r.status_code == 409
    detail = r.json().get("detail", "")
    assert "assignment" in detail
