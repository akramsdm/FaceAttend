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


# ------------------------------- insights (2D) --------------------------

from app.metrics import compute_insights, late_arrival_hours, _prev_equal_period


def _team(person_list, rng, rows, resolve, now):
    return team_metrics(person_list, rng, rows, resolve=resolve, now=now)


def test_insight_late_vs_previous_percent_and_min_sample_guard():
    sh = shift(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 9, 17, 0), grace=10)
    # spread a resolver over a week (Mon-Fri)
    def res(pid, d):
        s = datetime(d.year, d.month, d.day, 8, 0)
        e = datetime(d.year, d.month, d.day, 17, 0)
        return ResolvedShift(start=s, end=e, overnight=False, early_window=30, grace=10, break_min=0, source="shared", shift_date=d)
    people = [{"id": i, "name": f"P{i}", "department": "Eng"} for i in range(1, 13)]
    # current week: 10 late arrivals (>=10 events total when combined appropriately)
    rows_now = [row(i, "in", datetime(2026, 1, 6, 8, 20), flag="late", shift_date=date(2026, 1, 6)) for i in range(1, 11)]
    # previous equal week: 5 late
    rows_prev = [row(i, "in", datetime(2025, 12, 30, 8, 20), flag="late", shift_date=date(2025, 12, 30)) for i in range(1, 6)]
    now = datetime(2026, 1, 10, 0, 0)
    cur = (date(2026, 1, 5), date(2026, 1, 9)); prev = (date(2025, 12, 29), date(2026, 1, 2))
    tn = _team(people, cur, rows_now, res, now); tp = _team(people, prev, rows_prev, res, now)
    ins = compute_insights(start=cur[0], end=cur[1], department=None, team_now=tn, team_prev=tp,
                           weekday_now=lateness_by_weekday(people, cur, rows_now, resolve=res),
                           late_hours=late_arrival_hours(people, cur, rows_now, resolve=res),
                           people_now=[person_metrics(p, cur, rows_now, resolve=res, now=now) for p in people],
                           dept_groups=team_metrics(people, cur, rows_now, resolve=res, now=now, group_by="department"),
                           recognition={"attempts": 0, "matched": 0, "rejected": 0, "success_rate": 0.0, "backlog": 0})
    m = [x for x in ins if x["id"] == "late_vs_previous"]
    assert m, ins
    assert m[0]["numbers"]["current_late"] == 10 and m[0]["numbers"]["previous_late"] == 5
    assert m[0]["numbers"]["percent_change"] == pytest.approx(100.0)
    assert "flag=late" in m[0]["link"]


def test_insight_late_vs_previous_hidden_below_min_sample():
    def res(pid, d):
        s = datetime(d.year, d.month, d.day, 8, 0); e = datetime(d.year, d.month, d.day, 17, 0)
        return ResolvedShift(start=s, end=e, overnight=False, early_window=30, grace=10, break_min=0, source="shared", shift_date=d)
    people = [{"id": 1, "name": "A", "department": "Eng"}]
    now = datetime(2026, 1, 10, 0, 0)
    cur = (date(2026, 1, 5), date(2026, 1, 9)); prev = (date(2025, 12, 29), date(2026, 1, 2))
    rows_now = [row(1, "in", datetime(2026, 1, 6, 8, 20), flag="late", shift_date=date(2026, 1, 6))]  # 1
    rows_prev = [row(1, "in", datetime(2025, 12, 30, 8, 20), flag="late", shift_date=date(2025, 12, 30))]  # 1, total 2 < 10
    tn = _team(people, cur, rows_now, res, now); tp = _team(people, prev, rows_prev, res, now)
    ins = compute_insights(start=cur[0], end=cur[1], department=None, team_now=tn, team_prev=tp,
                           weekday_now=[], late_hours={}, people_now=[], dept_groups=[], recognition=None)
    assert not [x for x in ins if x["id"] == "late_vs_previous"]


