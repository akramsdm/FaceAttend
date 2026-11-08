"""Table-driven tests for pure shift rules (app.shifts)."""
from datetime import date, datetime, time

import pytest

from app.shifts import evaluate, resolve_shift, shift_date_for, validate_pattern


# --- helpers / fixtures ---

def dt(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, 0)


OFFICE_DAY = {
    "weekday": 0,  # Monday; overridden per test as needed
    "start_time": time(8, 0),
    "end_time": time(17, 0),
    "overnight": False,
    "enabled": True,
}

OFFICE_PATTERN = {
    "id": "p_office",
    "name": "Office hours",
    "owner_staff_id": None,
    "early_window_min": 30,
    "grace_min": 10,
    "break_min": 60,
    "days": [
        {"weekday": wd, "start_time": time(8, 0), "end_time": time(17, 0), "overnight": False, "enabled": True}
        for wd in range(0, 5)  # Mon-Fri
    ],
}

NIGHT_PATTERN = {
    "id": "p_night",
    "name": "Night",
    "owner_staff_id": None,
    "early_window_min": 30,
    "grace_min": 10,
    "break_min": 30,
    "days": [
        {"weekday": wd, "start_time": time(22, 0), "end_time": time(6, 0), "overnight": True, "enabled": True}
        for wd in range(0, 7)
    ],
}

# 2026-01-05 is a Monday
MON = date(2026, 1, 5)
SUN = date(2026, 1, 4)  # Sunday


def resolve_office(on_date=MON, staff_id=1, extra=None):
    data = {"patterns": [OFFICE_PATTERN], "assignments": [
        {"staff_id": staff_id, "pattern_id": "p_office", "effective_from": date(2026, 1, 1), "effective_to": None}
    ], "exceptions": []}
    if extra:
        for k, v in extra.items():
            if isinstance(v, list) and isinstance(data.get(k), list):
                data[k] = data[k] + v
            else:
                data[k] = v
    return resolve_shift(staff_id, on_date, data)


# --- §4.2 rules table (every row), table-driven ---

RULES_TABLE = [
    # (id, open_session, now, expect_actions, expect_msg, expect_flag, expect_confirm)
    ("window_open_not_in", False, dt(2026, 1, 5, 7, 45),
     ["in"], "window_open_not_clocked_in", "none", False),
    ("clocked_in", True, dt(2026, 1, 5, 10, 0),
     ["out"], "clock_out_early_confirm", "early_departure", True),
    ("before_early_window", False, dt(2026, 1, 5, 7, 0),
     [], "before_window", "none", False),
    ("after_start_plus_grace", False, dt(2026, 1, 5, 8, 20),
     ["in"], "late_arrival", "late", False),
    ("clock_out_before_end", True, dt(2026, 1, 5, 15, 0),
     ["out"], "clock_out_early_confirm", "early_departure", True),
    ("clock_out_after_end", True, dt(2026, 1, 5, 17, 30),
     ["out"], "recorded", "overtime", False),
]


@pytest.mark.parametrize(
    "case_id,open_session,now,actions,msg,flag,confirm",
    RULES_TABLE,
    ids=[r[0] for r in RULES_TABLE],
)
def test_rules_table_42(case_id, open_session, now, actions, msg, flag, confirm):
    shift = resolve_office(MON)
    assert shift is not None
    res = evaluate(now, shift, open_session=open_session, margin_overtime=0)
    assert res["allowed_actions"] == actions
    assert res["message"] == msg
    assert res["flag"] == flag
    assert res.get("confirm_required", False) is confirm


def test_rules_table_no_shift_today():
    # Sunday, office pattern Mon-Fri only
    shift = resolve_office(SUN)
    assert shift is None
    res = evaluate(dt(2026, 1, 4, 9, 0), shift, open_session=False)
    assert res["allowed_actions"] == []
    assert res["message"] == "no_shift_request_approval"
    assert res["flag"] == "none"


# --- exact boundaries ---

def test_boundary_minute_window_opens():
    # early window 30m before 08:00 => opens 07:30:00; 07:29 disabled, 07:30 enabled
    shift = resolve_office(MON)
    before = evaluate(dt(2026, 1, 5, 7, 29), shift, False)
    assert before["allowed_actions"] == []
    assert before["message"] == "before_window"
    at_open = evaluate(dt(2026, 1, 5, 7, 30), shift, False)
    assert at_open["allowed_actions"] == ["in"]
    assert at_open["flag"] == "none"


def test_boundary_minute_grace_ends():
    # start 08:00, grace 10 => grace ends 08:10:00; 08:10 on-time, 08:11 late
    shift = resolve_office(MON)
    at_grace = evaluate(dt(2026, 1, 5, 8, 10), shift, False)
    assert at_grace["allowed_actions"] == ["in"]
    assert at_grace["flag"] == "none"
    after_grace = evaluate(dt(2026, 1, 5, 8, 11), shift, False)
    assert after_grace["allowed_actions"] == ["in"]
    assert after_grace["flag"] == "late"
    assert after_grace["message"] == "late_arrival"


# --- night shift crossing midnight ---

