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


# ------------------------------- insights (2D) --------------------------

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def late_arrival_hours(
    people: Sequence[Any],
    date_range: tuple[date, date] | Sequence[date],
    rows: Sequence[Any],
    data: Mapping[str, Any] | None = None,
    *,
    resolve: Callable[[Any, date], ResolvedShift | None] | None = None,
    exceptions: Sequence[Any] | None = None,
) -> dict[int, int]:
    """Count late arrivals by local hour-of-day of the (late) first clock-in."""
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2 and isinstance(date_range[0], date):
        start, end = date_range[0], date_range[1]
    else:
        days = sorted(date_range)  # type: ignore[arg-type]
        start, end = (days[0], days[-1]) if days else (date.today(), date.today())
    counts: dict[int, int] = defaultdict(int)
    for p in people:
        pid = _pid(p)
        for day in _iter_days(start, end):
            sh = _resolve_for(pid, day, data, resolve)
            if sh is None or _is_day_off_exception(pid, day, data, exceptions):
                continue
            fin = _first_in(_rows_for_person_day(rows, pid, day))
            if fin is None:
                continue
            is_late, _ = _late_info(fin, sh)
            if not is_late:
                continue
            ts = _naive(_as_dt(_get(fin, "timestamp")))
            if ts is not None:
                counts[int(ts.hour)] += 1
    return dict(counts)


def _prev_equal_period(start: date, end: date) -> tuple[date, date]:
    days = (end - start).days + 1
    pend = start - timedelta(days=1)
    pstart = pend - timedelta(days=days - 1)
    return pstart, pend