def test_insight_weekday_and_hour_peak():
    def res(pid, d):
        s = datetime(d.year, d.month, d.day, 8, 0); e = datetime(d.year, d.month, d.day, 17, 0)
        return ResolvedShift(start=s, end=e, overnight=False, early_window=30, grace=10, break_min=0, source="shared", shift_date=d)
    people = [{"id": i, "name": f"P{i}", "department": "Eng"} for i in range(1, 5)]
    now = datetime(2026, 1, 10, 0, 0)
    cur = (date(2026, 1, 5), date(2026, 1, 9))  # 5=Mon
    rows = [
        row(1, "in", datetime(2026, 1, 5, 9, 15), flag="late", shift_date=date(2026, 1, 5)),  # Mon 09
        row(2, "in", datetime(2026, 1, 5, 9, 20), flag="late", shift_date=date(2026, 1, 5)),  # Mon 09
        row(3, "in", datetime(2026, 1, 6, 8, 20), flag="late", shift_date=date(2026, 1, 6)),  # Tue 08
    ]
    tn = _team(people, cur, rows, res, now)
    ins = compute_insights(start=cur[0], end=cur[1], department=None, team_now=tn, team_prev=None,
                           weekday_now=lateness_by_weekday(people, cur, rows, resolve=res),
                           late_hours=late_arrival_hours(people, cur, rows, resolve=res),
                           people_now=[person_metrics(p, cur, rows, resolve=res, now=now) for p in people],
                           dept_groups=[], recognition=None)
    m = [x for x in ins if x["id"] == "late_peak_time"][0]
    assert m["numbers"]["weekday"] == "Mon" and m["numbers"]["weekday_late"] == 2
    assert m["numbers"]["peak_hour"] == 9 and m["numbers"]["peak_hour_late"] == 2
    assert "09:00" in m["text"]


def test_insight_staff_followup_thresholds():
    def res(pid, d):
        s = datetime(d.year, d.month, d.day, 8, 0); e = datetime(d.year, d.month, d.day, 17, 0)
        return ResolvedShift(start=s, end=e, overnight=False, early_window=30, grace=10, break_min=0, source="shared", shift_date=d)
    people = [{"id": 1, "name": "Latey", "department": "Eng"}, {"id": 2, "name": "Missy", "department": "Eng"}, {"id": 3, "name": "Ok", "department": "Eng"}]
    cur = (date(2026, 1, 5), date(2026, 1, 9)); now = datetime(2026, 1, 10, 0, 0)
    rows = [row(1, "in", datetime(2026, 1, d, 8, 20), flag="late", shift_date=date(2026, 1, d)) for d in (5, 6, 7)]  # 3 late
    rows += [row(2, "in", datetime(2026, 1, 5, 8, 0), shift_date=date(2026, 1, 5)),
             row(2, "out", datetime(2026, 1, 5, 22, 0), flag="missing_clock_out", shift_date=date(2026, 1, 5)),
             row(2, "in", datetime(2026, 1, 6, 8, 0), shift_date=date(2026, 1, 6)),
             row(2, "out", datetime(2026, 1, 6, 22, 0), flag="missing_clock_out", shift_date=date(2026, 1, 6))]
    tn = _team(people, cur, rows, res, now)
    per = [person_metrics(p, cur, rows, resolve=res, now=now) for p in people]
    ins = compute_insights(start=cur[0], end=cur[1], department=None, team_now=tn, team_prev=None,
                           weekday_now=[], late_hours={}, people_now=per, dept_groups=[], recognition=None)
    m = [x for x in ins if x["id"] == "staff_followup"][0]
    assert m["numbers"]["count"] == 2
    ids = {s["staff_id"] for s in m["numbers"]["staff"]}
    assert ids == {1, 2}


