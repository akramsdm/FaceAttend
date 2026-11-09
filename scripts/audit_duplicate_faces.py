"""Read-only audit: compare every staff member's embeddings with every other.

Prints pairs whose best cosine score reaches the live match_threshold (with
both staff codes, the score, and whether each is active), sorted by score.
Also prints pairs that are close but below it (within 0.05) as "near matches".

Never writes to the database. Does not delete or change any enrolment.

Usage:
    .\.venv\Scripts\python.exe scripts/audit_duplicate_faces.py [--db sqlite:///./attendance.db]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only duplicate-face audit (no DB writes).")
    ap.add_argument("--db", default=None, help="SQLAlchemy database URL (default: env DATABASE_URL or sqlite:///./attendance.db)")
    args = ap.parse_args(argv)

    if args.db:
        os.environ["DATABASE_URL"] = args.db
    os.environ.setdefault("APP_ENV", "development")

    import numpy as np
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.models import FaceEmbedding, Staff
    from app.rules import _cosine_similarity, _normalised
    from app.settings_store import get_value, DEFINITIONS

    settings = get_settings()
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {})
    Session = sessionmaker(bind=engine, future=True)

    with Session() as db:
        # live threshold (read-only); fall back to config default if unset
        try:
            threshold = float(get_value(db, "match_threshold"))
        except Exception:
            threshold = float(settings.match_threshold)

        rows = db.execute(
            select(
                FaceEmbedding.staff_id,
                FaceEmbedding.vector,
                Staff.staff_code,
                Staff.name,
                Staff.active,
                Staff.anonymised,
            ).join(Staff, Staff.id == FaceEmbedding.staff_id)
        ).all()

    engine.dispose()

    # group by staff (include inactive; note anonymised separately)
    by: dict[int, dict] = {}
    for staff_id, vector, code, name, active, anonymised in rows:
        e = by.setdefault(int(staff_id), {"staff_id": int(staff_id), "code": code, "name": name, "active": bool(active), "anonymised": bool(anonymised), "vecs": []})
        e["vecs"].append(_normalised(np.asarray(vector, dtype=np.float32)))

    staff = list(by.values())
    print(f"duplicate-face audit (READ-ONLY, no DB writes)")
    print(f"database: {settings.database_url}")
    print(f"match_threshold (live): {threshold}")
    print(f"staff with embeddings compared: {len(staff)} (including inactive; anonymised flagged)")
    print()

    pairs = []
    for i in range(len(staff)):
        for j in range(i + 1, len(staff)):
            a, b = staff[i], staff[j]
            best = -1.0
            for va in a["vecs"]:
                for vb in b["vecs"]:
                    s = float(np.dot(va, vb))  # cosine on normalised
                    if s > best:
                        best = s
            pairs.append((best, a, b))
    pairs.sort(key=lambda x: x[0], reverse=True)

    near_margin = 0.05
    dups = [(s, a, b) for (s, a, b) in pairs if s >= threshold]
    nears = [(s, a, b) for (s, a, b) in pairs if (threshold - near_margin) <= s < threshold]

    def fmt(score, a, b):
        def side(x):
            flags = []
            flags.append("active" if x["active"] else "inactive")
            if x["anonymised"]:
                flags.append("anonymised")
            return f"{x['code']} ({','.join(flags)})"
        return f"  score={score:.6f}  {side(a)}  <->  {side(b)}  |  {a['name']} <-> {b['name']}"

    print(f"== DUPLICATES (best score >= {threshold}) [{len(dups)} pair(s)] ==")
    if dups:
        for s, a, b in dups:
            print(fmt(s, a, b))
    else:
        print("  (none)")
    print()
    print(f"== NEAR MATCHES (within {near_margin} below threshold: [{threshold - near_margin:.6f}, {threshold:.6f})) [{len(nears)} pair(s)] ==")
    if nears:
        for s, a, b in nears:
            print(fmt(s, a, b))
    else:
        print("  (none)")
    print()
    print("No database writes performed. No enrolments deleted or changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
