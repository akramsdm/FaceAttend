from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.metrics import (
    arrival_histogram,
    daily_series,
    lateness_by_weekday,
    person_metrics,
    scheduled_day,
    team_metrics,
    top_n,
)
from app.shifts import ResolvedShift


def shift(start, end, *, overnight=False, grace=10, break_min=0, shift_date=None):
    sd = shift_date or start.date()
    return ResolvedShift(start=start, end=end, overnight=overnight, early_window=30, grace=grace, break_min=break_min, source="shared", shift_date=sd)


def row(staff_id, typ, ts, flag=None, shift_date=None, undone=False):
    return {"staff_id": staff_id, "type": typ, "timestamp": ts, "flag": flag, "shift_date": shift_date, "undone_at": (object() if undone else None)}


DAY = date(2026, 1, 5)  # Mon
PERSON = {"id": 1, "name": "A", "department": "Eng", "shift": "Day"}


def resolve_day(*, overnight=False, start_h=8, start_m=0, end_h=17, end_m=0, grace=10, break_min=0):
    s = datetime(DAY.year, DAY.month, DAY.day, start_h, start_m)
    end_day = DAY + timedelta(days=1) if overnight else DAY
    e = datetime(end_day.year, end_day.month, end_day.day, end_h, end_m)
    sh = shift(s, e, overnight=overnight, grace=grace, break_min=break_min, shift_date=DAY)
    return lambda pid, d: sh if d == DAY else None


# --- table-driven cases requested ---


def test_night_shift_across_midnight_groups_by_shift_date_and_break():
    # Night 22:00 (DAY) -> 06:00 (DAY+1), break 30, grace 10. now after end.
    s = datetime(2026, 1, 5, 22, 0)
    e = datetime(2026, 1, 6, 6, 0)
    sh = shift(s, e, overnight=True, grace=10, break_min=30, shift_date=date(2026, 1, 5))
    rows = [
        row(1, "in", datetime(2026, 1, 5, 22, 0), shift_date=date(2026, 1, 5)),
        row(1, "out", datetime(2026, 1, 6, 6, 0), shift_date=date(2026, 1, 5)),  # out after midnight, same shift_date
    ]
    m = person_metrics(PERSON, (date(2026, 1, 5), date(2026, 1, 5)), rows, resolve=lambda pid, d: sh if d == date(2026, 1, 5) else None, now=datetime(2026, 1, 6, 7, 0))
    assert m["present"] == 1
    assert m["absent"] == 0
    # 8h span minus 30m break = 7.5h
    assert m["hours_worked"] == pytest.approx(7.5, abs=0.01)
    assert m["missing_clock_outs"] == 0


def test_break_subtraction_on_completed_pair():
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0), break_min=60)
    rows = [
        row(1, "in", datetime(2026, 1, 5, 8, 0), shift_date=DAY),
        row(1, "out", datetime(2026, 1, 5, 17, 0), shift_date=DAY),
    ]
    m = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 18, 0))
    # 9h - 60m = 8h
    assert m["hours_worked"] == pytest.approx(8.0, abs=0.01)


@pytest.mark.parametrize("minute,expect_late,expect_late_min", [
    (10, False, 0),   # exactly at start+grace => not late
    (11, True, 11),   # 1 minute after grace (minutes after START)
])
def test_late_at_exact_boundary_uses_stored_flag_and_minutes(minute, expect_late, expect_late_min):
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0), grace=10)
    ts = datetime(2026, 1, 5, 8, minute)
    flag = "late" if expect_late else None
    rows = [row(1, "in", ts, flag=flag, shift_date=DAY)]
    m = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 18, 0))
    assert m["late"] == (1 if expect_late else 0)
    assert m["late_minutes"] == (expect_late_min if expect_late else 0)
    assert m["on_time"] == (0 if expect_late else 1)


@pytest.mark.parametrize("now_dt,expect_absent,expect_sched_ended", [
    (datetime(2026, 1, 5, 16, 0), 0, 0),  # in-progress (before shift end) => not absent
    (datetime(2026, 1, 4, 12, 0), 0, 0),  # future relative? day is DAY but now before end => in-progress
    (datetime(2026, 1, 5, 17, 1), 1, 1),  # after shift end => absent
])
def test_absent_not_counted_for_future_or_in_progress(now_dt, expect_absent, expect_sched_ended):
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0))
    rows = []  # no clock-in
    m = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=now_dt)
    assert m["absent"] == expect_absent
    assert m["scheduled_ended"] == expect_sched_ended
    assert m["present"] == 0


def test_day_off_excluded_not_scheduled_not_absent():
    # no resolved shift (resolve returns None) => day off / not scheduled
    rows = []
    m = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: None, now=datetime(2026, 1, 5, 23, 0))
    assert m["scheduled_ended"] == 0
    assert m["absent"] == 0
    assert m["present"] == 0
    assert scheduled_day(1, DAY, resolver=lambda pid, d: None) is False
    # explicit day-off exception excludes even if a shift would resolve
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0))
    ex = [{"staff_id": 1, "date": DAY, "kind": "day_off"}]
    assert scheduled_day(1, DAY, resolver=lambda pid, d: sh, exceptions=ex) is False
    m2 = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: sh, exceptions=ex, now=datetime(2026, 1, 5, 23, 0))
    assert m2["scheduled_ended"] == 0 and m2["absent"] == 0


