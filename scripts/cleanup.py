"""Delete review frames older than the retention period."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cleanup import cleanup_review_frames  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.settings_store import get_value  # noqa: E402


def main() -> int:
    init_db()
    with SessionLocal() as session:
        days = get_value(session, "review_retention_days")
        deleted = cleanup_review_frames(session, int(days))
    print(f"Deleted {deleted} expired review frame(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
