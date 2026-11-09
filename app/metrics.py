"""Pure attendance/schedule metrics over plain data (no web, no database).

All inputs are plain mappings/objects (dict-like or attribute access) compatible
with SQLAlchemy model instances and the shapes used by app.shifts/rules.
Timestamps/shifts are naive datetimes in the server local timezone (same
convention as app.shifts). Night shifts group by ``shift_date`` (the shift's
start date).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from math import ceil
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.shifts import ResolvedShift, resolve_shift as _resolve_shift

# ---------------------------- access helpers ----------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, time.min)
    return None


def _naive(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    return v.replace(tzinfo=None) if getattr(v, "tzinfo", None) else v


def _pid(person: Any) -> Any:
    v = _get(person, "id", None)
    return v if v is not None else _get(person, "staff_id", None)


def _iter_days(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


# -------------------------- resolution / filtering -----------------------

def _resolve_for(person_id: Any, day: date, data: Mapping[str, Any] | None, resolver: Callable | None) -> ResolvedShift | None:
    if resolver is not None:
        try:
            return resolver(person_id, day)
        except Exception:
            return None
    if data is None:
        return None
    try:
        return _resolve_shift(person_id, day, data)
    except Exception:
        return None


def _is_day_off_exception(person_id: Any, day: date, data: Mapping[str, Any] | None, exceptions: Sequence[Any] | None) -> bool:
    exs = exceptions
    if exs is None and data is not None:
        exs = _get(data, "exceptions", []) or []
    for ex in exs or []:
        if _get(ex, "staff_id") != person_id:
            continue
        exd = _as_date(_get(ex, "date"))
        if exd != day:
            continue
        kind = str(_get(ex, "kind", "")).lower()
        if kind in {"day_off", "dayoff", "off"} or _get(ex, "is_day_off") is True:
            return True
    return False


def scheduled_day(person_id: Any, day: date, data: Mapping[str, Any] | None = None, resolver: Callable | None = None, exceptions: Sequence[Any] | None = None) -> bool:
    """True if ``day`` has a resolved shift and no day-off exception."""
    if _is_day_off_exception(person_id, day, data, exceptions):
        return False
    sh = _resolve_for(person_id, day, data, resolver)
    return sh is not None


# ----------------------------- row helpers ------------------------------

def _valid_rows(rows: Sequence[Any]) -> list[Any]:
    # ignore undone events
    return [r for r in rows if _get(r, "undone_at") is None and _get(r, "is_undone") is not True]


def _row_day(row: Any) -> date | None:
    sd = _as_date(_get(row, "shift_date"))
    if sd is not None:
        return sd
    ts = _as_dt(_get(row, "timestamp"))
    return ts.date() if ts is not None else None


def _rows_for_person_day(rows: Sequence[Any], person_id: Any, day: date) -> list[Any]:
    out = []
    for r in _valid_rows(rows):
        if _get(r, "staff_id") != person_id:
            continue
        if _row_day(r) == day:
            out.append(r)
    return out


def _first_in(rows: Sequence[Any]) -> Any | None:
    ins = [r for r in rows if _get(r, "type") == "in" and _get(r, "flag") != "missing_clock_out"]
    if not ins:
        return None
    ins.sort(key=lambda r: (_as_dt(_get(r, "timestamp")) or datetime.min, _get(r, "id", 0) or 0))
    return ins[0]


# --------------------------- metric primitives --------------------------

def _shift_end(sh: ResolvedShift | None) -> datetime | None:
    return _naive(sh.end) if sh is not None else None


def _late_info(first_in: Any, sh: ResolvedShift | None) -> tuple[bool, int | None]:
    """(is_late, late_minutes_after_shift_start). Uses stored flag where present."""
    if first_in is None:
        return False, None
    ts = _naive(_as_dt(_get(first_in, "timestamp")))
    if ts is None:
        return False, None
    flag_late = _get(first_in, "flag") == "late"
    if sh is None:
        return flag_late, None
    start = _naive(sh.start)
    grace_end = start + timedelta(minutes=int(sh.grace))
    is_late = flag_late or ts > grace_end
    if not is_late:
        return False, 0
    mins = int(ceil((ts - start).total_seconds() / 60.0))
    return True, max(0, mins)


def _pair_hours(day_rows: Sequence[Any], sh: ResolvedShift | None) -> tuple[float, int, int, int, int, float | None, float | None]:
    """Returns (hours, early_departures, overtime_count, missing_clock_outs, unpaired, early_min, overtime_min).

    Hours: in->out completed pairs minus break_min; missing clock-outs excluded.
    Minute offsets are vs shift end (positive = overtime minutes, early = minutes
    before shift end on early departure).
    """
    valid = sorted(_valid_rows(day_rows), key=lambda r: (_as_dt(_get(r, "timestamp")) or datetime.min, _get(r, "id", 0) or 0))
    missing = sum(1 for r in valid if _get(r, "flag") == "missing_clock_out")
    early_dep = sum(1 for r in valid if _get(r, "flag") == "early_departure")
    overtime_n = sum(1 for r in valid if _get(r, "flag") == "overtime")
    break_min = int(sh.break_min) if sh is not None else 0
    shift_end = _shift_end(sh)

    early_min = None
    overtime_min = None
    for r in valid:
        ts = _naive(_as_dt(_get(r, "timestamp")))
        if ts is None or shift_end is None or _get(r, "type") != "out":
            continue
        if _get(r, "flag") == "missing_clock_out":
            continue
        diff_min = (ts - shift_end).total_seconds() / 60.0
        if _get(r, "flag") == "early_departure":
            m = max(0.0, -diff_min)
            early_min = m if early_min is None else min(early_min, m)
        elif _get(r, "flag") == "overtime":
            m = max(0.0, diff_min)
            overtime_min = m if overtime_min is None else max(overtime_min, m)

    total_seconds = 0.0
    pending_in = None
    unpaired = 0
    for r in valid:
        if _get(r, "type") == "in" and _get(r, "flag") != "missing_clock_out":
            if pending_in is not None:
                unpaired += 1
            pending_in = _naive(_as_dt(_get(r, "timestamp")))
        elif _get(r, "type") == "out":
            if _get(r, "flag") == "missing_clock_out":
                pending_in = None
                continue
            if pending_in is not None:
                ts = _naive(_as_dt(_get(r, "timestamp")))
                if ts is not None:
                    delta = (ts - pending_in).total_seconds()
                    if delta > 0:
                        total_seconds += max(0.0, delta - break_min * 60)
                pending_in = None
            else:
                unpaired += 1
    if pending_in is not None:
        unpaired += 1
    hours = round(total_seconds / 3600.0, 4)
    return hours, early_dep, overtime_n, missing, unpaired, early_min, overtime_min


# ----------------------------- person metrics ---------------------------

def person_metrics(
    person: Any,
    date_range: tuple[date, date] | Sequence[date],
    rows: Sequence[Any],
    data: Mapping[str, Any] | None = None,
    *,
    resolve: Callable[[Any, date], ResolvedShift | None] | None = None,
    exceptions: Sequence[Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute metrics for one person over ``date_range`` (start, end) inclusive.

    ``rows`` are attendance rows (plain objects/dicts/mappings or model instances)
    for any staff; rows for this person are selected by ``staff_id``/``id``.
    ``data`` is the app.shifts-style mapping (patterns/assignments/exceptions)
    used with ``resolve_shift``; alternatively pass ``resolve(person_id, day)``.
    ``now`` (naive local) determines whether an in-progress/future day can be
    counted absent (a day is absent only once its shift end has passed).
    """
    person_id = _pid(person)
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2 and isinstance(date_range[0], date):
        start, end = date_range[0], date_range[1]
    else:
        days = sorted(date_range)  # type: ignore[arg-type]
        start, end = (days[0], days[-1]) if days else (date.today(), date.today())

    now_n = _naive(now)
    scheduled_ended = 0
    present_days = 0
    absent_days = 0
    clock_ins = 0
    on_time_ins = 0
    late_ins = 0
    late_minutes_total = 0
    early_departures = 0
    overtime_count = 0
    missing_clock_outs = 0
    hours_worked = 0.0
    arrival_offsets: list[float] = []
    by_day: list[dict[str, Any]] = []

    for day in _iter_days(start, end):
        sh = _resolve_for(person_id, day, data, resolve)
        is_scheduled = (sh is not None) and not _is_day_off_exception(person_id, day, data, exceptions)
        day_rows = _rows_for_person_day(rows, person_id, day)
        fin = _first_in(day_rows)
        is_present = is_scheduled and fin is not None

        ended = False
        if is_scheduled and sh is not None:
            ended = (now_n is not None and now_n > _naive(sh.end))
            if ended:
                scheduled_ended += 1
                if fin is not None:
                    present_days += 1
                else:
                    absent_days += 1

        is_late = False
        late_min = None
        if fin is not None and sh is not None:
            clock_ins += 1
            ts = _naive(_as_dt(_get(fin, "timestamp")))
            if ts is not None:
                offset = (ts - _naive(sh.start)).total_seconds() / 60.0
                arrival_offsets.append(offset)
            is_late, late_min = _late_info(fin, sh)
            if is_late:
                late_ins += 1
                late_minutes_total += int(late_min or 0)
            else:
                on_time_ins += 1
        elif fin is not None and sh is None:
            clock_ins += 1
            on_time_ins += 1  # no shift to be late against

        hrs, ed, ot, miss, _unp, _emin, _omin = _pair_hours(day_rows, sh)
        hours_worked += hrs
        early_departures += ed
        overtime_count += ot
        missing_clock_outs += miss

        by_day.append({
            "date": day, "scheduled": is_scheduled, "ended": ended,
            "present": bool(is_present), "late": is_late, "late_minutes": (late_min if is_late else 0),
            "hours_worked": hrs, "early_departures": ed, "overtime": ot, "missing_clock_outs": miss,
        })

    attendance_rate = (present_days / scheduled_ended) if scheduled_ended else 0.0
    on_time_rate = (on_time_ins / clock_ins) if clock_ins else 0.0
    avg_arrival = (sum(arrival_offsets) / len(arrival_offsets)) if arrival_offsets else 0.0
    return {
        "staff_id": person_id,
        "name": _get(person, "name"),
        "department": _get(person, "department"),
        "shift": _get(person, "shift") or _get(person, "shift_name"),
        "scheduled_ended": scheduled_ended,
        "present": present_days,
        "absent": absent_days,
        "clock_ins": clock_ins,
        "on_time": on_time_ins,
        "late": late_ins,
        "on_time_rate": on_time_rate,
        "attendance_rate": attendance_rate,
        "late_minutes": late_minutes_total,
        "early_departures": early_departures,
        "overtime": overtime_count,
        "hours_worked": round(hours_worked, 4),
        "missing_clock_outs": missing_clock_outs,
        "average_arrival_offset_minutes": round(avg_arrival, 2),
        "days": by_day,
    }