def compute_insights(
    *,
    start: date,
    end: date,
    department: str | None,
    team_now: Mapping[str, Any],
    team_prev: Mapping[str, Any] | None,
    weekday_now: Sequence[Mapping[str, Any]] | None,
    late_hours: Mapping[int, int] | None,
    people_now: Sequence[Mapping[str, Any]],
    dept_groups: Sequence[Mapping[str, Any]] | None,
    recognition: Mapping[str, Any] | None,
    base_link: str = "attendance.html",
) -> list[dict[str, Any]]:
    """Plain-rule insights (no external services). Returns <=5, ordered by |effect| desc.

    ``recognition`` may include: attempts, matched, rejected, success_rate,
    rejected_by_day (list of {date,count}), backlog.
    """
    insights: list[dict[str, Any]] = []
    q = f"from={start.isoformat()}&to={end.isoformat()}"
    if department:
        q += f"&department={department}"

    # 1) Late arrivals vs previous equal period (percent change), only when >=10 events total.
    cur_late = int((team_now or {}).get("late", 0) or 0)
    prev_late = int((team_prev or {}).get("late", 0) or 0) if team_prev is not None else 0
    if team_prev is not None and (cur_late + prev_late) >= 10 and prev_late > 0:
        pct_change = round(((cur_late - prev_late) / prev_late) * 100.0, 1)
        direction = "up" if pct_change > 0 else ("down" if pct_change < 0 else "flat")
        insights.append({
            "id": "late_vs_previous",
            "text": f"Late arrivals {direction} {abs(pct_change)}% vs the previous equal period ({cur_late} now, {prev_late} before).",
            "numbers": {"current_late": cur_late, "previous_late": prev_late, "percent_change": pct_change, "events_total": cur_late + prev_late},
            "link": f"{base_link}?{q}&flag=late",
            "effect": abs(pct_change),
        })
    elif team_prev is not None and (cur_late + prev_late) >= 10 and prev_late == 0 and cur_late > 0:
        insights.append({
            "id": "late_vs_previous",
            "text": f"Late arrivals increased vs the previous equal period ({cur_late} now, 0 before).",
            "numbers": {"current_late": cur_late, "previous_late": 0, "percent_change": None, "events_total": cur_late},
            "link": f"{base_link}?{q}&flag=late",
            "effect": float(cur_late) * 10.0,
        })

    # 2) Weekday and hour with the most late arrivals.
    if cur_late > 0 and weekday_now:
        worst_wd = max(weekday_now, key=lambda b: (int(b.get("late", 0) or 0), -int(b.get("weekday", 0) or 0)))
        wd_late = int(worst_wd.get("late", 0) or 0)
        if wd_late > 0:
            wd_name = worst_wd.get("weekday_name") or WEEKDAY_NAMES[int(worst_wd.get("weekday", 0) or 0) % 7]
            hour_part = ""
            nums: dict[str, Any] = {"weekday": wd_name, "weekday_late": wd_late}
            if late_hours:
                peak_h = max(late_hours.items(), key=lambda kv: (kv[1], -kv[0]))[0]
                peak_h_n = int(late_hours.get(peak_h, 0) or 0)
                if peak_h_n > 0:
                    hour_part = f", most often around {int(peak_h):02d}:00"
                    nums["peak_hour"] = int(peak_h)
                    nums["peak_hour_late"] = peak_h_n
            insights.append({
                "id": "late_peak_time",
                "text": f"Most late arrivals fall on {wd_name} ({wd_late}){hour_part}.",
                "numbers": nums,
                "link": f"{base_link}?{q}&flag=late",
                "effect": float(wd_late),
            })

    # 3) Staff with >=3 late arrivals or >=2 missing clock-outs.
    flagged = []
    for p in people_now or []:
        late_n = int(p.get("late", 0) or 0)
        miss_n = int(p.get("missing_clock_outs", 0) or 0)
        if late_n >= 3 or miss_n >= 2:
            flagged.append({"staff_id": p.get("staff_id"), "name": p.get("name"), "department": p.get("department"), "late": late_n, "missing_clock_outs": miss_n})
    if flagged:
        flagged.sort(key=lambda x: (-x["late"], -x["missing_clock_outs"], x.get("name") or ""))
        top = flagged[0]
        parts = []
        if top["late"] >= 3:
            parts.append(f"{top['late']} late")
        if top["missing_clock_outs"] >= 2:
            parts.append(f"{top['missing_clock_outs']} missing clock-outs")
        who = top.get("name") or f"Staff {top['staff_id']}"
        text = f"{len(flagged)} staff need follow-up (e.g. {who}: {', '.join(parts)})."
        link = f"{base_link}?{q}&staff_id={top['staff_id']}" if top.get("staff_id") else f"{base_link}?{q}"
        insights.append({
            "id": "staff_followup",
            "text": text,
            "numbers": {"count": len(flagged), "staff": flagged[:10]},
            "link": link,
            "effect": float(max(top["late"] * 2, top["missing_clock_outs"] * 3, len(flagged))),
        })

    # 4) Department attendance rate >10 points below overall rate.
    overall_rate = float((team_now or {}).get("attendance_rate", 0.0) or 0.0)
    if dept_groups and (team_now or {}).get("scheduled_ended", 0):
        low = []
        for g in dept_groups:
            rate = float(g.get("attendance_rate", 0.0) or 0.0)
            gap_pts = (overall_rate - rate) * 100.0
            if gap_pts > 10.0 and int(g.get("scheduled_ended", 0) or 0) > 0:
                low.append({"department": g.get("group"), "attendance_rate": rate, "overall_rate": overall_rate, "gap_points": round(gap_pts, 1)})
        if low:
            low.sort(key=lambda x: -x["gap_points"])
            w = low[0]
            insights.append({
                "id": "low_department_rate",
                "text": f"{w['department'] or '(none)'} attendance is {w['gap_points']} points below overall ({round(w['attendance_rate']*100,1)}% vs {round(overall_rate*100,1)}%).",
                "numbers": {"departments": low, "overall_rate": overall_rate},
                "link": f"{base_link}?{q}&department={w['department'] or ''}",
                "effect": float(w["gap_points"]),
            })

    # 5) Recognition health.
    if recognition is not None and int(recognition.get("attempts", 0) or 0) > 0:
        attempts = int(recognition.get("attempts", 0) or 0)
        matched = int(recognition.get("matched", 0) or 0)
        rejected = int(recognition.get("rejected", 0) or 0)
        success_rate = float(recognition.get("success_rate", 0.0) or 0.0)
        backlog = int(recognition.get("backlog", 0) or 0)
        # highlight when success < 80% or backlog > 0 or rejected notable
        if success_rate < 0.8 or backlog > 0 or rejected >= 5:
            insights.append({
                "id": "recognition_health",
                "text": f"Recognition: {round(success_rate*100,1)}% success ({matched}/{attempts}); {rejected} rejected; review backlog {backlog}.",
                "numbers": {
                    "attempts": attempts, "matched": matched, "rejected": rejected,
                    "success_rate": success_rate, "rejected_by_day": recognition.get("rejected_by_day", []), "backlog": backlog,
                },
                "link": "review.html",
                "effect": float((1.0 - success_rate) * 50.0 + min(backlog, 20) + min(rejected, 20)),
            })

    insights.sort(key=lambda x: (-float(x.get("effect", 0) or 0), x.get("id") or ""))
    return insights[:5]
