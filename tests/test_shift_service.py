"""Phase 3 Step D: copy-on-write/reset/GC service + endpoint contract tests."""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Assignment, Attendance, AuditLog, Pattern, PatternDay, Staff
from app.security import hash_password


def mk_staff(session_factory, code="S1", name="S One", dept="Eng"):
    with session_factory() as s:
        st = Staff(staff_code=code, name=name, department=dept)
        s.add(st)
        s.commit()
        return st.id


def shared_pattern(session_factory, name="Base", *, days=None):
    days = days or [
        (wd, "08:00", "17:00", False, True) for wd in range(0, 5)
    ]
    with session_factory() as s:
        p = Pattern(
            name=name,
            owner_staff_id=None,
            early_window_min=30,
            grace_min=10,
            break_min=60,
            is_default=False,
            kind="shared",
        )
        s.add(p)
        s.flush()
        for wd, st, en, ov, enb in days:
            hh, mm = st.split(":")
            eh, em = en.split(":")
            from datetime import time

            s.add(
                PatternDay(
                    pattern_id=p.id,
                    weekday=wd,
                    start_time=time(int(hh), int(mm)),
                    end_time=time(int(eh), int(em)),
                    overnight=ov,
                    enabled=enb,
                )
            )
        s.commit()
        return p.id


def week(days=None, early=30, grace=10, brk=60):
    days = days or [
        {"weekday": wd, "start_time": "08:00", "end_time": "17:00", "overnight": False, "enabled": True}
        for wd in range(0, 5)
    ]
    return days, {"early_window_min": early, "grace_min": grace, "break_min": brk}


def personal_count(session_factory, staff_id):
    with session_factory() as s:
        return s.scalar(
            select(func.count()).select_from(Pattern).where(Pattern.owner_staff_id == staff_id)
        )


def test_assign_does_not_create_personal_row(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "A1", "A", "Eng")
    pid = shared_pattern(session_factory, "OfficeA")
    before = session_factory and 0
    with session_factory() as s:
        before = s.scalar(select(func.count()).select_from(Pattern)) or 0
    r = authenticated_client.post(
        "/api/admin/shifts/assignments",
        json={"pattern_id": pid, "staff_ids": [sid], "effective_from": "2026-01-05"},
    )
    assert r.status_code == 200 and r.json()["assigned"] == 1
    with session_factory() as s:
        after = s.scalar(select(func.count()).select_from(Pattern)) or 0
        personal = s.scalars(
            select(Pattern).where(Pattern.owner_staff_id.isnot(None))
        ).all()
        asn = s.scalar(
            select(Assignment).where(Assignment.staff_id == sid, Assignment.pattern_id == pid)
        )
    assert after == before  # no new pattern rows
    assert personal == []
    assert asn is not None and asn.effective_to is None


def test_get_patterns_returns_only_shared(authenticated_client: TestClient, session_factory):
    sid = mk_staff(session_factory, "G1", "G", "Eng")
    base_id = shared_pattern(session_factory, "SharedOnly")
    # create a personal row directly
    with session_factory() as s:
        p = Pattern(
            name="SharedOnly (personal:G1)",
            owner_staff_id=sid,
            early_window_min=30,
            grace_min=10,
            break_min=60,
            is_default=False,
            kind="personal",
            based_on_pattern_id=base_id,
        )
        s.add(p)
        s.commit()
    r = authenticated_client.get("/api/admin/shifts/patterns")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()}
    kinds = {p.get("kind") for p in r.json()}
    assert base_id in ids
    # personal must not appear
    assert all(p.get("owner_staff_id") is None for p in r.json())
    assert "personal" not in kinds