# ----------------------------- aggregates -------------------------------

def daily_series(
    people: Sequence[Any],
    date_range: tuple[date, date] | Sequence[date],
    rows: Sequence[Any],
    data: Mapping[str, Any] | None = None,
    *,
    resolve: Callable[[Any, date], ResolvedShift | None] | None = None,
    exceptions: Sequence[Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2 and isinstance(date_range[0], date):
        start, end = date_range[0], date_range[1]
    else:
        days = sorted(date_range)  # type: ignore[arg-type]
        start, end = (days[0], days[-1]) if days else (date.today(), date.today())
    out = []
    for day in _iter_days(start, end):
        agg = {"date": day, "scheduled_ended": 0, "present": 0, "absent": 0, "late": 0, "clock_ins": 0, "on_time": 0, "hours_worked": 0.0, "missing_clock_outs": 0}
        for p in people:
            m = person_metrics(p, (day, day), rows, data, resolve=resolve, exceptions=exceptions, now=now)
            for k in ("scheduled_ended", "present", "absent", "late", "clock_ins", "on_time", "missing_clock_outs"):
                agg[k] += int(m.get(k, 0) or 0)
            agg["hours_worked"] = round(agg["hours_worked"] + float(m.get("hours_worked", 0) or 0), 4)
        agg["on_time_rate"] = (agg["on_time"] / agg["clock_ins"]) if agg["clock_ins"] else 0.0
        agg["attendance_rate"] = (agg["present"] / agg["scheduled_ended"]) if agg["scheduled_ended"] else 0.0
        out.append(agg)
    return out


def team_metrics(
    people: Sequence[Any],
    date_range: tuple[date, date] | Sequence[date],
    rows: Sequence[Any],
    data: Mapping[str, Any] | None = None,
    *,
    resolve: Callable[[Any, date], ResolvedShift | None] | None = None,
    exceptions: Sequence[Any] | None = None,
    now: datetime | None = None,
    group_by: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Team totals, or grouped by ``department`` / ``shift`` / ``weekday``."""
    if group_by == "weekday":
        per = [person_metrics(p, date_range, rows, data, resolve=resolve, exceptions=exceptions, now=now) for p in people]
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        buckets = {i: {"weekday": i, "weekday_name": names[i], "clock_ins": 0, "late": 0, "on_time": 0} for i in range(7)}
        for pm in per:
            for d in pm.get("days", []):
                # count clock-ins/late on days that had a clock-in? derive from day rows via flags
                wd = d["date"].weekday() if isinstance(d["date"], date) else 0
                # a day contributes at most 1 first clock-in in our per-day model; infer from present/late on scheduled
                # but easier: recompute from daily detail
                pass
        # recompute weekday from person rows directly
        buckets = {i: {"weekday": i, "weekday_name": names[i], "clock_ins": 0, "late": 0, "on_time": 0} for i in range(7)}
        if isinstance(date_range, (tuple, list)) and len(date_range) == 2 and isinstance(date_range[0], date):
            start, end = date_range[0], date_range[1]
        else:
            days = sorted(date_range)  # type: ignore[arg-type]
            start, end = (days[0], days[-1]) if days else (date.today(), date.today())
        for p in people:
            pid = _pid(p)
            for day in _iter_days(start, end):
                sh = _resolve_for(pid, day, data, resolve)
                day_rows = _rows_for_person_day(rows, pid, day)
                fin = _first_in(day_rows)
                if fin is None:
                    continue
                wd = day.weekday()
                buckets[wd]["clock_ins"] += 1
                is_late, _ = _late_info(fin, sh)
                if is_late:
                    buckets[wd]["late"] += 1
                else:
                    buckets[wd]["on_time"] += 1
        res = []
        for i in range(7):
            b = buckets[i]
            b["on_time_rate"] = (b["on_time"] / b["clock_ins"]) if b["clock_ins"] else 0.0
            b["lateness_rate"] = (b["late"] / b["clock_ins"]) if b["clock_ins"] else 0.0
            res.append(b)
        return res

    per = [person_metrics(p, date_range, rows, data, resolve=resolve, exceptions=exceptions, now=now) for p in people]
    if group_by in {"department", "shift"}:
        key = "department" if group_by == "department" else "shift"
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for pm in per:
            groups[str(pm.get(key) or "")].append(pm)
        out = []
        for g, items in sorted(groups.items()):
            out.append({"group": g, **_sum_person_metrics(items)})
        return out
    return {"group": "all", **_sum_person_metrics(per), "people": per}


def _sum_person_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def s(k: str) -> float:
        return sum(float(x.get(k, 0) or 0) for x in items)
    clock_ins = int(s("clock_ins"))
    on_time = int(s("on_time"))
    scheduled_ended = int(s("scheduled_ended"))
    present = int(s("present"))
    # average arrival offset weighted by clock-ins
    offsets = []
    for x in items:
        ci = int(x.get("clock_ins", 0) or 0)
        if ci:
            offsets.extend([float(x.get("average_arrival_offset_minutes", 0) or 0)] * ci)
    avg_arr = (sum(offsets) / len(offsets)) if offsets else 0.0
    return {
        "headcount": len(items),
        "scheduled_ended": scheduled_ended,
        "present": present,
        "absent": int(s("absent")),
        "clock_ins": clock_ins,
        "on_time": on_time,
        "late": int(s("late")),
        "on_time_rate": (on_time / clock_ins) if clock_ins else 0.0,
        "attendance_rate": (present / scheduled_ended) if scheduled_ended else 0.0,
        "late_minutes": int(s("late_minutes")),
        "early_departures": int(s("early_departures")),
        "overtime": int(s("overtime")),
        "hours_worked": round(s("hours_worked"), 4),
        "missing_clock_outs": int(s("missing_clock_outs")),
        "average_arrival_offset_minutes": round(avg_arr, 2),
    }


def arrival_histogram(
    people: Sequence[Any],
    date_range: tuple[date, date] | Sequence[date],
    rows: Sequence[Any],
    data: Mapping[str, Any] | None = None,
    *,
    resolve: Callable[[Any, date], ResolvedShift | None] | None = None,
    exceptions: Sequence[Any] | None = None,
    bucket_minutes: int = 15,
) -> list[dict[str, Any]]:
    """Histogram of arrival offsets (minutes vs shift start, negative=early)."""
    if bucket_minutes <= 0:
        bucket_minutes = 15
    offsets: list[float] = []
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2 and isinstance(date_range[0], date):
        start, end = date_range[0], date_range[1]
    else:
        days = sorted(date_range)  # type: ignore[arg-type]
        start, end = (days[0], days[-1]) if days else (date.today(), date.today())
    for p in people:
        pid = _pid(p)
        for day in _iter_days(start, end):
            sh = _resolve_for(pid, day, data, resolve)
            if sh is None:
                continue
            fin = _first_in(_rows_for_person_day(rows, pid, day))
            if fin is None:
                continue
            ts = _naive(_as_dt(_get(fin, "timestamp")))
            if ts is None:
                continue
            offsets.append((ts - _naive(sh.start)).total_seconds() / 60.0)
    if not offsets:
        return []
    b = bucket_minutes
    buckets: dict[int, int] = defaultdict(int)
    for v in offsets:
        key = int(v // b) * b
        buckets[key] += 1
    return [{"bucket_start_min": k, "bucket_end_min": k + b, "count": buckets[k]} for k in sorted(buckets)]


def lateness_by_weekday(
    people: Sequence[Any],
    date_range: tuple[date, date] | Sequence[date],
    rows: Sequence[Any],
    data: Mapping[str, Any] | None = None,
    *,
    resolve: Callable[[Any, date], ResolvedShift | None] | None = None,
    exceptions: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    return team_metrics(people, date_range, rows, data, resolve=resolve, exceptions=exceptions, group_by="weekday")  # type: ignore[return-value]


def top_n(people_metrics: Sequence[Mapping[str, Any]], metric: str, n: int = 5, *, reverse: bool = True) -> list[dict[str, Any]]:
    items = [dict(m) for m in people_metrics]
    items.sort(key=lambda m: (float(m.get(metric, 0) or 0), str(m.get("staff_id") or "")), reverse=reverse)
    return items[: max(0, int(n))]
