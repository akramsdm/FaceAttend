from __future__ import annotations

import json
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.metrics import person_metrics, team_metrics, daily_series, arrival_histogram
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
        p = Pattern(name="OfficeT", kind="shared", early_window_min=30, grace_min=10, break_min=60, is_default=False)
        s.add(p); s.flush()
        for wd in range(5):
            s.add(PatternDay(pattern_id=p.id, weekday=wd, start_time=time(8, 0), end_time=time(17, 0), overnight=False, enabled=True))
        eff = date(2026, 1, 1)
        for stf in [a, b] + ([c] if c else []):
            s.add(Assignment(staff_id=stf.id, pattern_id=p.id, effective_from=eff, effective_to=None))
        s.commit()
        return {"a": a.id, "b": b.id, "c": (c.id if c else None), "pattern": p.id}


def _add_att(session_factory, staff_id, typ, ts, flag=None, shift_date=None, undone=False):
    with session_factory() as s:
        s.add(Attendance(staff_id=staff_id, type=typ, timestamp=ts, method="face", flag=flag,
                         shift_date=shift_date, undone_at=(datetime(2026, 1, 5, 12, 0) if undone else None)))
        s.commit()


class _QueryCounter:
    def __init__(self, engine):
        self.n = 0
        self.engine = engine

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self.n += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._before)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._before)