def test_insight_low_department_more_than_10_points_below_overall():
    def res(pid, d):
        s = datetime(d.year, d.month, d.day, 8, 0); e = datetime(d.year, d.month, d.day, 17, 0)
        return ResolvedShift(start=s, end=e, overnight=False, early_window=30, grace=10, break_min=0, source="shared", shift_date=d)
    people = [{"id": 1, "name": "A", "department": "Eng"}, {"id": 2, "name": "B", "department": "Sales"}]
    cur = (date(2026, 1, 5), date(2026, 1, 5)); now = datetime(2026, 1, 5, 23, 0)
    rows = [row(1, "in", datetime(2026, 1, 5, 8, 0), shift_date=date(2026, 1, 5))]  # Eng present, Sales absent
    tn = team_metrics(people, cur, rows, resolve=res, now=now)
    dept = team_metrics(people, cur, rows, resolve=res, now=now, group_by="department")
    ins = compute_insights(start=cur[0], end=cur[1], department=None, team_now=tn, team_prev=None,
                           weekday_now=[], late_hours={}, people_now=[], dept_groups=dept, recognition=None)
    m = [x for x in ins if x["id"] == "low_department_rate"][0]
    assert m["numbers"]["departments"][0]["department"] == "Sales"
    assert m["numbers"]["departments"][0]["gap_points"] == pytest.approx(50.0)


def test_insight_recognition_health_success_rejected_backlog():
    tn = {"late": 0, "attendance_rate": 1.0, "scheduled_ended": 1}
    reco = {"attempts": 20, "matched": 10, "rejected": 10, "success_rate": 0.5,
            "rejected_by_day": [{"date": "2026-01-05", "count": 10}], "backlog": 4}
    ins = compute_insights(start=date(2026, 1, 5), end=date(2026, 1, 5), department=None,
                           team_now=tn, team_prev=None, weekday_now=[], late_hours={}, people_now=[],
                           dept_groups=[], recognition=reco)
    m = [x for x in ins if x["id"] == "recognition_health"][0]
    assert m["numbers"]["success_rate"] == 0.5 and m["numbers"]["rejected"] == 10 and m["numbers"]["backlog"] == 4
    assert m["numbers"]["rejected_by_day"][0]["count"] == 10
    assert m["link"] == "review.html"


def test_insight_no_insight_case():
    tn = {"late": 0, "attendance_rate": 1.0, "scheduled_ended": 5}
    dept = [{"group": "Eng", "attendance_rate": 1.0, "scheduled_ended": 5}]
    people_now = [{"staff_id": 1, "name": "A", "late": 1, "missing_clock_outs": 0, "attendance_rate": 1.0}]
    # prev equal but total late events < 10 => no late-vs-prev; no peak (no late), no followup thresholds, dept gap 0, healthy recognition
    reco = {"attempts": 10, "matched": 10, "rejected": 0, "success_rate": 1.0, "backlog": 0, "rejected_by_day": []}
    ins = compute_insights(start=date(2026, 1, 5), end=date(2026, 1, 5), department=None,
                           team_now=tn, team_prev={"late": 0}, weekday_now=[], late_hours={}, people_now=people_now,
                           dept_groups=dept, recognition=reco)
    assert ins == []


def test_insight_capped_at_five_ordered_by_effect():
    tn = {"late": 20, "attendance_rate": 0.5, "scheduled_ended": 10}
    tp = {"late": 5}
    weekday = [{"weekday": 0, "weekday_name": "Mon", "clock_ins": 5, "late": 5, "on_time": 0, "on_time_rate": 0.0, "lateness_rate": 1.0}]
    dept = [{"group": "A", "attendance_rate": 0.2, "scheduled_ended": 5}, {"group": "B", "attendance_rate": 1.0, "scheduled_ended": 5}]
    people_now = [{"staff_id": i, "name": f"P{i}", "late": 3, "missing_clock_outs": 2} for i in range(1, 4)]
    reco = {"attempts": 10, "matched": 2, "rejected": 8, "success_rate": 0.2, "backlog": 5, "rejected_by_day": []}
    ins = compute_insights(start=date(2026, 1, 5), end=date(2026, 1, 9), department=None,
                           team_now=tn, team_prev=tp, weekday_now=weekday, late_hours={9: 5},
                           people_now=people_now, dept_groups=dept, recognition=reco)
    assert len(ins) <= 5
    effects = [x["effect"] for x in ins]
    assert effects == sorted(effects, reverse=True)
