from __future__ import annotations

import csv
import io
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.metrics import person_metrics
from app.models import Admin, Assignment, Attendance, Pattern, PatternDay, Staff
from app.security import CSRF_HEADER_NAME, hash_password

ADMIN_PW = "correct horse battery staple"
MGR_PW = "manager horse battery staple"


def _hdrs(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


def _seed(session_factory, *, with_second_dept=False):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.add(Admin(username="mgr", password_hash=hash_password(MGR_PW), role="manager", department_scope="Eng"))
        a = Staff(staff_code="EA", name="Eng A", department="Eng", active=True, anonymised=False)
        b = Staff(staff_code="EB", name="Eng B", department="Eng", active=True, anonymised=False)
        s.add_all([a, b])
        c = None
        if with_second_dept:
            c = Staff(staff_code="SC", name="Sales C", department="Sales", active=True, anonymised=False)
            s.add(c)
        s.flush()
        p = Pattern(name="OfficeR", kind="shared", early_window_min=30, grace_min=10, break_min=60, is_default=False)
        s.add(p); s.flush()
        for wd in range(5):
            s.add(PatternDay(pattern_id=p.id, weekday=wd, start_time=time(8, 0), end_time=time(17, 0), overnight=False, enabled=True))
        eff = date(2026, 1, 1)
        for stf in [a, b] + ([c] if c else []):
            s.add(Assignment(staff_id=stf.id, pattern_id=p.id, effective_from=eff, effective_to=None))
        s.commit()
        return {"a": a.id, "b": b.id, "c": (c.id if c else None)}


def _add_att(session_factory, staff_id, typ, ts, flag=None, shift_date=None):
    with session_factory() as s:
        s.add(Attendance(staff_id=staff_id, type=typ, timestamp=ts, method="face", flag=flag, shift_date=shift_date))
        s.commit()


def test_late_arrivals_csv_totals_equal_metrics_api(client: TestClient, session_factory):
    ids = _seed(session_factory)
    # A: 3 late days -> follow-up at default 3; B: 1 late
    for d, flag in [(5, "late"), (6, "late"), (7, "late")]:
        _add_att(session_factory, ids["a"], "in", datetime(2026, 1, d, 8, 20), flag=flag, shift_date=date(2026, 1, d))
    _add_att(session_factory, ids["b"], "in", datetime(2026, 1, 5, 8, 25), flag="late", shift_date=date(2026, 1, 5))
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-01-05", "to": "2026-01-09"}
    js = client.get("/api/admin/metrics/reports/late-arrivals", params=params, headers=hdr)
    assert js.status_code == 200, js.text
    body = js.json()
    csv_r = client.get("/api/admin/metrics/reports/late-arrivals", params=dict(params, format="csv"), headers=hdr)
    assert csv_r.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_r.text)))
    header = rows[0]
    assert header == body["columns"]
    data_rows = [r for r in rows[1:] if r and r[0] != "TOTAL"]
    total_row = [r for r in rows[1:] if r and r[0] == "TOTAL"][0]
    # totals equal on-screen (JSON) numbers
    assert int(total_row[4]) == body["totals"]["late_arrivals"]
    assert int(total_row[5]) == body["totals"]["late_minutes"]
    assert len(data_rows) == body["totals"]["staff"] == len(body["items"])
    # per-row late arrivals/minutes match JSON
    by_id = {str(it["staff_id"]): it for it in body["items"]}
    for r in data_rows:
        it = by_id[r[0]]
        assert int(r[4]) == int(it["late"])
        assert int(r[5]) == int(it["late_minutes"])