def test_missing_clock_out_excluded_from_hours_but_counted():
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0), break_min=60)
    rows = [
        row(1, "in", datetime(2026, 1, 5, 8, 0), shift_date=DAY),
        row(1, "out", datetime(2026, 1, 5, 22, 0), flag="missing_clock_out", shift_date=DAY),
    ]
    m = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 23, 0))
    assert m["hours_worked"] == pytest.approx(0.0, abs=0.001)
    assert m["missing_clock_outs"] == 1
    # still counts as present (had a real clock-in)
    assert m["present"] == 1


def test_undone_events_ignored():
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0))
    rows = [
        row(1, "in", datetime(2026, 1, 5, 8, 5), shift_date=DAY, undone=True),
        row(1, "out", datetime(2026, 1, 5, 17, 0), shift_date=DAY, undone=True),
    ]
    m = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 23, 0))
    # undone clock-in ignored => absent (shift ended, no valid in)
    assert m["present"] == 0
    assert m["absent"] == 1
    assert m["hours_worked"] == pytest.approx(0.0)
    assert m["clock_ins"] == 0


def test_empty_ranges_return_zeros_not_errors():
    m = person_metrics(PERSON, (DAY, DAY), [], resolve=lambda pid, d: None, now=datetime(2026, 1, 5, 23, 0))
    assert m["present"] == 0 and m["absent"] == 0 and m["late"] == 0
    assert m["hours_worked"] == 0.0 and m["on_time_rate"] == 0.0 and m["attendance_rate"] == 0.0
    assert m["missing_clock_outs"] == 0 and m["average_arrival_offset_minutes"] == 0.0
    t = team_metrics([PERSON], (DAY, DAY), [], resolve=lambda pid, d: None)
    assert t["headcount"] == 1 and t["present"] == 0 and t["hours_worked"] == 0.0
    assert daily_series([PERSON], (DAY, DAY), [], resolve=lambda pid, d: None) != []
    assert arrival_histogram([PERSON], (DAY, DAY), [], resolve=lambda pid, d: None) == []
    assert lateness_by_weekday([PERSON], (DAY, DAY), [], resolve=lambda pid, d: None)  # 7 buckets
    assert top_n([], "late") == []


# --- additional required functions / behaviors ---


def test_team_grouping_department_shift_weekday_and_top_n():
    p1 = {"id": 1, "name": "A", "department": "Eng", "shift": "Day"}
    p2 = {"id": 2, "name": "B", "department": "Sales", "shift": "Day"}
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0), grace=10)
    rows = [
        row(1, "in", datetime(2026, 1, 5, 8, 20), flag="late", shift_date=DAY),
        row(2, "in", datetime(2026, 1, 5, 8, 0), shift_date=DAY),
    ]
    now = datetime(2026, 1, 5, 18, 0)
    by_dept = team_metrics([p1, p2], (DAY, DAY), rows, resolve=lambda pid, d: sh, now=now, group_by="department")
    assert {g["group"] for g in by_dept} == {"Eng", "Sales"}
    by_shift = team_metrics([p1, p2], (DAY, DAY), rows, resolve=lambda pid, d: sh, now=now, group_by="shift")
    assert len(by_shift) == 1 and by_shift[0]["group"] == "Day"
    by_wd = lateness_by_weekday([p1, p2], (DAY, DAY), rows, resolve=lambda pid, d: sh)
    mon = [b for b in by_wd if b["weekday"] == 0][0]  # 2026-01-05 is Mon
    assert mon["clock_ins"] == 2 and mon["late"] == 1
    per = [person_metrics(p, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=now) for p in (p1, p2)]
    assert top_n(per, "late", 1)[0]["staff_id"] == 1


def test_arrival_histogram_and_average_offset():
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0))
    rows = [
        row(1, "in", datetime(2026, 1, 5, 7, 50), shift_date=DAY),  # -10 min
        row(2, "in", datetime(2026, 1, 5, 8, 5), shift_date=DAY),   # +5 min
    ]
    p1 = {"id": 1}; p2 = {"id": 2}
    hist = arrival_histogram([p1, p2], (DAY, DAY), rows, resolve=lambda pid, d: sh, bucket_minutes=15)
    assert sum(b["count"] for b in hist) == 2
    m = person_metrics(p1, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 18, 0))
    assert m["average_arrival_offset_minutes"] == pytest.approx(-10.0, abs=0.01)


def test_early_departure_and_overtime_flags_minutes():
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0))
    rows = [
        row(1, "in", datetime(2026, 1, 5, 8, 0), shift_date=DAY),
        row(1, "out", datetime(2026, 1, 5, 16, 30), flag="early_departure", shift_date=DAY),
    ]
    m = person_metrics(PERSON, (DAY, DAY), rows, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 18, 0))
    assert m["early_departures"] == 1
    # overtime
    rows2 = [row(1, "in", datetime(2026, 1, 5, 8, 0), shift_date=DAY), row(1, "out", datetime(2026, 1, 5, 18, 0), flag="overtime", shift_date=DAY)]
    m2 = person_metrics(PERSON, (DAY, DAY), rows2, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 19, 0))
    assert m2["overtime"] == 1


def test_daily_series_includes_day():
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 17, 0))
    rows = [row(1, "in", datetime(2026, 1, 5, 8, 0), shift_date=DAY)]
    ser = daily_series([PERSON], (DAY, DAY), rows, resolve=lambda pid, d: sh, now=datetime(2026, 1, 5, 18, 0))
    assert len(ser) == 1 and ser[0]["date"] == DAY and ser[0]["present"] == 1
