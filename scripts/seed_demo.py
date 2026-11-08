"""Demo seed: create staff records only from names passed in (no fake faces)."""
import argparse
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Staff  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create staff records from names you provide (no fake faces, no embeddings)."
    )
    parser.add_argument("names", nargs="*", help="Staff names to create")
    parser.add_argument("--prefix", default="DEMO", help="Staff code prefix (default: DEMO)")
    args = parser.parse_args(argv)

    if not args.names:
        parser.print_usage()
        print("Provide at least one name. Example: python scripts/seed_demo.py \"Ada Lovelace\" \"Grace Hopper\"")
        return 2

    init_db()
    created = 0
    with SessionLocal() as session:
        # find next available index for prefix
        existing = session.scalars(select(Staff.staff_code).where(Staff.staff_code.like(f"{args.prefix}%"))).all()
        used = set(existing)
        idx = 1
        for name in args.names:
            name = name.strip()
            if not name:
                continue
            # skip duplicate name (by exact name) to keep demo idempotent-ish
            if session.scalar(select(Staff).where(Staff.name == name, Staff.anonymised.is_(False))) is not None:
                print(f"skip (exists): {name}")
                continue
            while True:
                code = f"{args.prefix}{idx:03d}"
                idx += 1
                if code not in used:
                    used.add(code)
                    break
            session.add(Staff(staff_code=code, name=name, active=True))
            print(f"created: {code}  {name}")
            created += 1
        session.commit()
    print(f"Done. Created {created} staff record(s). No faces/embeddings were added.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
