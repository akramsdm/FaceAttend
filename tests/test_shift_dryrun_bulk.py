"""Phase 3 Step F: exception ranges, bulk/holiday, dry-run (assign/save-week/exceptions/bulk)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Assignment, Attendance, Pattern, PatternDay, ShiftException, Staff
from app.security import CSRF_HEADER_NAME, hash_password

MGR_PW = "manager horse battery staple"


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


def mk_staff(session_factory, code="S1", dept="Eng"):
    with session_factory() as s:
        st = Staff(staff_code=code, name=code, department=dept)
        s.add(st)
        s.commit()
        return st.id


def mk_mgr(session_factory, username="mgr", dept="Eng"):
    with session_factory() as s:
        m = AdminLike(username=username, dept=dept)
        s.add(m.obj)
        s.commit()
        return m.obj.id


# lightweight admin creator
class AdminLike:
    def __init__(self, username, dept):
        from app.models import Admin

        self.obj = Admin(username=username, password_hash=hash_password(MGR_PW), role="manager", department_scope=dept)


def test_exception_range_creates_right_number_of_rows(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "R1", "Eng")
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "start_date": "2026-01-05", "end_date": "2026-01-09", "kind": "day_off", "reason": "leave"},
    )
    assert r.status_code in (200, 201)
    body = r.json()
    assert body["count"] == 5
    with session_factory() as s:
        n = s.scalar(select(func.count()).select_from(ShiftException).where(ShiftException.staff_id == sid))
    assert n == 5
    dates = {e["date"] for e in body["items"]}
    assert dates == {"2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"}


def test_exception_single_date_backward_compatible(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "R2", "Eng")
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "date": "2026-02-10", "kind": "day_off", "reason": "off"},
    )
    assert r.status_code in (200, 201)
    b = r.json()
    assert b["date"] == "2026-02-10" and b["kind"] == "day_off" and "id" in b


def test_exception_range_reason_and_custom_hours_required(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "R3", "Eng")
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "start_date": "2026-01-05", "end_date": "2026-01-06", "kind": "day_off", "reason": ""},
    )
    assert r.status_code == 422
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "start_date": "2026-01-05", "end_date": "2026-01-06", "kind": "custom_hours", "reason": "x"},
    )
    assert r.status_code == 422


def test_exception_range_max_62_days(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "R4", "Eng")
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "start_date": "2026-01-01", "end_date": "2026-03-10", "kind": "day_off", "reason": "long"},
    )
    assert r.status_code == 422
    assert "62" in r.json().get("detail", "")


def test_bulk_holiday_respects_department_scope(client: TestClient, session_factory):
    # admin + manager scoped to Eng
    with session_factory() as s:
        from app.models import Admin

        s.add(Admin(username="alice", password_hash=hash_password("correct horse battery staple"), role="admin"))
        s.add(Admin(username="mgr", password_hash=hash_password(MGR_PW), role="manager", department_scope="Eng"))
        s.commit()
    eng = mk_staff(session_factory, "E1", "Eng")
    hr = mk_staff(session_factory, "H1", "HR")
    # manager "everyone" => only their scope (Eng)
    r = client.post("/api/admin/login", json={"username": "mgr", "password": MGR_PW})
    mh = {CSRF_HEADER_NAME: r.json()["csrf_token"]}
    r = client.post(
        "/api/admin/shifts/exceptions/bulk",
        json={"start_date": "2026-05-01", "end_date": "2026-05-01", "kind": "day_off", "reason": "holiday", "everyone": True},
        headers=mh,
    )
    assert r.status_code == 200
    assert set(r.json()["affected_people"]) == {eng}
    with session_factory() as s:
        rows = s.scalars(select(ShiftException).where(ShiftException.date == date(2026, 5, 1))).all()
    assert {e.staff_id for e in rows} == {eng}

    # admin everyone => all
    r = client.post("/api/admin/login", json={"username": "alice", "password": "correct horse battery staple"})
    ah = {CSRF_HEADER_NAME: r.json()["csrf_token"]}
    r = client.post(
        "/api/admin/shifts/exceptions/bulk",
        json={"start_date": "2026-05-02", "end_date": "2026-05-02", "kind": "day_off", "reason": "holiday", "everyone": True},
        headers=ah,
    )
    assert r.status_code == 200
    assert set(r.json()["affected_people"]) == {eng, hr}

    # manager requests HR department explicitly => scope-filtered out (422 no staff in scope)
    r = client.post("/api/admin/login", json={"username": "mgr", "password": MGR_PW})
    mh = {CSRF_HEADER_NAME: r.json()["csrf_token"]}
    r = client.post(
        "/api/admin/shifts/exceptions/bulk",
        json={"start_date": "2026-05-03", "end_date": "2026-05-03", "kind": "day_off", "reason": "holiday", "department": "HR"},
        headers=mh,
    )
    assert r.status_code == 422


def test_dry_run_assign_writes_nothing(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "D1", "Eng")
    r = authenticated_client.post("/api/admin/shifts/patterns", json=office_payload("DR"))
    pid = r.json()["id"]
    r = authenticated_client.post(
        "/api/admin/shifts/assignments",
        json={"pattern_id": pid, "staff_ids": [sid], "effective_from": "2026-06-01", "dry_run": True},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["dry_run"] is True and sid in b["affected_people"]
    assert "assignments_ending" in b
    with session_factory() as s:
        n = s.scalar(select(func.count()).select_from(Assignment).where(Assignment.staff_id == sid))
    assert n == 0


def test_dry_run_exceptions_writes_nothing(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "D2", "Eng")
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "start_date": "2026-06-01", "end_date": "2026-06-03", "kind": "day_off", "reason": "x", "dry_run": True},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["dry_run"] is True and b["count"] == 3 and "exceptions_replaced" in b
    with session_factory() as s:
        n = s.scalar(select(func.count()).select_from(ShiftException).where(ShiftException.staff_id == sid))
    assert n == 0


def test_dry_run_bulk_writes_nothing(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "D3", "Eng")
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions/bulk",
        json={"start_date": "2026-06-01", "end_date": "2026-06-02", "kind": "day_off", "reason": "hol", "staff_ids": [sid], "dry_run": True},
    )
    assert r.status_code == 200
    assert r.json()["dry_run"] is True and r.json()["count"] == 2
    with session_factory() as s:
        n = s.scalar(select(func.count()).select_from(ShiftException).where(ShiftException.staff_id == sid))
    assert n == 0


def test_dry_run_save_week_writes_nothing(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "D4", "Eng")
    r = authenticated_client.post("/api/admin/shifts/patterns", json=office_payload("BaseW"))
    base_id = r.json()["id"]
    days = [{"weekday": wd, "start_time": "07:00", "end_time": "15:00", "overnight": False, "enabled": True} for wd in range(5)]
    r = authenticated_client.post(
        f"/api/admin/shifts/staff/{sid}/week",
        json={"pattern_id": base_id, "days": days, "early_window_min": 30, "grace_min": 10, "break_min": 60, "effective_from": "2026-07-01", "dry_run": True},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["dry_run"] is True and "assignments_ending" in b
    with session_factory() as s:
        personal = s.scalars(select(Pattern).where(Pattern.owner_staff_id == sid)).all()
        asn = s.scalars(select(Assignment).where(Assignment.staff_id == sid)).all()
    assert personal == [] and asn == []


def test_past_attendance_warning_appears(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "W1", "Eng")
    target = date(2026, 8, 10)

    with session_factory() as s:
        # attendance on target date (via shift_date)
        s.add(Attendance(staff_id=sid, type="in", method="manual", shift_date=target))
        s.commit()
    r = authenticated_client.post(
        "/api/admin/shifts/exceptions",
        json={"staff_id": sid, "date": target.isoformat(), "kind": "day_off", "reason": "x", "dry_run": True},
    )
    assert r.status_code == 200
    warn = r.json().get("warning") or ""
    assert "attendance" in warn.lower() and "differ" in warn.lower()
