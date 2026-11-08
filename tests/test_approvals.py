from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.approvals import close_missing_sessions
from app.models import Admin, ApprovalRequest, Assignment, Attendance, AuditLog, Pattern, Staff
from app.routers.shifts import seed_default_patterns
from app.security import CSRF_HEADER_NAME, hash_password
from app.settings_store import set_values

MGR_PW = "manager horse battery staple"


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


@pytest.fixture()
def admin_hdrs(auth_headers):
    return auth_headers


@pytest.fixture()
def setup(session_factory: sessionmaker[Session]):
    with session_factory() as s:
        seed_default_patterns(s)
        set_values(s, {"missing_close_hours": 4})
        eng = Staff(staff_code="E1", name="Eng One", department="Eng")
        hr = Staff(staff_code="H1", name="HR One", department="HR")
        s.add_all([eng, hr]); s.commit(); s.refresh(eng); s.refresh(hr)
        office = s.scalar(select(Pattern).where(Pattern.name == "Office hours", Pattern.is_default.is_(True)))
        for st in (eng, hr):
            s.add(Assignment(staff_id=st.id, pattern_id=office.id,
                             effective_from=datetime(2026, 1, 1).date(), effective_to=None))
        mgr = Admin(username="mgr", password_hash=hash_password(MGR_PW), role="manager", department_scope="Eng")
        s.add(mgr); s.commit(); s.refresh(mgr)
        return {"eng": eng.id, "hr": hr.id, "mgr": mgr.id, "pattern": office.id}


def mk_request(session_factory, staff_id: int, action="in", reason="no_shift") -> int:
    with session_factory() as s:
        r = ApprovalRequest(staff_id=staff_id, action=action, reason_code=reason,
                            requested_at=datetime(2026, 1, 5, 9, 0, 0), status="pending")
        s.add(r); s.commit(); return r.id