def test_attendance_summary_csv_totals_equal_metrics_api(client: TestClient, session_factory):
    ids = _seed(session_factory)
    _add_att(session_factory, ids["a"], "in", datetime(2026, 1, 5, 8, 0), shift_date=date(2026, 1, 5))
    _add_att(session_factory, ids["a"], "out", datetime(2026, 1, 5, 17, 0), shift_date=date(2026, 1, 5))
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-01-05", "to": "2026-01-09"}
    js = client.get("/api/admin/metrics/reports/attendance-summary", params=params, headers=hdr)
    assert js.status_code == 200, js.text
    body = js.json()
    csv_r = client.get("/api/admin/metrics/reports/attendance-summary", params=dict(params, format="csv"), headers=hdr)
    assert csv_r.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_r.text)))
    total_row = [r for r in rows[1:] if r and r[0] == "TOTAL"][0]
    assert int(total_row[4]) == body["totals"]["scheduled_ended"]
    assert int(total_row[5]) == body["totals"]["present"]
    assert int(total_row[6]) == body["totals"]["absent"]
    assert int(total_row[7]) == body["totals"]["late"]
    assert float(total_row[10]) == pytest.approx(body["totals"]["hours_worked"], abs=0.01)
    assert int(total_row[11]) == body["totals"]["missing_clock_outs"]
    # per-row parity with /people metrics API (same engine)
    people = client.get("/api/admin/metrics/people", params=dict(params, sort="late"), headers=hdr).json()["items"]
    by_id = {str(it["staff_id"]): it for it in people}
    data_rows = [r for r in rows[1:] if r and r[0] != "TOTAL"]
    for r in data_rows:
        it = by_id[r[0]]
        assert int(r[5]) == int(it["present"])
        assert int(r[6]) == int(it["absent"])
        assert int(r[7]) == int(it["late"])
        assert float(r[10]) == pytest.approx(float(it["hours_worked"]), abs=0.01)


def test_followup_badge_appears_at_exact_threshold_and_configurable(client: TestClient, session_factory):
    ids = _seed(session_factory)
    # exactly 2 late -> NOT flagged at default (3 in 30)
    for d in (5, 6):
        _add_att(session_factory, ids["a"], "in", datetime(2026, 1, d, 8, 20), flag="late", shift_date=date(2026, 1, d))
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-01-05", "to": "2026-01-09"}
    body = client.get("/api/admin/metrics/people", params=dict(params, sort="late"), headers=hdr).json()
    a = [x for x in body["items"] if x["staff_id"] == ids["a"]][0]
    assert a["late"] == 2
    assert a["needs_followup"] is False
    # add 3rd late -> flagged at exact threshold
    _add_att(session_factory, ids["a"], "in", datetime(2026, 1, 7, 8, 20), flag="late", shift_date=date(2026, 1, 7))
    body2 = client.get("/api/admin/metrics/people", params=dict(params, sort="late"), headers=hdr).json()
    a2 = [x for x in body2["items"] if x["staff_id"] == ids["a"]][0]
    assert a2["late"] == 3
    assert a2["needs_followup"] is True
    assert body2["followup"]["late_count"] == 3
    assert "not an automatic penalty" in body2["followup"]["note"]
    # tighten rule to 2 -> already flagged; relax to 4 -> not flagged
    client.put("/api/admin/settings", json={"followup_late_count": 4}, headers=hdr)
    body3 = client.get("/api/admin/metrics/people", params=dict(params, sort="late"), headers=hdr).json()
    a3 = [x for x in body3["items"] if x["staff_id"] == ids["a"]][0]
    assert body3["followup"]["late_count"] == 4
    assert a3["needs_followup"] is False


def test_followup_settings_validation_and_audit(client: TestClient, session_factory):
    _seed(session_factory)
    hdr = _hdrs(client, "alice", ADMIN_PW)
    # validation
    for bad in ({"followup_late_count": 0}, {"followup_late_count": 500}, {"followup_period_days": 0}, {"followup_period_days": 500}):
        r = client.put("/api/admin/settings", json=bad, headers=hdr)
        assert r.status_code == 422, (bad, r.text)
    # valid change is audited
    r = client.put("/api/admin/settings", json={"followup_late_count": 5, "followup_period_days": 14}, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["followup_late_count"] == 5 and body["followup_period_days"] == 14
    # audit records the changes
    with session_factory() as s:
        acts = [a for a in s.scalars(select(__import__('app.models', fromlist=['AuditLog']).AuditLog)).all()
                if a.action == "update_setting" and a.target in ("followup_late_count", "followup_period_days")]
        assert {a.target for a in acts} == {"followup_late_count", "followup_period_days"}
        assert all("-> 5" in (a.detail or "") or "-> 14" in (a.detail or "") for a in acts)
