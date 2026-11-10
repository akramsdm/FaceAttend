"""Pure shift/clock rule logic (no database, no web).

Data shapes are plain mappings/objects (dicts, dataclasses, simple namespaces).
Times/dates use stdlib datetime types; "now"/"timestamp" values are treated as
naive datetimes in the server's local timezone (TIMEZONE) unless stated.

Conventions used by this module (aligned to Phase 2 defaults):
- ``early_window`` / ``grace`` / ``break_min`` / ``margin_overtime`` are minutes.
- A day-off/absent exception resolves to no shift.
- ``evaluate(now, shift=None, ...)`` means "no shift today".
- For an overnight shift, "before the early window" means before the
  early-window opening on the shift's *start* date (a clock-out after midnight
  while in-session is handled as clocked-in, not "before window").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping

MINUTES = timedelta(minutes=1)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _time(val: Any) -> time:
    if isinstance(val, time):
        return val
    if isinstance(val, datetime):
        return val.time().replace(tzinfo=None)
    if isinstance(val, str):
        parts = val.strip().split(":")
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    raise TypeError(f"unsupported time value: {val!r}")


def _minutes(val: Any, default: int = 0) -> int:
    if val is None:
        return int(default)
    return int(val)


@dataclass(frozen=True)
class ResolvedShift:
    """Resolved shift window for one staff member on one (start) date."""

    start: datetime
    end: datetime
    overnight: bool
    early_window: int  # minutes before start
    grace: int  # minutes after start
    break_min: int
    source: str = "pattern"  # exception | personal | shared
    shift_date: date | None = None
    pattern_id: int | None = None


def _find_personal_pattern(
    staff_id: Any, data: Mapping[str, Any]
) -> Any | None:
    patterns = _get(data, "patterns", []) or []
    for p in patterns:
        if _get(p, "owner_staff_id") == staff_id:
            return p
    return None


def _find_pattern_by_id(pattern_id: Any, data: Mapping[str, Any]) -> Any | None:
    if pattern_id is None:
        return None
    for p in _get(data, "patterns", []) or []:
        if _get(p, "id") == pattern_id:
            return p
    return None


def _active_assignment(
    staff_id: Any, on_date: date, data: Mapping[str, Any]
) -> Any | None:
    assignments = _get(data, "assignments", []) or []
    for a in assignments:
        if _get(a, "staff_id") != staff_id:
            continue
        eff = _get(a, "effective_from")
        eff_to = _get(a, "effective_to")
        if eff is not None and on_date < eff:
            continue
        if eff_to is not None and on_date > eff_to:
            continue
        return a
    return None


def _matching_exception(
    staff_id: Any, on_date: date, data: Mapping[str, Any]
) -> Any | None:
    for ex in _get(data, "exceptions", []) or []:
        if _get(ex, "staff_id") == staff_id and _get(ex, "date") == on_date:
            return ex
    return None


def _pattern_day(pattern: Any, weekday: int) -> Any | None:
    days = _get(pattern, "days", []) or _get(pattern, "pattern_days", []) or []
    for d in days:
        if int(_get(d, "weekday", -1)) == int(weekday):
            return d
    return None


def _build_resolved(
    on_date: date,
    start_t: time,
    end_t: time,
    overnight: bool,
    early_window: int,
    grace: int,
    break_min: int,
    source: str,
    pattern_id: int | None = None,
) -> ResolvedShift:
    start_dt = datetime.combine(on_date, start_t)
    end_day = on_date + timedelta(days=1) if overnight else on_date
    end_dt = datetime.combine(end_day, end_t)
    return ResolvedShift(
        start=start_dt,
        end=end_dt,
        overnight=bool(overnight),
        early_window=int(early_window),
        grace=int(grace),
        break_min=int(break_min),
        source=source,
        shift_date=on_date,
        pattern_id=pattern_id,
    )


def _resolved_from_pattern(
    pattern: Any, on_date: date, source: str
) -> ResolvedShift | None:
    day = _pattern_day(pattern, on_date.weekday())
    if day is None:
        return None
    if not bool(_get(day, "enabled", True)):
        return None
    start_t = _time(_get(day, "start_time"))
    end_t = _time(_get(day, "end_time"))
    # A cross-midnight span implies overnight even if not flagged. Conversely,
    # a span whose end is after its start cannot cross midnight: ignore a stale
    # overnight flag left over from an earlier night-shift configuration.
    overnight = end_t <= start_t
    early_window = _minutes(_get(pattern, "early_window_min", _get(pattern, "early_window", 30)), 30)
    grace = _minutes(_get(pattern, "grace_min", _get(pattern, "grace", 10)), 10)
    break_min = _minutes(_get(pattern, "break_min", _get(pattern, "break", 0)), 0)
    return _build_resolved(on_date, start_t, end_t, overnight, early_window, grace, break_min, source, pattern_id=_get(pattern, "id"))


def _is_day_off_exception(ex: Any) -> bool:
    kind = str(_get(ex, "kind", "")).lower()
    if kind in {"day_off", "dayoff", "off"}:
        return True
    if _get(ex, "is_day_off") is True:
        return True
    # A custom-hours exception without times behaves as no shift.
    if kind in {"custom_hours", "custom"}:
        return _get(ex, "start_time") is None or _get(ex, "end_time") is None
    return False


def _resolved_from_exception(
    ex: Any, on_date: date, staff_id: Any, data: Mapping[str, Any]
) -> ResolvedShift | None:
    if _is_day_off_exception(ex):
        return None
    # custom hours: use exception times; inherit windows from the would-be pattern
    # when available, else sensible defaults.
    start_t = _time(_get(ex, "start_time"))
    end_t = _time(_get(ex, "end_time"))
    # Derive overnight from the times: an end at or before the start is the
    # only shape that can cross midnight.
    overnight = end_t <= start_t
    # inherit early/grace/break from personal, then assigned pattern.
    inherit = _find_personal_pattern(staff_id, data)
    if inherit is None:
        assignment = _active_assignment(staff_id, on_date, data)
        if assignment is not None:
            inherit = _find_pattern_by_id(_get(assignment, "pattern_id"), data)
    early_window = _minutes(_get(inherit, "early_window_min", _get(inherit, "early_window", 30)), 30) if inherit else 30
    grace = _minutes(_get(inherit, "grace_min", _get(inherit, "grace", 10)), 10) if inherit else 10
    break_min = _minutes(_get(inherit, "break_min", _get(inherit, "break", 0)), 0) if inherit else 0
    base_pid = _get(inherit, "id") if inherit is not None else None
    return _build_resolved(on_date, start_t, end_t, overnight, early_window, grace, break_min, "exception", pattern_id=base_pid)


def resolve_shift(staff_id: Any, on_date: date, data: Mapping[str, Any]) -> ResolvedShift | None:
    """Resolve the shift for ``staff_id`` on ``on_date``.

    Order (first match wins): date exception, personal pattern, assigned shared
    pattern, otherwise ``None`` (no shift). A day-off exception yields ``None``.

    ``data`` is a plain mapping that may contain ``patterns`` (each with optional
    ``owner_staff_id`` and ``days``/``pattern_days``), ``assignments`` (staff_id,
    pattern_id, effective_from/to), and ``exceptions`` (staff_id, date, kind,
    optional start/end_time, reason).
    """
    ex = _matching_exception(staff_id, on_date, data)
    if ex is not None:
        return _resolved_from_exception(ex, on_date, staff_id, data)

    personal = _find_personal_pattern(staff_id, data)
    if personal is not None:
        resolved = _resolved_from_pattern(personal, on_date, "personal")
        if resolved is not None:
            return resolved
        # personal pattern exists but no enabled day -> fall through? treat as no
        # match for this day and continue to shared assignment.
        # (a personal pattern takes precedence as an identity; its day off means
        # no workday from that pattern - continue to shared only if spec wanted
        # fallback; spec states "personal pattern" as the step - day disabled
        # means not working that day under it.)
        return None

    assignment = _active_assignment(staff_id, on_date, data)
    if assignment is not None:
        pattern = _find_pattern_by_id(_get(assignment, "pattern_id"), data)
        if pattern is not None:
            return _resolved_from_pattern(pattern, on_date, "shared")
    return None


def resolve_pattern(
    pattern_id: Any, on_date: date, data: Mapping[str, Any], source: str = "pattern"
) -> ResolvedShift | None:
    """Rebuild a resolved shift from a stored ``shift_id`` (pattern id).

    This is used to reconstruct the shift that belongs to an already-open
    attendance session. It deliberately resolves the pattern directly instead
    of re-running assignment/exception resolution for the current day, so a
    later reassignment cannot change an open session's shift.
    """

    pattern = _find_pattern_by_id(pattern_id, data)
    if pattern is None:
        return None
    return _resolved_from_pattern(pattern, on_date, source)


def _shift_bounds(shift: Any) -> tuple[datetime, datetime, bool, int, int, int]:
    if isinstance(shift, ResolvedShift):
        return (shift.start, shift.end, shift.overnight, shift.early_window, shift.grace, shift.break_min)
    if isinstance(shift, Mapping):
        start = shift["start"]
        end = shift["end"]
        if isinstance(start, time):
            # relative form: requires context; interpret as same arbitrary date
            base = shift.get("shift_date") or date.today()
            start = datetime.combine(base, start)
        if isinstance(end, time):
            base = (start.date() + timedelta(days=1)) if shift.get("overnight") else start.date()
            end = datetime.combine(base, end)
        return (
            start,
            end,
            bool(shift.get("overnight", end <= start)),
            _minutes(shift.get("early_window", shift.get("early_window_min", 30)), 30),
            _minutes(shift.get("grace", shift.get("grace_min", 10)), 10),
            _minutes(shift.get("break_min", shift.get("break", 0)), 0),
        )
    start = getattr(shift, "start")
    end = getattr(shift, "end")
    return (
        start,
        end,
        bool(getattr(shift, "overnight", end <= start)),
        _minutes(getattr(shift, "early_window", getattr(shift, "early_window_min", 30)), 30),
        _minutes(getattr(shift, "grace", getattr(shift, "grace_min", 10)), 10),
        _minutes(getattr(shift, "break_min", getattr(shift, "break", 0)), 0),
    )


def shift_date_for(timestamp: datetime, shift: Any) -> date:
    """Return the shift (start) date a ``timestamp`` belongs to.

    For an overnight shift, times after midnight but before the shift end belong
    to the previous calendar date (the date the shift started). For non-overnight
    shifts the calendar date of ``timestamp`` is returned.
    """
    start, end, overnight, *_ = _shift_bounds(shift)
    if not overnight:
        return timestamp.date()
    ts_t = timestamp.time().replace(tzinfo=None)
    end_t = end.time().replace(tzinfo=None)
    start_t = start.time().replace(tzinfo=None)
    # After midnight portion of an overnight shift: time is < end_t and < start_t
    # (or simply <= end_t on the next day) -> belongs to previous day.
    if ts_t < start_t and ts_t <= end_t:
        return (timestamp.date() - timedelta(days=1))
    # Also handle if timestamp is already on the next calendar day but within end.
    if timestamp.date() > start.date() and timestamp <= end:
        return start.date()
    return timestamp.date() if timestamp.date() == start.date() else start.date()


def _fmt_hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def evaluate(
    now: datetime,
    shift: Any | None,
    open_session: bool,
    margin_overtime: int | float | None = 0,
    completed_session_for_shift: bool = False,
) -> dict[str, Any]:
    """Evaluate clock-in/out allowance for the current situation.

    Returns ``{"allowed_actions": [...], "message": <code>, "flag": <flag>}``
    where actions are subsets of ``["in","out"]``, message is a stable code, and
    flag is one of ``late|early_departure|overtime|none``.

    ``margin_overtime`` is minutes beyond shift end before ``overtime`` is flagged
    (clock-out after end but within the margin is allowed with flag ``none``).
    ``None`` is treated as 0.

    ``completed_session_for_shift``: True when a non-undone in has a later
    non-undone out for the same person and shift_date (shift already completed)
    and there is no open session.
    """
    ot_margin = int(0 if margin_overtime is None else margin_overtime)

    if shift is None:
        return {
            "allowed_actions": [],
            "message": "no_shift_request_approval",
            "flag": "none",
            "confirm_required": False,
        }

    if completed_session_for_shift and not open_session:
        return {
            "allowed_actions": [],
            "message": "shift_already_completed",
            "flag": "none",
            "confirm_required": False,
            "reason_code": "reentry",
        }

    start, end, overnight, early_window, grace, _break = _shift_bounds(shift)
    window_open = start - timedelta(minutes=early_window)
    grace_end = start + timedelta(minutes=grace)

    if open_session:
        # Clock-in disabled while clocked in.
        if now < end:
            return {
                "allowed_actions": ["out"],
                "message": "clock_out_early_confirm",
                "flag": "early_departure",
                "confirm_required": True,
            }
        # now >= end: after shift end
        if now <= end + timedelta(minutes=ot_margin):
            return {
                "allowed_actions": ["out"],
                "message": "recorded",
                "flag": "none",
                "confirm_required": False,
            }
        return {
            "allowed_actions": ["out"],
            "message": "recorded",
            "flag": "overtime",
            "confirm_required": False,
        }

    # Not clocked in.
    # For an overnight shift, an early-morning "now" (after midnight, within the
    # shift) is not "before the early window" of that start date; it is treated as
    # after grace (late) if the person never clocked in.
    if overnight and now.date() > start.date() and now <= end:
        return {
            "allowed_actions": ["in"],
            "message": "late_arrival",
            "flag": "late",
            "confirm_required": False,
        }

    if now < window_open:
        return {
            "allowed_actions": [],
            "message": "before_window",
            "flag": "none",
            "confirm_required": False,
            "window_opens_at": window_open,
            "shift_starts_at": start,
        }
    # Clock-in after the shift has already ended is not a normal on-time
    # session. Late arrival *within* the shift is still allowed and flagged
    # (product rule), but after shift end an explicit approval is required.
    if now > end:
        return {
            "allowed_actions": [],
            "message": "after_shift_end",
            "flag": "none",
            "confirm_required": False,
            "reason_code": "out_of_window",
        }
    if now <= start:
        return {
            "allowed_actions": ["in"],
            "message": "window_open_not_clocked_in",
            "flag": "none",
            "confirm_required": False,
        }
    if now <= grace_end:
        return {
            "allowed_actions": ["in"],
            "message": "window_open_not_clocked_in",
            "flag": "none",
            "confirm_required": False,
        }
    # after start + grace (and not clocked in)
    return {
        "allowed_actions": ["in"],
        "message": "late_arrival",
        "flag": "late",
        "confirm_required": False,
    }


def _to_time_or_none(val: Any) -> time | None:
    if val is None:
        return None
    return _time(val)


def validate_pattern(pattern: Any) -> list[str]:
    """Validate a shift pattern per §4.3 (pattern-level rules).

    Returns a list of error strings (empty when valid). Checks:
    - end after start unless marked overnight
    - break shorter than the shift
    - (day entries present make sense)

    Note: overlapping assignments for one person, one shift per person per day,
    and "exceptions need a reason" are data/cross-record rules enforced where
    assignments/exceptions are validated (API layer), not on a single pattern.
    """
    errors: list[str] = []
    days = _get(pattern, "days", []) or _get(pattern, "pattern_days", []) or []
    if not days:
        errors.append("pattern has no days")
        return errors

    break_min = _minutes(_get(pattern, "break_min", _get(pattern, "break", 0)), 0)
    for d in days:
        if not bool(_get(d, "enabled", True)):
            continue
        weekday = _get(d, "weekday", "?")
        start_t = _to_time_or_none(_get(d, "start_time"))
        end_t = _to_time_or_none(_get(d, "end_time"))
        overnight = bool(_get(d, "overnight", False))
        if start_t is None or end_t is None:
            errors.append(f"weekday {weekday}: start_time and end_time required")
            continue
        if not overnight and end_t <= start_t:
            errors.append(f"weekday {weekday}: end must be after start unless overnight")
            continue
        # compute duration
        start_dt = datetime.combine(date(2000, 1, 1), start_t)
        end_day = date(2000, 1, 2) if (overnight or end_t <= start_t) else date(2000, 1, 1)
        end_dt = datetime.combine(end_day, end_t)
        duration_min = (end_dt - start_dt).total_seconds() / 60.0
        if break_min >= duration_min:
            errors.append(f"weekday {weekday}: break must be shorter than the shift")
    return errors
