from __future__ import annotations

import csv
import io
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Admin, Assignment, Attendance, Pattern, PatternDay, Staff
from app.security import CSRF_HEADER_NAME, hash_password

ADMIN_PW = "correct horse battery staple"
MGR_PW = "manager horse battery staple"


def _hdrs(client: TestClient, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


def _set_setting(session_factory, key, value):
    # upsert via DB directly to avoid CSRF/admin dependency in helpers
    from app.models import Setting
    with session_factory() as s:
        row = s.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=str(value)); s.add(row)
        else:
            row.value = str(value)
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


def _mk_staff(s: Session, code, name, dept):
    st = Staff(staff_code=code, name=name, department=dept, active=True, anonymised=False)
    s.add(st); s.flush(); return st


def _mk_pattern(s: Session, name, st, et, overnight=False, grace=10, break_min=60):
    p = Pattern(name=name, kind="shared", early_window_min=30, grace_min=grace, break_min=break_min, is_default=False)
    s.add(p); s.flush()
    for wd in range(7):
        s.add(PatternDay(pattern_id=p.id, weekday=wd, start_time=st, end_time=et, overnight=overnight, enabled=True))
    return p


def _assign(s: Session, staff_id, pattern_id, eff=date(2026, 1, 1)):
    s.add(Assignment(staff_id=staff_id, pattern_id=pattern_id, effective_from=eff, effective_to=None))


def _att(s: Session, staff_id, typ, ts, *, shift_id=None, shift_date=None, flag=None, method="face",
         edited_by=None, edit_reason=None, approved_by=None, undone_at=None):
    s.add(Attendance(staff_id=staff_id, type=typ, timestamp=ts, method=method, shift_id=shift_id,
                     shift_date=shift_date, flag=flag, edited_by=edited_by, edit_reason=edit_reason,
                     approved_by=approved_by, undone_at=undone_at))


def _seed_basic(session_factory, with_second_dept=False):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.add(Admin(username="mgr", password_hash=hash_password(MGR_PW), role="manager", department_scope="Eng"))
        a = _mk_staff(s, "EA", "Eng A", "Eng")
        b = _mk_staff(s, "EB", "Eng B", "Eng")
        c = _mk_staff(s, "SC", "Sales C", "Sales") if with_second_dept else None
        p = _mk_pattern(s, "Day", time(8, 0), time(17, 0), overnight=False, grace=10, break_min=60)
        for stf in [a, b] + ([c] if c else []):
            _assign(s, stf.id, p.id)
        s.commit()
        return {"a": a.id, "b": b.id, "c": (c.id if c else None), "p": p.id}


def _sessions(client, hdr, **params):
    return client.get("/api/admin/attendance/sessions", params=params, headers=hdr)


def _all_sessions(body):
    out = []
    for d in body.get("days", []):
        for sess in d.get("sessions", []):
            out.append(sess)
    return out


# --- backward compat: old endpoints still work ---

def test_old_attendance_endpoints_still_work(client: TestClient, session_factory):
    _seed_basic(session_factory)
    hdr = _hdrs(client, "alice", ADMIN_PW)
    assert client.get("/api/admin/attendance", headers=hdr).status_code == 200
    assert client.get("/api/admin/attendance/export.csv", headers=hdr).status_code == 200
    # dashboard/report require admin (kept) - just ensure no 404
    r = client.get("/api/admin/attendance/dashboard", headers=hdr)
    assert r.status_code != 404


# --- exact boundary minutes (day shift, defaults early_arrival=15, late_clockout=15, grace=10) ---
# shift 08:00-17:00, grace 10 => on_time window ends 08:10
# early_arrival 15 => <= 07:45 early, (07:45,08:10] on_time, >08:10 late
# late_clockout 15 => <17:00 early_departure, [17:00,17:15) on_time, >=17:15 late_clock_out