def test_department_scoping_for_managers(client: TestClient, session_factory):
    ids = _seed(session_factory, with_second_dept=True)
    hdr = _hdrs(client, "mgr", MGR_PW)
    params = {"from": "2026-01-05", "to": "2026-01-09", "department": "Sales"}
    r = client.get("/api/admin/metrics/summary", params=params, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["range"]["total_hours"] == 0.0
    assert body["range"]["late_count"] == 0
    r2 = client.get("/api/admin/metrics/summary", params={"from": "2026-01-05", "to": "2026-01-09"}, headers=hdr)
    assert r2.status_code == 200
    rp = client.get("/api/admin/metrics/people", params={"from": "2026-01-05", "to": "2026-01-09"}, headers=hdr)
    assert {it["staff_id"] for it in rp.json()["items"]} == {ids["a"], ids["b"]}
    r3 = client.get(f"/api/admin/metrics/person/{ids['c']}", params={"from": "2026-01-05", "to": "2026-01-09"}, headers=hdr)
    assert r3.status_code == 403
    r4 = client.get(f"/api/admin/metrics/person/{ids['a']}", params={"from": "2026-01-05", "to": "2026-01-09"}, headers=hdr)
    assert r4.status_code == 200


def test_date_cap_366_days(client: TestClient, session_factory):
    _seed(session_factory)
    hdr = _hdrs(client, "alice", ADMIN_PW)
    r = client.get("/api/admin/metrics/summary", params={"from": "2026-01-01", "to": "2027-01-03"}, headers=hdr)
    assert r.status_code == 422
    r = client.get("/api/admin/metrics/summary", params={"from": "2026-01-01", "to": "2027-01-01"}, headers=hdr)
    assert r.status_code == 200
    r = client.get("/api/admin/metrics/summary", params={"from": "2026-01-10", "to": "2026-01-01"}, headers=hdr)
    assert r.status_code == 422


def test_empty_ranges_return_zeros(client: TestClient, session_factory):
    _seed(session_factory)
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-02-02", "to": "2026-02-06"}
    for path in ["/api/admin/metrics/summary", "/api/admin/metrics/trend", "/api/admin/metrics/breakdown",
                 "/api/admin/metrics/arrivals", "/api/admin/metrics/people"]:
        r = client.get(path, params=(dict(params, **({"by": "department"} if "breakdown" in path else {}))), headers=hdr)
        assert r.status_code == 200, f"{path} {r.text}"
        b = r.json()
        if "summary" in path:
            assert b["range"]["total_hours"] == 0.0
            assert b["range"]["late_count"] == 0
        if "arrivals" in path:
            assert b["items"] == []


def test_numbers_match_app_metrics_on_small_fixture(client: TestClient, session_factory):
    ids = _seed(session_factory)
    _add_att(session_factory, ids["a"], "in", datetime(2026, 1, 5, 8, 20), flag="late", shift_date=date(2026, 1, 5))
    _add_att(session_factory, ids["a"], "out", datetime(2026, 1, 5, 17, 0), shift_date=date(2026, 1, 5))
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-01-05", "to": "2026-01-05"}
    rp = client.get("/api/admin/metrics/people", params=params, headers=hdr)
    assert rp.status_code == 200
    items = {it["staff_id"]: it for it in rp.json()["items"]}
    from app.routers.metrics import _shifts_data_for, _attendance_rows
    from app.models import Staff
    with session_factory() as s:
        people = list(s.scalars(select(Staff).where(Staff.id.in_([ids["a"], ids["b"]])).order_by(Staff.id)).all())
        data = _shifts_data_for(s, [ids["a"], ids["b"]], date(2026, 1, 5), date(2026, 1, 5))
        rows = _attendance_rows(s, [ids["a"], ids["b"]], date(2026, 1, 5), date(2026, 1, 5), "UTC")
    now_local = datetime(2026, 1, 5, 18, 0)
    per = {p.id: person_metrics(p, (date(2026, 1, 5), date(2026, 1, 5)), rows, data, now=now_local) for p in people}
    tm = team_metrics(people, (date(2026, 1, 5), date(2026, 1, 5)), rows, data, now=now_local)
    for sid in [ids["a"], ids["b"]]:
        assert items[sid]["present"] == per[sid]["present"]
        assert items[sid]["absent"] == per[sid]["absent"]
        assert items[sid]["late"] == per[sid]["late"]
        assert items[sid]["hours_worked"] == pytest.approx(per[sid]["hours_worked"], abs=0.01)
        assert items[sid]["on_time_rate"] == pytest.approx(per[sid]["on_time_rate"], abs=0.001)
    rs = client.get("/api/admin/metrics/summary", params=params, headers=hdr).json()
    assert rs["range"]["late_count"] == tm["late"]
    assert rs["range"]["total_hours"] == pytest.approx(tm["hours_worked"], abs=0.01)
    assert rs["range"]["on_time_rate"] == pytest.approx(tm["on_time_rate"], abs=0.001)
    ds = daily_series(people, (date(2026, 1, 5), date(2026, 1, 5)), rows, data, now=now_local)
    rt = client.get("/api/admin/metrics/trend", params=params, headers=hdr).json()
    assert rt["items"][0]["present"] == ds[0]["present"]
    assert rt["items"][0]["late"] == ds[0]["late"]
    assert rt["items"][0]["absent"] == ds[0]["absent"]
    ah = arrival_histogram(people, (date(2026, 1, 5), date(2026, 1, 5)), rows, data)
    ra = client.get("/api/admin/metrics/arrivals", params=params, headers=hdr).json()
    assert sum(i["count"] for i in ra["items"]) == sum(i["count"] for i in ah)


def test_constant_query_count_per_endpoint(client: TestClient, session_factory, engine):
    ids = _seed(session_factory)
    with session_factory() as s:
        p = s.scalar(select(Pattern).where(Pattern.name == "OfficeT"))
        for i in range(5):
            st = Staff(staff_code=f"EX{i}", name=f"Extra {i}", department="Eng", active=True, anonymised=False)
            s.add(st); s.flush()
            s.add(Assignment(staff_id=st.id, pattern_id=p.id, effective_from=date(2026, 1, 1), effective_to=None))
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-01-05", "to": "2026-01-09"}
    endpoints = [
        ("/api/admin/metrics/summary", dict(params)),
        ("/api/admin/metrics/trend", dict(params)),
        ("/api/admin/metrics/breakdown", dict(params, by="department")),
        ("/api/admin/metrics/arrivals", dict(params)),
        ("/api/admin/metrics/people", dict(params)),
        (f"/api/admin/metrics/person/{ids['a']}", dict(params)),
    ]
    baseline = {}
    for path, p in endpoints:
        with _QueryCounter(engine) as qc:
            r = client.get(path, params=p, headers=hdr)
        assert r.status_code == 200, f"{path} {r.text}"
        baseline[path] = qc.n
        assert qc.n <= 22, f"{path} issued {qc.n} queries (expected constant, <=22)"
    # add more staff and confirm counts do not grow (constant w.r.t. staff count)
    with session_factory() as s:
        pat = s.scalar(select(Pattern).where(Pattern.name == "OfficeT"))
        for i in range(10, 20):
            st = Staff(staff_code=f"EX{i}", name=f"Extra {i}", department="Eng", active=True, anonymised=False)
            s.add(st); s.flush()
            s.add(Assignment(staff_id=st.id, pattern_id=pat.id, effective_from=date(2026, 1, 1), effective_to=None))
        s.commit()
    for path, p in endpoints:
        with _QueryCounter(engine) as qc:
            r = client.get(path, params=p, headers=hdr)
        assert r.status_code == 200
        assert qc.n == baseline[path], f"{path} query count grew with staff: {baseline[path]} -> {qc.n}"
