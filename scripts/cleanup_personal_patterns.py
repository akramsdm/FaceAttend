"""Clean up personal shift patterns (Phase 3 / Step C).

Default is --dry-run (no writes). Pass --apply to write changes.

For each personal pattern (kind='personal' or owner_staff_id set):
- If its days and windows are identical to its base shared pattern: repoint its
  assignments to the base and delete the copy. If the copy is referenced by
  attendance.shift_id it is KEPT (attendance rows are never touched) and only
  assignments are repointed.
- If different from its base (a genuine custom week): keep it as kind='personal'.
- If nothing references it (no assignments, no attendance rows): delete it.

Prints a table of every decision and the reason. Never touches attendance rows.
Records each applied action in AuditLog.

Run on a COPY of the database first (use --db). Do not apply until confirmed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.audit import record_audit  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Assignment, Attendance, Pattern, PatternDay  # noqa: E402


def _day_sig(days: list[PatternDay]) -> list[tuple]:
    return sorted(
        (
            d.weekday,
            d.start_time.strftime("%H:%M:%S") if d.start_time else None,
            d.end_time.strftime("%H:%M:%S") if d.end_time else None,
            bool(d.overnight),
            bool(d.enabled),
        )
        for d in days
    )


def _sig(p: Pattern) -> tuple:
    return (p.early_window_min, p.grace_min, p.break_min, _day_sig(list(p.days)))


def _resolve_base(db: Session, p: Pattern) -> Pattern | None:
    if p.based_on_pattern_id:
        base = db.get(Pattern, p.based_on_pattern_id)
        if base is not None and base.owner_staff_id is None:
            return base
    if " (personal:" in (p.name or ""):
        base_name = p.name.split(" (personal:", 1)[0]
        return db.scalar(
            select(Pattern).where(
                Pattern.name == base_name, Pattern.owner_staff_id.is_(None)
            )
        )
    return None


def _counts(db: Session, pid: int) -> tuple[int, int]:
    asn = db.scalar(select(Assignment).where(Assignment.pattern_id == pid).with_only_columns(
        __import__("sqlalchemy").func.count()
    ).order_by(None))
    # use explicit count queries
    from sqlalchemy import func

    asn = db.scalar(select(func.count()).select_from(Assignment).where(Assignment.pattern_id == pid)) or 0
    att = db.scalar(select(func.count()).select_from(Attendance).where(Attendance.shift_id == pid)) or 0
    return int(asn), int(att)


def cleanup(db: Session, *, apply: bool) -> list[dict]:
    personals = list(
        db.scalars(
            select(Pattern).where(
                (Pattern.kind == "personal") | (Pattern.owner_staff_id.isnot(None))
            ).order_by(Pattern.id)
        ).all()
    )
    report: list[dict] = []
    for p in personals:
        # refresh relationship
        _ = list(p.days)
        asn, att = _counts(db, p.id)
        base = _resolve_base(db, p)
        row = {
            "id": p.id,
            "name": p.name,
            "owner_staff_id": p.owner_staff_id,
            "base": base.id if base else None,
            "base_name": base.name if base else None,
            "assignments": asn,
            "attendance_refs": att,
        }
        if asn == 0 and att == 0:
            row["decision"] = "DELETE"
            row["reason"] = "unreferenced (no assignments, no attendance rows)"
            if apply:
                record_audit(
                    db, None, "cleanup_delete_personal_pattern",
                    f"pattern:{p.id}", f"unreferenced; base={row['base_name'] or row['base']}",
                )
                db.delete(p)
        elif base is not None and _sig(p) == _sig(base):
            repointed = 0
            if asn:
                for a in db.scalars(select(Assignment).where(Assignment.pattern_id == p.id)).all():
                    a.pattern_id = base.id
                    repointed += 1
            if att:
                row["decision"] = "REPOINT_ASSIGNMENTS_KEEP"
                row["reason"] = (
                    f"identical to base '{base.name}' (id {base.id}); repointed {repointed} assignment(s); "
                    f"KEPT copy because {att} attendance row(s) reference it (attendance never touched)"
                )
                if apply and repointed:
                    record_audit(
                        db, None, "cleanup_repoint_assignments",
                        f"pattern:{p.id}", f"repointed {repointed} assignment(s) -> base pattern:{base.id} ({base.name}); copy kept (attendance refs={att})",
                    )
            else:
                row["decision"] = "REPOINT_ASSIGNMENTS_DELETE"
                row["reason"] = (
                    f"identical to base '{base.name}' (id {base.id}); repointed {repointed} assignment(s); deleted copy"
                )
                if apply:
                    if repointed:
                        record_audit(
                            db, None, "cleanup_repoint_assignments",
                            f"pattern:{p.id}", f"repointed {repointed} assignment(s) -> base pattern:{base.id} ({base.name})",
                        )
                    record_audit(
                        db, None, "cleanup_delete_personal_pattern",
                        f"pattern:{p.id}", f"identical to base:{base.id} ({base.name}); no attendance refs",
                    )
                    db.delete(p)
        else:
            why = "no base shared pattern resolved" if base is None else f"days/windows differ from base '{base.name}' (id {base.id})"
            row["decision"] = "KEEP_PERSONAL"
            row["reason"] = f"genuine custom week kept as kind='personal' ({why})"
            if apply and p.kind != "personal":
                p.kind = "personal"
                record_audit(db, None, "cleanup_mark_personal", f"pattern:{p.id}", why)
        report.append(row)
    if apply:
        db.commit()
    return report


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("No personal patterns found.")
        return
    cols = ["id", "name", "base", "assignments", "attendance_refs", "decision", "reason"]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    # cap reason for readability
    widths["reason"] = min(widths["reason"], 90)
    widths["name"] = min(widths["name"], 32)
    def fmt(r):
        vals = []
        for c in cols:
            v = str(r.get(c, ""))
            if c in ("reason", "name"):
                v = (v[: widths[c] - 1] + "…") if len(v) > widths[c] else v
            vals.append(v.ljust(widths[c]))
        return " | ".join(vals)
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for r in rows:
        print(fmt(r))


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean up personal patterns (dry-run default).")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    ap.add_argument("--db", help="SQLite database path (default: app configured DATABASE_URL).")
    args = ap.parse_args()
    apply = bool(args.apply)
    if args.db:
        engine = create_engine(f"sqlite+pysqlite:///{args.db}", connect_args={"check_same_thread": False})
    else:
        engine = create_engine(get_settings().database_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    mode = "APPLY (writing)" if apply else "DRY-RUN (no writes)"
    print(f"cleanup_personal_patterns: {mode}")
    print(f"database: {args.db or get_settings().database_url}")
    with SessionLocal() as db:
        report = cleanup(db, apply=apply)
    print()
    _print_table(report)
    print()
    print(f"summary: {len(report)} personal pattern(s); "
          f"{sum(1 for r in report if 'DELETE' in r['decision'])} delete, "
          f"{sum(1 for r in report if 'REPOINT' in r['decision'])} repoint, "
          f"{sum(1 for r in report if r['decision']=='KEEP_PERSONAL')} keep-personal.")
    if not apply:
        print("No changes written (dry-run). Re-run with --apply to commit and record AuditLog entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