def test_approve_creates_attendance_flag_and_audits(authenticated_client: TestClient, session_factory, setup):
    rid = mk_request(session_factory, setup["eng"], action="in", reason="out_of_window")
    r = authenticated_client.post(f"/api/admin/approvals/{rid}/approve", json={"note": "ok"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "approved" and d["flag"] == "out_of_window"
    att_id = d["attendance_id"]
    with session_factory() as s:
        req = s.get(ApprovalRequest, rid)
        assert req.status == "approved" and req.decided_by is not None
        att = s.get(Attendance, att_id)
        assert att is not None and att.staff_id == setup["eng"]
        assert att.type == "in" and att.flag == "out_of_window"
        assert att.approved_by == req.decided_by
        logs = s.scalars(select(AuditLog).where(AuditLog.action == "approve_request",
                          AuditLog.target == f"approval:{rid}")).all()
        assert len(logs) == 1 and f"attendance:{att_id}" in (logs[0].detail or "")

    # early_out maps to early_departure
    rid2 = mk_request(session_factory, setup["eng"], action="out", reason="early_out")
    r2 = authenticated_client.post(f"/api/admin/approvals/{rid2}/approve", json={})
    assert r2.status_code == 200 and r2.json()["flag"] == "early_departure"


def test_reject_audited_and_no_attendance(authenticated_client: TestClient, session_factory, setup):
    rid = mk_request(session_factory, setup["eng"], action="in", reason="no_shift")
    r = authenticated_client.post(f"/api/admin/approvals/{rid}/reject", json={"note": "denied"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    with session_factory() as s:
        req = s.get(ApprovalRequest, rid)
        assert req.status == "rejected" and req.decision_note == "denied"
        n = s.scalar(select(func.count()).select_from(Attendance).where(Attendance.staff_id == setup["eng"])) or 0
        assert n == 0
        logs = s.scalars(select(AuditLog).where(AuditLog.action == "reject_request",
                          AuditLog.target == f"approval:{rid}")).all()
        assert len(logs) == 1 and "denied" in (logs[0].detail or "")


def test_department_scoping(authenticated_client: TestClient, client: TestClient, session_factory, setup):
    rid_eng = mk_request(session_factory, setup["eng"], reason="no_shift")
    rid_hr = mk_request(session_factory, setup["hr"], reason="no_shift")
    mgr_h = login(client, "mgr", MGR_PW)
    # manager sees only Eng request
    lr = client.get("/api/admin/approvals?status=pending", headers=mgr_h)
    assert lr.status_code == 200
    ids = {x["id"] for x in lr.json()}
    assert rid_eng in ids and rid_hr not in ids
    # manager cannot decide HR request
    r = client.post(f"/api/admin/approvals/{rid_hr}/approve", json={}, headers=mgr_h)
    assert r.status_code == 403
    # admin can decide HR (separate login; note: sets admin cookie on shared client)
    admin_h = login(client, "alice", "correct horse battery staple")
    r2 = client.post(f"/api/admin/approvals/{rid_hr}/approve", json={}, headers=admin_h)
    assert r2.status_code == 200
    # re-login manager for subsequent manager-scoped checks
    mgr_h = login(client, "mgr", MGR_PW)
    r3 = client.post(f"/api/admin/approvals/{rid_eng}/reject", json={"note": "x"}, headers=mgr_h)
    assert r3.status_code == 200

    # missing clock-out scoping
    with session_factory() as s:
        s.add(Attendance(staff_id=setup["hr"], type="out", timestamp=datetime(2026, 1, 5, 21, 0, 0),
                         method="manual", flag="missing_clock_out", shift_date=datetime(2026, 1, 5).date(),
                         edit_reason="auto_missing:999"))
        s.add(Attendance(staff_id=setup["eng"], type="out", timestamp=datetime(2026, 1, 5, 21, 0, 0),
                         method="manual", flag="missing_clock_out", shift_date=datetime(2026, 1, 5).date(),
                         edit_reason="auto_missing:998"))
        s.commit()
    mr = client.get("/api/admin/approvals/missing-clockout", headers=mgr_h)
    assert mr.status_code == 200
    mgr_rows = mr.json()
    assert all(x["staff_id"] == setup["eng"] for x in mgr_rows)
    assert any(x["staff_id"] == setup["eng"] for x in mgr_rows)
    admin_h = login(client, "alice", "correct horse battery staple")
    hr_list = client.get("/api/admin/approvals/missing-clockout", headers=admin_h).json()
    hr_row = [x for x in hr_list if x["staff_id"] == setup["hr"]][0]
    mgr_h = login(client, "mgr", MGR_PW)
    bad = client.post(f"/api/admin/approvals/missing-clockout/{hr_row['id']}/close",
                      json={"timestamp": "2026-01-05T17:00:00", "reason": "real"}, headers=mgr_h)
    assert bad.status_code == 403


def test_session_closing_at_exact_cutoff(session_factory, setup):
    # Office 08:00-17:00 Mon 2026-01-05; close 4h after end => 21:00:00 local/UTC (default TZ=UTC)
    with session_factory() as s:
        seed_default_patterns(s); set_values(s, {"missing_close_hours": 4})
        s.add(Attendance(staff_id=setup["eng"], type="in", timestamp=datetime(2026, 1, 5, 8, 0, 0),
                         method="face", shift_date=datetime(2026, 1, 5).date()))
        s.commit()
        # before cut-off: no close
        closed = close_missing_sessions(s, now=datetime(2026, 1, 5, 20, 59, 59))
        assert closed == []
        n = s.scalar(select(func.count()).select_from(Attendance).where(
            Attendance.staff_id == setup["eng"], Attendance.flag == "missing_clock_out")) or 0
        assert n == 0

    with session_factory() as s:
        # exactly at cut-off: closes
        closed = close_missing_sessions(s, now=datetime(2026, 1, 5, 21, 0, 0))
        assert len(closed) == 1 and closed[0]["staff_id"] == setup["eng"]
        row = s.scalar(select(Attendance).where(
            Attendance.staff_id == setup["eng"], Attendance.flag == "missing_clock_out"))
        assert row is not None and row.type == "out"
        assert row.timestamp == datetime(2026, 1, 5, 21, 0, 0)
        # idempotent
        closed2 = close_missing_sessions(s, now=datetime(2026, 1, 5, 22, 0, 0))
        assert closed2 == []
        n = s.scalar(select(func.count()).select_from(Attendance).where(
            Attendance.staff_id == setup["eng"], Attendance.flag == "missing_clock_out")) or 0
        assert n == 1


def test_correction_needs_a_reason(authenticated_client: TestClient, session_factory, setup):
    with session_factory() as s:
        out = Attendance(staff_id=setup["eng"], type="out", timestamp=datetime(2026, 1, 5, 21, 0, 0),
                         method="manual", flag="missing_clock_out", shift_date=datetime(2026, 1, 5).date(),
                         edit_reason="auto_missing:1")
        s.add(out); s.commit(); out_id = out.id
    # missing/empty reason rejected
    r = authenticated_client.post(f"/api/admin/approvals/missing-clockout/{out_id}/close",
                                  json={"timestamp": "2026-01-05T17:00:00", "reason": ""})
    assert r.status_code == 422
    r2 = authenticated_client.post(f"/api/admin/approvals/missing-clockout/{out_id}/close",
                                   json={"timestamp": "2026-01-05T17:00:00", "reason": "   "})
    assert r2.status_code == 422
    # with reason succeeds, row kept (not deleted)
    r3 = authenticated_client.post(f"/api/admin/approvals/missing-clockout/{out_id}/close",
                                   json={"timestamp": "2026-01-05T17:05:00", "reason": "left at 17:05"})
    assert r3.status_code == 200
    with session_factory() as s:
        row = s.get(Attendance, out_id)
        assert row is not None  # never deleted
        assert row.flag == "missing_clock_out_corrected"
        assert row.edit_reason == "left at 17:05"
        assert row.edited_by is not None
