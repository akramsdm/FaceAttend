"""Phase 3 Step G: GET /people (scope, working_today, night strip, constant queries) + /staff/{id}/history order."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Admin, Assignment, Pattern, PatternDay, ShiftException, Staff
from app.security import CSRF_HEADER_NAME, hash_password
from app.shift_service import save_personal_week

MGR_PW = "manager horse battery staple"
ADMIN_PW = "correct horse battery staple"


def hdrs(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


def mk_staff(session_factory, code: str, name: str, dept: str) -> int:
    with session_factory() as s:
        st = Staff(staff_code=code, name=name, department=dept)
        s.add(st)
        s.commit()
        return st.id


def mk_pattern(session_factory, name: str, days_spec) -> int:
    from datetime import time

    with session_factory() as s:
        p = Pattern(
            name=name,
            owner_staff_id=None,
            early_window_min=30,
            grace_min=10,
            break_min=0,
            is_default=False,
            kind="shared",
        )
        s.add(p)
        s.flush()
        for wd, stt, ent, ov in days_spec:
            sh, sm = [int(x) for x in stt.split(":")]
            eh, em = [int(x) for x in ent.split(":")]
            s.add(PatternDay(pattern_id=p.id, weekday=wd, start_time=time(sh, sm), end_time=time(eh, em), overnight=ov, enabled=True))
        s.commit()
        return p.id


def assign(session_factory, staff_id: int, pattern_id: int, effective_from: date):
    with session_factory() as s:
        s.add(Assignment(staff_id=staff_id, pattern_id=pattern_id, effective_from=effective_from, effective_to=None))
        s.commit()


WEEK_START = date(2026, 1, 5)  # Monday


def test_people_scope_filtering(client: TestClient, session_factory):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.add(Admin(username="mgr", password_hash=hash_password(MGR_PW), role="manager", department_scope="Eng"))
        s.commit()
    e1 = mk_staff(session_factory, "E1", "Eng One", "Eng")
    h1 = mk_staff(session_factory, "H1", "HR One", "HR")
    pid = mk_pattern(session_factory, "Office", [(0, "08:00", "17:00", False), (1, "08:00", "17:00", False), (2, "08:00", "17:00", False), (3, "08:00", "17:00", False), (4, "08:00", "17:00", False)])
    assign(session_factory, e1, pid, date(2026, 1, 1))
    assign(session_factory, h1, pid, date(2026, 1, 1))

    # manager sees only Eng
    hdr = hdrs(client, "mgr", MGR_PW)
    r = client.get("/api/admin/shifts/people", params={"week_start": WEEK_START.isoformat()}, headers=hdr)
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["id"] for i in items} == {e1}
    assert all(i["department"] == "Eng" for i in items)

    # admin sees all, can filter by department/search
    hdr = hdrs(client, "alice", ADMIN_PW)
    r = client.get("/api/admin/shifts/people", params={"week_start": WEEK_START.isoformat()}, headers=hdr)
    assert {i["id"] for i in r.json()["items"]} == {e1, h1}
    r = client.get("/api/admin/shifts/people", params={"week_start": WEEK_START.isoformat(), "department": "HR"}, headers=hdr)
    assert {i["id"] for i in r.json()["items"]} == {h1}
    r = client.get("/api/admin/shifts/people", params={"week_start": WEEK_START.isoformat(), "search": "Eng One"}, headers=hdr)
    assert {i["id"] for i in r.json()["items"]} == {e1}


def test_people_working_today_filter(client: TestClient, session_factory):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.commit()
    today = date.today()
    # align week_start to a Monday containing today
    week_start = today - timedelta(days=today.weekday())
    wd_today = today.weekday()
    a = mk_staff(session_factory, "A1", "Works", "Eng")
    b = mk_staff(session_factory, "B1", "Off", "Eng")
    work_days = [(wd_today, "08:00", "17:00", False)]
    off_days = [(d, "08:00", "17:00", False) for d in range(7) if d != wd_today]
    pa = mk_pattern(session_factory, "TodayOnly", work_days)
    pb = mk_pattern(session_factory, "NotToday", off_days)
    assign(session_factory, a, pa, date(2020, 1, 1))
    assign(session_factory, b, pb, date(2020, 1, 1))
    hdr = hdrs(client, "alice", ADMIN_PW)
    r = client.get("/api/admin/shifts/people", params={"week_start": week_start.isoformat(), "working_today": True}, headers=hdr)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert a in ids
    assert b not in ids


def test_people_night_shift_in_week_strip(client: TestClient, session_factory):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.commit()
    sid = mk_staff(session_factory, "N1", "Night", "Eng")
    # night Mon 22:00-06:00 overnight
    pid = mk_pattern(session_factory, "Night", [(0, "22:00", "06:00", True)])
    assign(session_factory, sid, pid, date(2026, 1, 1))
    hdr = hdrs(client, "alice", ADMIN_PW)
    r = client.get("/api/admin/shifts/people", params={"week_start": WEEK_START.isoformat()}, headers=hdr)
    assert r.status_code == 200
    row = next(i for i in r.json()["items"] if i["id"] == sid)
    assert len(row["days"]) == 7
    mon = next(d for d in row["days"] if d["date"] == "2026-01-05")
    assert mon["shift"] is not None
    assert mon["shift"]["overnight"] is True
    assert "22:00:00" in mon["shift"]["start"]
    assert "2026-01-06" in mon["shift"]["end"]  # crosses midnight
    tue = next(d for d in row["days"] if d["date"] == "2026-01-06")
    assert tue["shift"] is None  # pattern only Mon in this spec
    assert row["schedule_label"] == "Night"


def test_people_survives_day_change_exception(client: TestClient, session_factory):
    """A day change in the week must not crash the People view.

    Regression: ``_shifts_data_for`` returned ORM ShiftException objects while
    ``_people_rows`` indexed them as dicts, raising
    ``TypeError: 'ShiftException' object is not subscriptable`` and leaving the
    reassignment UI stuck on "Loading…" after any day change was saved.
    """
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.commit()
    sid = mk_staff(session_factory, "X1", "Exception Person", "Eng")
    pid = mk_pattern(session_factory, "OfficeX", [(wd, "08:00", "17:00", False) for wd in range(5)])
    assign(session_factory, sid, pid, date(2026, 1, 1))
    hdr = hdrs(client, "alice", ADMIN_PW)

    r = client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "date": "2026-01-05", "kind": "day_off", "reason": "leave"},
        headers=hdr,
    )
    assert r.status_code == 201, r.text

    r = client.get(
        "/api/admin/shifts/people",
        params={"week_start": WEEK_START.isoformat()},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    row = next(i for i in r.json()["items"] if i["id"] == sid)
    assert row["source"] == "changed"
    mon = next(d for d in row["days"] if d["date"] == "2026-01-05")
    assert mon["shift"] is None
    tue = next(d for d in row["days"] if d["date"] == "2026-01-06")
    assert tue["shift"] is not None


def test_people_constant_query_count_not_per_person(client: TestClient, session_factory):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.commit()
    pid = mk_pattern(session_factory, "OfficeQ", [(wd, "08:00", "17:00", False) for wd in range(5)])
    ids1 = [mk_staff(session_factory, "Q1", "Q1", "Eng")]
    for sid in ids1:
        assign(session_factory, sid, pid, date(2026, 1, 1))
    hdr = hdrs(client, "alice", ADMIN_PW)
    r1 = client.get("/api/admin/shifts/people", params={"week_start": WEEK_START.isoformat(), "search": "Q1", "debug": True}, headers=hdr)
    assert r1.status_code == 200
    c1 = r1.json()["query_count"]
    assert len(r1.json()["items"]) == 1

    # add more people (not matched by Q1 search) -> different search to include all Q*
    for i in range(2, 7):
        sid = mk_staff(session_factory, f"Q{i}", f"Q{i}", "Eng")
        assign(session_factory, sid, pid, date(2026, 1, 1))
    r5 = client.get("/api/admin/shifts/people", params={"week_start": WEEK_START.isoformat(), "search": "Q", "debug": True}, headers=hdr)
    assert r5.status_code == 200
    c5 = r5.json()["query_count"]
    assert len(r5.json()["items"]) == 6
    # constant queries: not one per person (count must not grow with N)
    assert c5 == c1, f"query count grew with staff: 1 person={c1}, 6 people={c5}"
    assert c5 <= 8, f"expected small constant query count, got {c5}"


def test_history_time_order_assignments_personal_exceptions(client: TestClient, session_factory):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.commit()
    sid = mk_staff(session_factory, "HST", "Hist", "Eng")
    base = mk_pattern(session_factory, "BaseH", [(wd, "08:00", "17:00", False) for wd in range(5)])
    # assignment effective 2026-01-01
    assign(session_factory, sid, base, date(2026, 1, 1))
    # personal version via service (different week) effective 2026-02-01
    days = [{"weekday": wd, "start_time": "07:00", "end_time": "15:00", "overnight": False, "enabled": True} for wd in range(5)]
    with session_factory() as s:
        res = save_personal_week(s, sid, days, {"early_window_min": 30, "grace_min": 10, "break_min": 0}, date(2026, 2, 1), base_pattern_id=base)
        s.commit()
        assert res["status"] == "personal_saved"
    # exceptions out of insertion order
    hdr = hdrs(client, "alice", ADMIN_PW)
    r = client.post("/api/admin/shifts/exceptions", json={"staff_id": sid, "date": "2026-03-10", "kind": "day_off", "reason": "later"}, headers=hdr)
    assert r.status_code in (200, 201)
    r = client.post("/api/admin/shifts/exceptions", json={"staff_id": sid, "date": "2026-01-15", "kind": "day_off", "reason": "earlier"}, headers=hdr)
    assert r.status_code in (200, 201)

    r = client.get(f"/api/admin/shifts/staff/{sid}/history", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["staff_id"] == sid
    # assignments in time order
    assert [a["effective_from"] for a in body["assignments"]] == sorted(a["effective_from"] for a in body["assignments"])
    # exceptions in time order with actor/reason
    ex_dates = [e["date"] for e in body["exceptions"]]
    assert ex_dates == ["2026-01-15", "2026-03-10"]
    assert body["exceptions"][0]["reason"] == "earlier"
    assert body["exceptions"][1]["reason"] == "later"
    assert body["exceptions"][0]["created_by"] is not None
    # personal versions present with base + created_at
    assert len(body["personal_versions"]) >= 1
    pv = body["personal_versions"][0]
    assert pv["based_on_pattern_id"] == base and pv["based_on_name"] == "BaseH"
    assert pv["created_at"] is not None
    # unified events sorted by at/date
    ats = [e.get("at") or "" for e in body["events"]]
    assert ats == sorted(ats)


def test_history_scope_forbidden_for_other_dept(client: TestClient, session_factory):
    with session_factory() as s:
        s.add(Admin(username="mgr", password_hash=hash_password(MGR_PW), role="manager", department_scope="Eng"))
        s.commit()
    hr = mk_staff(session_factory, "HRX", "HRX", "HR")
    hdr = hdrs(client, "mgr", MGR_PW)
    r = client.get(f"/api/admin/shifts/staff/{hr}/history", headers=hdr)
    assert r.status_code == 403