def test_identical_save_is_reset(session_factory):
    from app.shift_service import save_personal_week

    sid = mk_staff(session_factory, "R1", "R", "Eng")
    base_id = shared_pattern(session_factory, "BaseR")
    # give staff an existing personal (different) first, then identical save => reset
    diff_days, diff_w = week(
        [{"weekday": wd, "start_time": "09:00", "end_time": "18:00", "overnight": False, "enabled": True}
         for wd in range(0, 5)]
    )
    with session_factory() as s:
        res = save_personal_week(s, sid, diff_days, diff_w, date(2026, 1, 5), base_pattern_id=base_id)
        s.commit()
        assert res["status"] == "personal_saved"
        personal_id = res["pattern_id"]
    assert personal_count(session_factory, sid) == 1

    same_days, same_w = week()  # identical to base (Mon-Fri 08:00-17:00, 30/10/60)
    with session_factory() as s:
        res2 = save_personal_week(s, sid, same_days, same_w, date(2026, 2, 1), base_pattern_id=base_id)
        s.commit()
        assert res2["status"] == "reset"
        assert res2["pattern_id"] == base_id
        # base assigned as current
        active = s.scalars(
            select(Assignment).where(Assignment.staff_id == sid, Assignment.effective_to.is_(None))
        ).all()
        prev = s.get(Pattern, personal_id)
        # closed assignment to personal remains (history) => personal referenced, archived+kept
        closed_ref = s.scalars(
            select(Assignment).where(Assignment.staff_id == sid, Assignment.pattern_id == personal_id)
        ).all()
    assert len(active) == 1 and active[0].pattern_id == base_id
    assert prev is not None and prev.archived_at is not None  # kept: referenced by assignment history
    assert len(closed_ref) == 1 and closed_ref[0].effective_to is not None


def test_different_save_creates_one_personal_row(session_factory):
    from app.shift_service import save_personal_week

    sid = mk_staff(session_factory, "D1", "D", "Eng")
    base_id = shared_pattern(session_factory, "BaseD")
    days, w = week(
        [{"weekday": wd, "start_time": "07:00", "end_time": "15:00", "overnight": False, "enabled": True}
         for wd in range(0, 5)]
    )
    with session_factory() as s:
        res = save_personal_week(s, sid, days, w, date(2026, 1, 5), base_pattern_id=base_id)
        s.commit()
    assert res["status"] == "personal_saved"
    assert personal_count(session_factory, sid) == 1
    with session_factory() as s:
        p = s.get(Pattern, res["pattern_id"])
        assert p is not None and p.owner_staff_id == sid and p.kind == "personal"
        assert p.based_on_pattern_id == base_id
        a = s.scalar(
            select(Assignment).where(Assignment.staff_id == sid, Assignment.effective_to.is_(None))
        )
        assert a is not None and a.pattern_id == p.id and str(a.effective_from) == "2026-01-05"


def test_second_edit_archives_first(session_factory):
    from app.shift_service import save_personal_week

    sid = mk_staff(session_factory, "E1", "E", "Eng")
    base_id = shared_pattern(session_factory, "BaseE")
    days1, w = week(
        [{"weekday": wd, "start_time": "07:00", "end_time": "15:00", "overnight": False, "enabled": True}
         for wd in range(0, 5)]
    )
    days2, _ = week(
        [{"weekday": wd, "start_time": "10:00", "end_time": "18:00", "overnight": False, "enabled": True}
         for wd in range(0, 5)]
    )
    with session_factory() as s:
        r1 = save_personal_week(s, sid, days1, w, date(2026, 1, 5), base_pattern_id=base_id)
        s.commit()
        first_id = r1["pattern_id"]
        # make first referenced so it can't be gc'd
        s.add(Attendance(staff_id=sid, type="in", method="manual", shift_id=first_id))
        s.commit()
        r2 = save_personal_week(s, sid, days2, w, date(2026, 2, 1), base_pattern_id=base_id)
        s.commit()
        second_id = r2["pattern_id"]
    assert first_id != second_id
    with session_factory() as s:
        first = s.get(Pattern, first_id)
        second = s.get(Pattern, second_id)
        assert first is not None and first.archived_at is not None  # referenced by attendance => archived, kept
        assert second is not None and second.archived_at is None
        a = s.scalar(select(Assignment).where(Assignment.staff_id == sid, Assignment.effective_to.is_(None)))
        assert a.pattern_id == second_id
        all_personal = s.scalars(select(Pattern).where(Pattern.owner_staff_id == sid)).all()
    assert {p.id for p in all_personal} == {first_id, second_id}


