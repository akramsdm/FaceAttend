"""Close open sessions missing a clock-out (Phase 2 / Step I).

Run at startup (wired in app.main lifespan) and on a schedule.
Proposed schedule: run every 30 minutes via OS task scheduler/cron, e.g.
  Windows Task Scheduler: action `.\.venv\Scripts\python.exe scripts\close_sessions.py`
  Linux cron: `*/30 * * * * cd /app && .venv/bin/python scripts/close_sessions.py`

Sessions are closed 4 hours after shift end (setting `missing_close_hours`,
default 4). The auto 'out' row is flagged `missing_clock_out` and audited;
missing clock-outs are never counted as worked time automatically. Managers
can enter the real time with a required reason via the admin approvals UI/API.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.approvals import close_missing_sessions  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402


def main() -> int:
    init_db()
    with SessionLocal() as session:
        closed = close_missing_sessions(session)
    print(f"Closed {len(closed)} missing-clockout session(s).")
    for c in closed:
        print(f"  staff={c['staff_id']} in={c['in_id']} out={c['out_id']} shift_date={c['shift_date']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
