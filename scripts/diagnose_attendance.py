"""Diagnose historical attendance lifecycle inconsistencies (read-only by default).

Reports:
  * orphan clock-outs (an ``out`` with no matching ``in``)
  * employees with more than one open session
  * stale ``open_key`` values on already-closed sessions
  * open sessions still relying on the legacy (NULL ``open_key``) fallback
  * clock-outs without a ``paired_in_id`` link

Nothing is deleted or rewritten unless ``--backfill-links`` is supplied, and
even then only NULL linkage columns are populated for unambiguous pairs.

Usage:
    .\\.venv\\Scripts\\python.exe scripts\\diagnose_attendance.py
    .\\.venv\\Scripts\\python.exe scripts\\diagnose_attendance.py --days 365
    .\\.venv\\Scripts\\python.exe scripts\\diagnose_attendance.py --backfill-links
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.attendance_service import find_open_session  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Attendance, Staff  # noqa: E402
from app.routers.attendance import _pair_sessions  # noqa: E402


def _load(days: int):
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(Attendance)
                .where(Attendance.timestamp >= cutoff)
                .order_by(Attendance.staff_id, Attendance.timestamp, Attendance.id)
            ).all()
        )
        staff = {s.id: s for s in db.scalars(select(Staff)).all()}
    return rows, staff


def _label(staff: dict[int, Staff], staff_id: int) -> str:
    s = staff.get(staff_id)
    return f"{staff_id}:{s.name}" if s else str(staff_id)


def diagnose(days: int) -> dict:
    rows, staff = _load(days)
    zone = ZoneInfo(get_settings().timezone)
    paired = _pair_sessions(rows, zone)

    orphans = [(sid, sd, out_r) for sid, sd, in_r, out_r in paired if in_r is None and out_r is not None]
    legacy_open = [(sid, sd, in_r) for sid, sd, in_r, out_r in paired if in_r is not None and out_r is None and in_r.open_key is None]
    open_sessions = [(sid, sd, in_r) for sid, sd, in_r, out_r in paired if in_r is not None and out_r is None]
    negative_sessions = [
        (sid, sd, in_r, out_r)
        for sid, sd, in_r, out_r in paired
        if in_r is not None and out_r is not None and out_r.timestamp < in_r.timestamp
    ]
    missing_links = [
        (sid, sd, in_r, out_r)
        for sid, sd, in_r, out_r in paired
        if in_r is not None and out_r is not None and out_r.paired_in_id is None
    ]

    explicit_open: dict[str, list[Attendance]] = defaultdict(list)
    for r in rows:
        if r.type == "in" and r.open_key is not None and r.undone_at is None:
            explicit_open[r.open_key].append(r)
    duplicate_open = {k: v for k, v in explicit_open.items() if len(v) > 1}

    # An open_key is stale when a paired clock-out already exists.
    closed_by = defaultdict(list)
    for r in rows:
        if r.type == "out" and r.paired_in_id is not None and r.undone_at is None:
            closed_by[r.paired_in_id].append(r)
    stale_open_key = [
        r for r in rows
        if r.type == "in" and r.open_key is not None and r.id in closed_by
    ]

    return {
        "rows": rows,
        "staff": staff,
        "paired": paired,
        "orphans": orphans,
        "legacy_open": legacy_open,
        "open_sessions": open_sessions,
        "negative_sessions": negative_sessions,
        "missing_links": missing_links,
        "duplicate_open": duplicate_open,
        "stale_open_key": stale_open_key,
    }


def backfill_links(report: dict) -> int:
    changed = 0
    with SessionLocal() as db:
        for sid, sd, in_r, out_r in report["missing_links"]:
            out_row = db.get(Attendance, out_r.id)
            if out_row is not None and out_row.paired_in_id is None:
                out_row.paired_in_id = in_r.id
                changed += 1
        for sid, sd, in_r, out_r in report["paired"]:
            if in_r is None or out_r is not None or in_r.open_key is not None:
                continue
            open_now = find_open_session(db, sid)
            if open_now is not None and open_now.id == in_r.id:
                row = db.get(Attendance, in_r.id)
                if row is not None and row.open_key is None:
                    row.open_key = str(sid)
                    changed += 1
        db.commit()
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90, help="look-back window (default 90)")
    parser.add_argument(
        "--backfill-links",
        action="store_true",
        help="populate NULL paired_in_id/open_key for unambiguous pairs (never deletes rows)",
    )
    args = parser.parse_args()

    init_db()
    report = diagnose(args.days)
    staff = report["staff"]
    print(f"Scanned {len(report['rows'])} attendance rows in the last {args.days} day(s).")

    orphans = report["orphans"]
    print(f"\nOrphan clock-outs: {len(orphans)}")
    for sid, sd, out_r in orphans[:50]:
        print(f"  out id={out_r.id} staff={_label(staff, sid)} shift_date={sd} at={out_r.timestamp}")

    print(f"\nOpen sessions on legacy fallback (NULL open_key): {len(report['legacy_open'])}")
    for sid, sd, in_r in report["legacy_open"][:50]:
        print(f"  in id={in_r.id} staff={_label(staff, sid)} shift_date={sd} at={in_r.timestamp}")

    print(f"\nAll open sessions (awaiting clock-out or correction): {len(report['open_sessions'])}")
    for sid, sd, in_r in report["open_sessions"][:50]:
        print(
            f"  in id={in_r.id} staff={_label(staff, sid)} shift_date={sd} "
            f"at={in_r.timestamp} open_key={in_r.open_key}"
        )

    print(f"\nNegative sessions (clock-out before clock-in): {len(report['negative_sessions'])}")
    for sid, sd, in_r, out_r in report["negative_sessions"][:50]:
        print(
            f"  in id={in_r.id} at={in_r.timestamp} -> out id={out_r.id} at={out_r.timestamp} "
            f"staff={_label(staff, sid)} shift_date={sd}"
        )

    print(f"\nEmployees with duplicate explicit open sessions: {len(report['duplicate_open'])}")
    for key, items in report["duplicate_open"].items():
        print(f"  open_key={key} ids={[r.id for r in items]}")

    print(f"\nStale open_key on closed sessions: {len(report['stale_open_key'])}")
    for r in report["stale_open_key"][:50]:
        print(f"  in id={r.id} staff={_label(staff, r.staff_id)} open_key={r.open_key}")

    print(f"\nClock-outs missing paired_in_id link: {len(report['missing_links'])}")
    for sid, sd, in_r, out_r in report["missing_links"][:50]:
        print(f"  out id={out_r.id} should pair with in id={in_r.id} staff={_label(staff, sid)} shift_date={sd}")

    if args.backfill_links:
        changed = backfill_links(report)
        print(f"\nBackfilled {changed} linkage column(s); no rows were deleted.")
    else:
        print("\nNo changes made. Re-run with --backfill-links to populate safe NULL links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