def test_night_shift_crossing_midnight():
    # Night 22:00-06:00 (overnight) starts Mon 2026-01-05
    data = {"patterns": [NIGHT_PATTERN], "assignments": [
        {"staff_id": 1, "pattern_id": "p_night", "effective_from": date(2026, 1, 1), "effective_to": None}
    ], "exceptions": []}
    shift = resolve_shift(1, MON, data)
    assert shift is not None
    assert shift.overnight is True
    assert shift.start == dt(2026, 1, 5, 22, 0)
    assert shift.end == dt(2026, 1, 6, 6, 0)

    # clock in at 21:45 (window open, 15m before start)
    res = evaluate(dt(2026, 1, 5, 21, 45), shift, False)
    assert res["allowed_actions"] == ["in"]
    assert res["flag"] == "none"

    # clock out after midnight but before end => early departure (ends 06:00)
    res = evaluate(dt(2026, 1, 6, 2, 0), shift, True)
    assert res["allowed_actions"] == ["out"]
    assert res["flag"] == "early_departure"

    # clock out after end => overtime (margin 0)
    res = evaluate(dt(2026, 1, 6, 6, 30), shift, True)
    assert res["allowed_actions"] == ["out"]
    assert res["flag"] == "overtime"


def test_shift_date_for_night_shift():
    data = {"patterns": [NIGHT_PATTERN], "assignments": [
        {"staff_id": 1, "pattern_id": "p_night", "effective_from": date(2026, 1, 1), "effective_to": None}
    ], "exceptions": []}
    shift = resolve_shift(1, MON, data)
    assert shift_date_for(dt(2026, 1, 5, 22, 30), shift) == MON
    # after midnight belongs to start date
    assert shift_date_for(dt(2026, 1, 6, 2, 0), shift) == MON
    assert shift_date_for(dt(2026, 1, 6, 5, 59), shift) == MON


# --- day-off exception, no assignment, assignment ending yesterday ---

def test_day_off_exception_overrides_pattern():
    shift = resolve_office(MON, extra={"exceptions": [
        {"staff_id": 1, "date": MON, "kind": "day_off", "reason": "leave"}
    ]})
    assert shift is None
    res = evaluate(dt(2026, 1, 5, 9, 0), shift, False)
    assert res["message"] == "no_shift_request_approval"


def test_no_assignment():
    data = {"patterns": [OFFICE_PATTERN], "assignments": [], "exceptions": []}
    shift = resolve_shift(1, MON, data)
    assert shift is None


def test_assignment_ending_yesterday():
    # effective_to == yesterday means it ended yesterday -> not effective today (Mon)
    data = {"patterns": [OFFICE_PATTERN], "assignments": [
        {"staff_id": 1, "pattern_id": "p_office", "effective_from": date(2025, 12, 1), "effective_to": date(2026, 1, 4)}
    ], "exceptions": []}
    assert resolve_shift(1, MON, data) is None  # Mon is after effective_to (Sun 4th)

    # while still effective on a weekday in pattern, it resolves
    data_active = {"patterns": [OFFICE_PATTERN], "assignments": [
        {"staff_id": 1, "pattern_id": "p_office", "effective_from": date(2025, 12, 1), "effective_to": date(2026, 1, 5)}
    ], "exceptions": []}
    assert resolve_shift(1, MON, data_active) is not None


# --- resolve order & personal pattern ---

def test_exception_wins_over_personal_and_shared():
    personal = {**OFFICE_PATTERN, "id": "p_pers", "owner_staff_id": 1,
                "days": [{"weekday": 0, "start_time": time(9, 0), "end_time": time(18, 0), "overnight": False, "enabled": True}]}
    data = {"patterns": [OFFICE_PATTERN, personal], "assignments": [
        {"staff_id": 1, "pattern_id": "p_office", "effective_from": date(2026, 1, 1), "effective_to": None}
    ], "exceptions": [{"staff_id": 1, "date": MON, "kind": "custom_hours",
                       "start_time": time(10, 0), "end_time": time(15, 0), "reason": "appt"}]}
    shift = resolve_shift(1, MON, data)
    assert shift is not None
    assert shift.start == dt(2026, 1, 5, 10, 0)
    assert shift.source == "exception"


# --- validate_pattern ---

def test_validate_pattern_ok():
    assert validate_pattern(OFFICE_PATTERN) == []
    assert validate_pattern(NIGHT_PATTERN) == []


def test_validate_pattern_end_not_after_start():
    bad = {"id": "x", "days": [{"weekday": 0, "start_time": time(17, 0), "end_time": time(8, 0), "overnight": False, "enabled": True}]}
    errs = validate_pattern(bad)
    assert any("end must be after start" in e for e in errs)


def test_validate_pattern_break_not_shorter():
    bad = {"id": "x", "break_min": 600, "days": [{"weekday": 0, "start_time": time(8, 0), "end_time": time(12, 0), "overnight": False, "enabled": True}]}
    errs = validate_pattern(bad)
    assert any("break must be shorter" in e for e in errs)


def test_validate_pattern_overnight_allows_end_before_start():
    assert validate_pattern(NIGHT_PATTERN) == []