def test_reset_removes_personal_copy(session_factory):
    from app.shift_service import save_personal_week, reset_to_schedule

    sid = mk_staff(session_factory, "X1", "X", "Eng")
    base_id = shared_pattern(session_factory, "BaseX")
    alt_id = shared_pattern(session_factory, "AltX", days=[(wd, "06:00", "14:00", False, True) for wd in range(0, 5)])
    days, w = week(
        [{"weekday": wd, "start_time": "09:00", "end_time": "17:00", "overnight": False, "enabled": True}
         for wd in range(0, 5)]
    )
    with session_factory() as s:
        res = save_personal_week(s, sid, days, w, date(2026, 1, 5), base_pattern_id=base_id)
        personal_id = res["pattern_id"]
        s.commit()
    assert personal_count(session_factory, sid) == 1
    with session_factory() as s:
        out = reset_to_schedule(s, sid, alt_id, date(2026, 3, 1))
        s.commit()
    assert out["status"] == "reset"
    with session_factory() as s:
        a = s.scalar(select(Assignment).where(Assignment.staff_id == sid, Assignment.effective_to.is_(None)))
        prev = s.get(Pattern, personal_id)
    assert a is not None and a.pattern_id == alt_id
    # closed assignment to personal remains (history) => archived+kept (not unreferenced)
    assert prev is not None and prev.archived_at is not None


def test_gc_keeps_referenced_and_deletes_unreferenced(session_factory):
    from app.shift_service import gc_personal_patterns

    sid = mk_staff(session_factory, "GC1", "GC", "Eng")
    base_id = shared_pattern(session_factory, "BaseGC")
    with session_factory() as s:
        keep = Pattern(name="k", owner_staff_id=sid, early_window_min=30, grace_min=10, break_min=0, is_default=False, kind="personal", based_on_pattern_id=base_id)
        drop = Pattern(name="d", owner_staff_id=sid, early_window_min=30, grace_min=10, break_min=0, is_default=False, kind="personal", based_on_pattern_id=base_id)
        s.add_all([keep, drop]); s.flush()
        s.add(Assignment(staff_id=sid, pattern_id=keep.id, effective_from=date(2026, 1, 1), effective_to=None))
        s.commit()
        drop_id, keep_id = drop.id, keep.id
        report = gc_personal_patterns(s, staff_id=sid)
        s.commit()
    actions = {r["id"]: r["action"] for r in report}
    assert actions.get(drop_id) == "deleted"
    assert actions.get(keep_id) == "kept"
    with session_factory() as s:
        assert s.get(Pattern, drop_id) is None
        assert s.get(Pattern, keep_id) is not None


def test_history_preserved(session_factory):
    """Assignment/pattern history preserved: referenced archived versions kept, unreferenced removed."""
    from app.shift_service import save_personal_week, reset_to_schedule

    sid = mk_staff(session_factory, "H1", "H", "Eng")
    base_id = shared_pattern(session_factory, "BaseH")
    days1, w = week(
        [{"weekday": wd, "start_time": "07:00", "end_time": "15:00", "overnight": False, "enabled": True}
         for wd in range(0, 5)]
    )
    days2, _ = week(
        [{"weekday": wd, "start_time": "10:00", "end_time": "18:00", "overnight": False, "enabled": True}
         for wd in range(0, 5)]
    )
    with session_factory() as s:
        r1 = save_personal_week(s, sid, days1, w, date(2026, 1, 1), base_pattern_id=base_id)
        s.commit(); v1 = r1["pattern_id"]
        # reference v1 so history is preserved (attendance)
        s.add(Attendance(staff_id=sid, type="in", method="manual", shift_id=v1))
        s.commit()
        r2 = save_personal_week(s, sid, days2, w, date(2026, 2, 1), base_pattern_id=base_id)
        s.commit(); v2 = r2["pattern_id"]
        reset_to_schedule(s, sid, base_id, date(2026, 3, 1))
        s.commit()
        # assignments history (closed/kept rows)
        asns = s.scalars(select(Assignment).where(Assignment.staff_id == sid).order_by(Assignment.id)).all()
        pats = {p.id: p for p in s.scalars(select(Pattern).where(Pattern.id.in_([v1, v2, base_id]))).all()}
    # v1 referenced by attendance => preserved+archived; v2 referenced by a closed
    # assignment (history) => preserved+archived (history kept); base exists.
    assert v1 in pats and pats[v1].archived_at is not None
    assert v2 in pats and pats[v2].archived_at is not None
    assert base_id in pats
    # assignment history rows exist covering v1 and v2 and base
    assert {a.pattern_id for a in asns} >= {v1, v2, base_id}
    # current assignment is base
    current = [a for a in asns if a.effective_to is None]
    assert len(current) == 1 and current[0].pattern_id == base_id
