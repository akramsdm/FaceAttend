from __future__ import annotations

import csv
import io
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Assignment, Attendance, Pattern, Staff
from app.routers.shifts import seed_default_patterns


def add_staff(session_factory, code="S001", name="Ada", dept="Eng") -> int:
    with session_factory() as s:
        st = Staff(staff_code=code, name=name, department=dept)
        s.add(st); s.commit(); return st.id


def assign_office(session_factory, staff_id: int):
    with session_factory() as s:
        seed_default_patterns(s)
        office = s.scalar(select(Pattern).where(Pattern.name == "Office hours", Pattern.is_default.is_(True)))
        s.add(Assignment(staff_id=staff_id, pattern_id=office.id,
                         effective_from=datetime(2026, 1, 1).date(), effective_to=None))
        s.commit()


def add_att(session_factory, staff_id, typ, ts, **kw):
    with session_factory() as s:
        r = Attendance(staff_id=staff_id, type=typ, timestamp=ts, method=kw.pop("method", "face"), **kw)
        s.add(r); s.commit(); return r.id


# --- night shift hours across midnight (group by shift_date) ---
def test_night_shift_hours_across_midnight_break_subtraction(authenticated_client: TestClient, session_factory):
    sid = add_staff(session_factory)
    # assign Night pattern (22:00-06:00 overnight, break 30)
    with session_factory() as s:
        seed_default_patterns(s)
        night = s.scalar(select(Pattern).where(Pattern.name == "Night", Pattern.is_default.is_(True)))
        s.add(Assignment(staff_id=sid, pattern_id=night.id, effective_from=datetime(2026,1,1).date(), effective_to=None))
        s.commit()
    # shift starts 2026-01-05 22:00Z, ends 2026-01-06 06:00Z => 8h gross, minus 30m break = 7.5h
    add_att(session_factory, sid, "in", datetime(2026, 1, 5, 22, 0, 0), shift_date=datetime(2026,1,5).date())
    add_att(session_factory, sid, "out", datetime(2026, 1, 6, 6, 0, 0), shift_date=datetime(2026,1,5).date())
    r = authenticated_client.get("/api/admin/attendance/report/monthly.csv", params={"year": 2026, "month": 1})
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    row = rows[0]
    # grouped by shift_date (start day) - not split across midnight
    assert float(row["hours_worked"]) == pytest.approx(7.5, abs=0.01)


# --- late at exact boundary (shift start + grace) ---
def test_late_at_exact_boundary_shift(authenticated_client: TestClient, session_factory):
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)  # 08:00 start, grace 10 => late after 08:10:00
    # at boundary 08:10:00: not late
    add_att(session_factory, sid, "in", datetime(2026, 1, 5, 8, 10, 0), shift_date=datetime(2026,1,5).date())
    add_att(session_factory, sid, "out", datetime(2026, 1, 5, 17, 0, 0), shift_date=datetime(2026,1,5).date())
    r = authenticated_client.get("/api/admin/attendance/report/monthly.csv", params={"year": 2026, "month": 1})
    row = list(csv.DictReader(io.StringIO(r.text)))[0]
    assert row["late_arrivals"] == "0"
    # after boundary: late
    add_att(session_factory, sid, "in", datetime(2026, 1, 6, 8, 10, 1), shift_date=datetime(2026,1,6).date(), flag="late")
    r = authenticated_client.get("/api/admin/attendance/report/monthly.csv", params={"year": 2026, "month": 1})
    row = list(csv.DictReader(io.StringIO(r.text)))[0]
    assert int(row["late_arrivals"]) >= 1


# --- absent only when a shift was assigned ---
def test_absent_only_when_shift_assigned(authenticated_client: TestClient, session_factory):
    assigned = add_staff(session_factory, "A1", "Assigned", "Eng")
    unassigned = add_staff(session_factory, "B1", "NoShift", "Eng")
    assign_office(session_factory, assigned)
    # do NOT assign unassigned; both have no attendance on a weekday (e.g. 2026-01-05 Mon)
    r = authenticated_client.get("/api/admin/attendance/dashboard")
    assert r.status_code == 200
    d = r.json()
    assert "absent_today" in d and "present_today" in d and "missing_clock_out" in d


def test_absent_only_when_shift_assigned_explicit(authenticated_client: TestClient, session_factory, monkeypatch):
    # freeze dashboard "now" to 2026-01-05 12:00 UTC (a Monday, Office hours assigned)
    import app.routers.attendance as att_mod
    from datetime import datetime as dt
    monkeypatch.setattr(att_mod, "_utcnow_naive", lambda: dt(2026, 1, 5, 12, 0, 0), raising=False)
    assigned = add_staff(session_factory, "A2", "Assigned", "Eng")
    unassigned = add_staff(session_factory, "B2", "NoShift", "Eng")
    assign_office(session_factory, assigned)
    # neither clocked in
    r = authenticated_client.get("/api/admin/attendance/dashboard")
    assert r.status_code == 200
    d = r.json()
    # only `assigned` had a shift that day => absent 1 (not 2)
    assert d["absent_today"] == 1
    assert d["present_today"] == 0


# --- flags + missing clock-out in CSV, excluded from totals ---
def test_monthly_csv_flags_and_missing_excluded(authenticated_client: TestClient, session_factory):
    sid = add_staff(session_factory)
    assign_office(session_factory, sid)
    # early departure flagged out
    add_att(session_factory, sid, "in", datetime(2026, 1, 5, 8, 0, 0), shift_date=datetime(2026,1,5).date())
    add_att(session_factory, sid, "out", datetime(2026, 1, 5, 15, 0, 0), shift_date=datetime(2026,1,5).date(), flag="early_departure")
    # missing clock-out: in with no valid out counted; auto out flagged missing_clock_out excluded
    add_att(session_factory, sid, "in", datetime(2026, 1, 6, 8, 0, 0), shift_date=datetime(2026,1,6).date())
    add_att(session_factory, sid, "out", datetime(2026, 1, 6, 21, 0, 0), shift_date=datetime(2026,1,6).date(),
            flag="missing_clock_out", method="manual")
    r = authenticated_client.get("/api/admin/attendance/report/monthly.csv", params={"year": 2026, "month": 1})
    rows = list(csv.DictReader(io.StringIO(r.text)))
    row = rows[0]
    # columns updated
    for col in ["early_departures", "overtime_count", "missing_clock_outs", "hours_worked"]:
        assert col in row
    assert int(row["early_departures"]) >= 1
    assert int(row["missing_clock_outs"]) >= 1
    # hours exclude missing clock-out (only day 1's 7h minus break 60m = 6h counted? Office break 60)
    # day 1: 15:00-08:00 = 7h, minus 60m break = 6.0h
    assert float(row["hours_worked"]) == pytest.approx(6.0, abs=0.05)


def test_export_csv_has_shift_columns(authenticated_client: TestClient, session_factory):
    sid = add_staff(session_factory)
    add_att(session_factory, sid, "in", datetime(2026, 1, 5, 8, 0, 0), shift_date=datetime(2026,1,5).date(), flag="late")
    r = authenticated_client.get("/api/admin/attendance/export.csv", params={"start": "2026-01-05", "end": "2026-01-05"})
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))
    header = rows[0]
    assert "shift_date" in header and "flag" in header