@pytest.mark.parametrize("in_minute,exp_status,exp_mins", [
    (45, "early", -15),      # 07:45 exactly at early threshold -> early
    (46, "on_time", -14),    # 07:46 just inside
    (60, "on_time", 0),      # 08:00
    (70, "on_time", 10),     # 08:10 grace boundary -> on_time
    (71, "late", 11),        # 08:11
])
def test_arrival_boundaries(client, session_factory, in_minute, exp_status, exp_mins):
    ids = _seed_basic(session_factory)
    sd = date(2026, 1, 5)  # Mon
    with session_factory() as s:
        hh = 7 if in_minute < 60 else 8
        mm = in_minute if in_minute < 60 else in_minute - 60
        _att(s, ids["a"], "in", datetime(2026, 1, 5, hh, mm), shift_id=ids["p"], shift_date=sd)
        _att(s, ids["a"], "out", datetime(2026, 1, 5, 17, 0), shift_id=ids["p"], shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    body = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json()
    recs = _all_sessions(body)
    assert len(recs) == 1
    assert recs[0]["arrival_status"] == exp_status
    assert recs[0]["minutes_vs_start"] == exp_mins


@pytest.mark.parametrize("out_h,out_m,exp_status,exp_mins", [
    (16, 59, "early_departure", -1),
    (17, 0, "on_time", 0),
    (17, 14, "on_time", 14),          # just before late_clockout threshold
    (17, 15, "late_clock_out", 15),   # at threshold
    (17, 16, "late_clock_out", 16),
])
def test_departure_boundaries(client, session_factory, out_h, out_m, exp_status, exp_mins):
    ids = _seed_basic(session_factory)
    sd = date(2026, 1, 5)
    with session_factory() as s:
        _att(s, ids["a"], "in", datetime(2026, 1, 5, 8, 0), shift_id=ids["p"], shift_date=sd)
        _att(s, ids["a"], "out", datetime(2026, 1, 5, out_h, out_m), shift_id=ids["p"], shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    body = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json()
    rec = _all_sessions(body)[0]
    assert rec["departure_status"] == exp_status
    assert rec["minutes_vs_end"] == exp_mins


# --- night shift cases (spec) ---

def test_night_shift_arrival_and_late_clockout(client, session_factory):
    # late_clockout threshold 10 per spec example
    _set_setting(session_factory, "late_clockout_minutes", 10)
    _set_setting(session_factory, "early_arrival_minutes", 15)
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        st = _mk_staff(s, "NA", "Night A", "Eng")
        p = _mk_pattern(s, "Night", time(22, 0), time(6, 0), overnight=True, grace=10, break_min=30)
        _assign(s, st.id, p.id)
        sd = date(2026, 1, 5)
        # arrival before 22:00 -> early (21:40, 20 min early, threshold 15)
        _att(s, st.id, "in", datetime(2026, 1, 5, 21, 40), shift_id=p.id, shift_date=sd)
        # 06:10 vs 06:00 end, threshold 10 => late_clock_out at >=10
        _att(s, st.id, "out", datetime(2026, 1, 6, 6, 10), shift_id=p.id, shift_date=sd)
        s.commit()
        sid = st.id
    hdr = _hdrs(client, "alice", ADMIN_PW)
    body = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json()
    recs = _all_sessions(body)
    assert len(recs) == 1
    r = recs[0]
    assert r["arrival_status"] == "early"
    assert r["minutes_vs_start"] == -20
    assert r["departure_status"] == "late_clock_out"  # 06:10, +10, threshold 10
    assert r["minutes_vs_end"] == 10


# --- no-shift -> null statuses and excluded from arrival/departure filters ---

def test_no_shift_null_and_excluded(client, session_factory):
    with session_factory() as s:
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        st = _mk_staff(s, "NS", "No Shift", "Eng")
        # no assignment
        sd = date(2026, 1, 5)
        _att(s, st.id, "in", datetime(2026, 1, 5, 7, 0), shift_date=sd)
        _att(s, st.id, "out", datetime(2026, 1, 5, 18, 0), shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    body = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json()
    r = _all_sessions(body)[0]
    assert r["arrival_status"] is None and r["departure_status"] is None
    # excluded from arrival/departure filters
    b2 = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05", "arrival": "early"}).json()
    assert _all_sessions(b2) == []
    b3 = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05", "departure": "late_clock_out"}).json()
    assert _all_sessions(b3) == []


# --- OR inside group, AND across groups; min-minutes ---

def test_or_within_and_across_groups_and_min_minutes(client, session_factory):
    ids = _seed_basic(session_factory)
    sd = date(2026, 1, 5)
    with session_factory() as s:
        # A: late arrival (+30), early departure (-60) -> late AND early_departure
        _att(s, ids["a"], "in", datetime(2026, 1, 5, 8, 30), shift_id=ids["p"], shift_date=sd)
        _att(s, ids["a"], "out", datetime(2026, 1, 5, 16, 0), shift_id=ids["p"], shift_date=sd)
        # B: on_time arrival, late clock out (+30)
        _att(s, ids["b"], "in", datetime(2026, 1, 5, 8, 0), shift_id=ids["p"], shift_date=sd)
        _att(s, ids["b"], "out", datetime(2026, 1, 5, 17, 30), shift_id=ids["p"], shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    base = {"from": "2026-01-05", "to": "2026-01-05"}
    # OR within arrival: early OR late -> matches A (late) but not B? B on_time
    b = _sessions(client, hdr, **base, arrival="early,late").json()
    assert [r["staff_id"] for r in _all_sessions(b)] == [ids["a"]]
    # AND across: arrival late AND departure early_departure -> only A
    b = _sessions(client, hdr, **base, arrival="late", departure="early_departure").json()
    assert [r["staff_id"] for r in _all_sessions(b)] == [ids["a"]]
    # min minutes: arrival late >=20 -> A (+30) yes
    b = _sessions(client, hdr, **base, arrival="late", arrival_min_minutes=20).json()
    assert [r["staff_id"] for r in _all_sessions(b)] == [ids["a"]]
    b = _sessions(client, hdr, **base, arrival="late", arrival_min_minutes=31).json()
    assert _all_sessions(b) == []
    # departure min: early_departure >= 30 -> A (-60 => 60 min early) yes
    b = _sessions(client, hdr, **base, departure="early_departure", departure_min_minutes=30).json()
    assert [r["staff_id"] for r in _all_sessions(b)] == [ids["a"]]


# --- undone ignored; orphan/open sessions; edited/manual/approved flags ---

def test_undone_orphan_open_and_record_tags(client, session_factory):
    ids = _seed_basic(session_factory)
    sd = date(2026, 1, 5)
    with session_factory() as s:
        # undone in should be ignored -> leaves orphan out? create undone in + real out
        _att(s, ids["a"], "in", datetime(2026, 1, 5, 8, 0), shift_id=ids["p"], shift_date=sd, undone_at=datetime(2026, 1, 5, 8, 1))
        _att(s, ids["a"], "out", datetime(2026, 1, 5, 17, 0), shift_id=ids["p"], shift_date=sd)  # orphan
        # B: open/in_progress
        _att(s, ids["b"], "in", datetime(2026, 1, 5, 8, 5), shift_id=ids["p"], shift_date=sd, method="manual",
             edited_by=1, edit_reason="Reception sign-in")
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    body = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json()
    by = {r["staff_id"]: r for r in _all_sessions(body)}
    assert "orphan_out" in by[ids["a"]]["state_tags"]
    assert by[ids["a"]]["clock_in"] is None and by[ids["a"]]["clock_out"] is not None
    assert "in_progress" in by[ids["b"]]["state_tags"]
    assert by[ids["b"]]["edited"] is True and by[ids["b"]]["reason"] == "Reception sign-in"
    assert "manual" in by[ids["b"]]["record_tags"] and "edited" in by[ids["b"]]["record_tags"]
    # session filter
    b = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05", "session": "orphan_out"}).json()
    assert [r["staff_id"] for r in _all_sessions(b)] == [ids["a"]]
    b = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05", "record": "manual"}).json()
    assert [r["staff_id"] for r in _all_sessions(b)] == [ids["b"]]


# --- threshold change affects results without migration ---

def test_threshold_change_read_time_no_migration(client, session_factory):
    ids = _seed_basic(session_factory)
    sd = date(2026, 1, 5)
    with session_factory() as s:
        _att(s, ids["a"], "in", datetime(2026, 1, 5, 7, 50), shift_id=ids["p"], shift_date=sd)  # -10 min
        _att(s, ids["a"], "out", datetime(2026, 1, 5, 17, 0), shift_id=ids["p"], shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    r1 = _all_sessions(_sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json())[0]
    assert r1["arrival_status"] == "on_time"  # -10, early threshold default 15 => on_time (only <=-15 early)
    _set_setting(session_factory, "early_arrival_minutes", 5)
    r2 = _all_sessions(_sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json())[0]
    assert r2["arrival_status"] == "early"  # -10 <= -5
    # same underlying attendance row unchanged
    with session_factory() as s:
        a = s.query(Attendance).filter(Attendance.staff_id == ids["a"], Attendance.type == "in").one()
        assert a.flag is None  # never stored by us


# --- manager dept scoping ---

def test_manager_dept_scoping(client, session_factory):
    ids = _seed_basic(session_factory, with_second_dept=True)
    sd = date(2026, 1, 5)
    with session_factory() as s:
        _att(s, ids["a"], "in", datetime(2026, 1, 5, 8, 0), shift_id=ids["p"], shift_date=sd)
        _att(s, ids["c"], "in", datetime(2026, 1, 5, 8, 0), shift_id=ids["p"], shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "mgr", MGR_PW)
    body = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05"}).json()
    assert {r["staff_id"] for r in _all_sessions(body)} == {ids["a"], ids["b"]} - {ids["b"]} | {ids["a"]}
    # explicit other dept blocked/empty
    body = _sessions(client, hdr, **{"from": "2026-01-05", "to": "2026-01-05", "department": "Sales"}).json()
    assert _all_sessions(body) == []


# --- constant query count ---

def test_sessions_query_count_constant(client, session_factory, engine):
    ids = _seed_basic(session_factory)
    sd = date(2026, 1, 5)
    with session_factory() as s:
        _att(s, ids["a"], "in", datetime(2026, 1, 5, 8, 0), shift_id=ids["p"], shift_date=sd)
        _att(s, ids["a"], "out", datetime(2026, 1, 5, 17, 0), shift_id=ids["p"], shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-01-05", "to": "2026-01-05"}
    with _QueryCounter(engine) as c1:
        r1 = client.get("/api/admin/attendance/sessions", params=params, headers=hdr)
    assert r1.status_code == 200
    # add more staff/sessions
    with session_factory() as s:
        for i in range(6):
            st = _mk_staff(s, f"X{i}", f"X{i}", "Eng")
            _assign(s, st.id, ids["p"])
            s.flush()
            _att(s, st.id, "in", datetime(2026, 1, 5, 8, 0), shift_id=ids["p"], shift_date=sd)
            _att(s, st.id, "out", datetime(2026, 1, 5, 17, 0), shift_id=ids["p"], shift_date=sd)
        s.commit()
    with _QueryCounter(engine) as c2:
        r2 = client.get("/api/admin/attendance/sessions", params=params, headers=hdr)
    assert r2.status_code == 200
    assert len(_all_sessions(r2.json())) > len(_all_sessions(r1.json()))
    assert c2.n == c1.n, f"query count grew: {c1.n} -> {c2.n}"


# --- CSV equals JSON ---

def test_sessions_csv_equals_json(client, session_factory):
    ids = _seed_basic(session_factory)
    sd = date(2026, 1, 5)
    with session_factory() as s:
        _att(s, ids["a"], "in", datetime(2026, 1, 5, 8, 20), shift_id=ids["p"], shift_date=sd)
        _att(s, ids["a"], "out", datetime(2026, 1, 5, 17, 30), shift_id=ids["p"], shift_date=sd)
        s.commit()
    hdr = _hdrs(client, "alice", ADMIN_PW)
    params = {"from": "2026-01-05", "to": "2026-01-05"}
    body = _sessions(client, hdr, **params).json()
    csv_r = client.get("/api/admin/attendance/sessions.csv", params=params, headers=hdr)
    assert csv_r.status_code == 200
    assert "text/csv" in csv_r.headers["content-type"]
    rows = list(csv.reader(io.StringIO(csv_r.text)))
    header = rows[0]
    data = [r for r in rows[1:] if r]
    recs = _all_sessions(body)
    assert len(data) == len(recs)
    # compare key columns
    by_staff = {str(r["staff_id"]): r for r in recs}
    for row in data:
        m = dict(zip(header, row))
        jr = by_staff[m["staff_id"]]
        assert m["arrival_status"] == (jr["arrival_status"] or "")
        assert m["departure_status"] == (jr["departure_status"] or "")
        assert m["minutes_vs_start"] == (str(jr["minutes_vs_start"]) if jr["minutes_vs_start"] is not None else "")
        assert m["minutes_vs_end"] == (str(jr["minutes_vs_end"]) if jr["minutes_vs_end"] is not None else "")
        assert m["duration_minutes"] == (str(jr["duration_minutes"]) if jr["duration_minutes"] is not None else "")
        assert m["shift_date"] == jr["shift_date"]
