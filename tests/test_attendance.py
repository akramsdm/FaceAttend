from __future__ import annotations

import csv
import io
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Attendance, AuditLog, Staff


def add_staff(session_factory: sessionmaker[Session], code="S001", name="Ada", dept="Eng") -> int:
    with session_factory() as session:
        s = Staff(staff_code=code, name=name, department=dept)
        session.add(s)
        session.commit()
        return s.id


def add_attendance(
    session_factory: sessionmaker[Session],
    staff_id: int,
    typ: str,
    ts: datetime,
    method: str = "face",
) -> int:
    with session_factory() as session:
        row = Attendance(staff_id=staff_id, type=typ, timestamp=ts, method=method)
        session.add(row)
        session.commit()
        return row.id


def auth(client: TestClient) -> dict[str, str]:
    # login handled by fixtures? authenticated_client exists; build headers via login
    r = client.post("/api/admin/login", json={"username": "alice", "password": "correct horse battery staple"})
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def test_routes_require_admin(client: TestClient) -> None:
    assert client.get("/api/admin/attendance").status_code == 401
    assert client.post("/api/admin/attendance", json={}).status_code == 401
    assert client.get("/api/admin/attendance/export.csv").status_code == 401
    assert client.get("/api/admin/attendance/report/monthly.csv?year=2026&month=1").status_code == 401


def test_filters_date_range_staff_department(
    authenticated_client: TestClient, session_factory: sessionmaker[Session], admin_id: int
) -> None:
    s1 = add_staff(session_factory, "S001", "Ada", "Eng")
    s2 = add_staff(session_factory, "S002", "Bob", "HR")
    # naive UTC timestamps
    add_attendance(session_factory, s1, "in", datetime(2026, 1, 10, 8, 0, 0))
    add_attendance(session_factory, s1, "out", datetime(2026, 1, 10, 17, 0, 0))
    add_attendance(session_factory, s2, "in", datetime(2026, 1, 11, 8, 0, 0))
    add_attendance(session_factory, s1, "in", datetime(2026, 1, 12, 8, 0, 0))

    r = authenticated_client.get("/api/admin/attendance", params={"start": "2026-01-10", "end": "2026-01-11"})
    assert r.status_code == 200
    assert [x["staff_id"] for x in r.json()] == [s1, s1, s2]

    r = authenticated_client.get("/api/admin/attendance", params={"staff_id": s1})
    assert [x["id"] for x in r.json()]  # has s1 rows
    assert all(x["staff_id"] == s1 for x in r.json())

    r = authenticated_client.get("/api/admin/attendance", params={"department": "HR"})
    assert len(r.json()) == 1
    assert r.json()[0]["staff_id"] == s2
    assert r.json()[0]["department"] == "HR"


def test_manual_record_requires_reason_and_audits(
    authenticated_client: TestClient, session_factory: sessionmaker[Session], admin_id: int
) -> None:
    s1 = add_staff(session_factory)
    # missing reason
    r = authenticated_client.post(
        "/api/admin/attendance",
        json={"staff_id": s1, "type": "in", "timestamp": "2026-01-10T09:00:00Z"},
    )
    assert r.status_code == 422

    r = authenticated_client.post(
        "/api/admin/attendance",
        json={"staff_id": s1, "type": "in", "timestamp": "2026-01-10T09:00:00Z", "reason": "Reception sign-in"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["method"] == "manual"
    assert body["edited_by"] == admin_id
    assert body["edit_reason"] == "Reception sign-in"
    assert body["staff_code"] == "S001"

    with session_factory() as session:
        log = session.scalar(select(AuditLog).where(AuditLog.action == "manual_attendance"))
        assert log is not None
        assert log.admin_id == admin_id
        assert log.target == f"attendance:{body['id']}"
        assert "Reception sign-in" in (log.detail or "")


def test_correction_requires_reason_and_audits(
    authenticated_client: TestClient, session_factory: sessionmaker[Session], admin_id: int
) -> None:
    s1 = add_staff(session_factory)
    att_id = add_attendance(session_factory, s1, "in", datetime(2026, 1, 10, 9, 0, 0))

    r = authenticated_client.patch(f"/api/admin/attendance/{att_id}", json={"type": "out"})
    assert r.status_code == 422  # reason required

    r = authenticated_client.patch(
        f"/api/admin/attendance/{att_id}",
        json={"type": "out", "reason": "Wrong type"},
    )
    assert r.status_code == 200
    assert r.json()["type"] == "out"
    assert r.json()["edited_by"] == admin_id
    assert r.json()["edit_reason"] == "Wrong type"

    with session_factory() as session:
        row = session.get(Attendance, att_id)
        assert row is not None and row.type == "out"
        log = session.scalar(select(AuditLog).where(AuditLog.action == "correct_attendance"))
        assert log is not None and log.admin_id == admin_id


def test_export_csv_columns(
    authenticated_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    s1 = add_staff(session_factory)
    add_attendance(session_factory, s1, "in", datetime(2026, 1, 10, 8, 0, 0))
    r = authenticated_client.get("/api/admin/attendance/export.csv", params={"start": "2026-01-10", "end": "2026-01-10"})
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    rows = list(csv.reader(io.StringIO(r.text)))
    header = rows[0]
    # Step J extends exports with shift fields (prefix preserved for compatibility)
    assert header[:9] == [
        "id","staff_id","staff_code","staff_name","department","type",
        "timestamp_utc","local_time","method",
    ]
    assert "shift_date" in header and "flag" in header and "edited_by" in header and "edit_reason" in header
    assert rows[1][2] == "S001"
    assert rows[1][5] == "in"


def test_monthly_report_hours_pairs_and_unpaired_in(
    authenticated_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    s1 = add_staff(session_factory)
    # complete pair: 8h, and an unpaired in
    add_attendance(session_factory, s1, "in", datetime(2026, 1, 10, 9, 0, 0))
    add_attendance(session_factory, s1, "out", datetime(2026, 1, 10, 17, 0, 0))
    add_attendance(session_factory, s1, "in", datetime(2026, 1, 11, 9, 0, 0))  # unpaired

    r = authenticated_client.get("/api/admin/attendance/report/monthly.csv", params={"year": 2026, "month": 1})
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["days_present"] == "2"
    assert row["hours_worked"] == "8.00"
    assert row["unpaired_in_days"] == "2026-01-11"


def test_late_flag_at_exact_boundary(
    authenticated_client: TestClient, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # default WORK_START 09:00, GRACE 15 => late after 09:15:00 local (UTC here)
    from app.config import get_settings
    get_settings.cache_clear()
    s1 = add_staff(session_factory)
    add_attendance(session_factory, s1, "in", datetime(2026, 1, 10, 9, 15, 0))  # at boundary: not late
    add_attendance(session_factory, s1, "out", datetime(2026, 1, 10, 17, 0, 0))
    r = authenticated_client.get("/api/admin/attendance/report/monthly.csv", params={"year": 2026, "month": 1})
    row = list(csv.DictReader(io.StringIO(r.text)))[0]
    assert row["late_arrivals"] == "0"

    add_attendance(session_factory, s1, "in", datetime(2026, 1, 12, 9, 15, 1))  # after boundary: late
    r = authenticated_client.get("/api/admin/attendance/report/monthly.csv", params={"year": 2026, "month": 1})
    row = list(csv.DictReader(io.StringIO(r.text)))[0]
    assert row["late_arrivals"] == "1"
    get_settings.cache_clear()
