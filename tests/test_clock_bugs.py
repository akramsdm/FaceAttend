from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.clock_tokens import issue_clock_token
from app.main import app
from app.models import Admin, ApprovalRequest, Assignment, Attendance, Pattern, Staff
from app.routers import clock as clock_router
from app.routers import signin as signin_router
from app.routers.shifts import seed_default_patterns
from app.security import CSRF_HEADER_NAME, hash_password
from app.settings_store import set_values
from app.recognition import FrameAnalysis
import numpy as np

ADMIN_PW = "correct horse battery staple"


class FakeRecognizer:
    def __init__(self): self.model_name="SFace"; self.model_version="2021dec"; self.analysis=None; self.embedding=None
    def analyse_frame(self, image_bytes):
        if self.analysis is not None: return self.analysis
        return FrameAnalysis(status="ok", frame=np.zeros((16,16,3),dtype=np.uint8), face_count=1,
                             face=np.zeros(15,dtype=np.float32), box=(0,0,16,16), landmarks=np.zeros((5,2),dtype=np.float32))
    def embed(self, frame, face):
        return self.embedding if self.embedding is not None else np.array([1.0,0.0], dtype=np.float32)


class FakeGallery:
    def __init__(self): self.best_id=None; self.best_score=None; self.second_score=None
    def match_embedding(self, embedding): return self.best_id, self.best_score, self.second_score


@pytest.fixture()
def env(client: TestClient, session_factory: sessionmaker[Session]):
    app.dependency_overrides[clock_router.get_now] = lambda: datetime(2026,1,5,18,0,0)
    rec = FakeRecognizer(); gal = FakeGallery()
    app.dependency_overrides[signin_router.get_recognizer] = lambda: rec
    app.dependency_overrides[signin_router.get_gallery] = lambda: gal
    app.dependency_overrides[signin_router.get_now] = lambda: datetime(2026,1,5,18,0,0)
    with session_factory() as s:
        seed_default_patterns(s)
        set_values(s, {"spoof_challenge": False, "allow_reentry_same_shift": False})
        s.add(Admin(username="alice", password_hash=hash_password(ADMIN_PW), role="admin"))
        s.commit()
    return client, rec, gal


def add_staff(session_factory, code="S1", name="Ada", dept="Eng"):
    with session_factory() as s:
        st = Staff(staff_code=code, name=name, department=dept, active=True); s.add(st); s.commit(); return st.id


def assign_office(session_factory, staff_id):
    with session_factory() as s:
        office = s.scalar(select(Pattern).where(Pattern.name=="Office hours", Pattern.is_default.is_(True)))
        s.add(Assignment(staff_id=staff_id, pattern_id=office.id, effective_from=datetime(2026,1,1).date(), effective_to=None))
        s.commit(); return office.id


def make_token(session_factory, staff_id, kiosk="k1", issued_at=None, ttl=20):
    issued_at = issued_at or datetime(2026,1,5,18,0,0)
    with session_factory() as s:
        tok,_=issue_clock_token(s, staff_id=staff_id, kiosk_id=kiosk, now=issued_at, ttl_seconds=ttl); s.commit(); return tok


def scan(client, kiosk="k1"):
    return client.post("/api/scan", headers={"X-Kiosk-ID":kiosk}, files={"image":("f.jpg",b"x","image/jpeg")})


def hdrs(client):
    r=client.post("/api/admin/login", json={"username":"alice","password":ADMIN_PW}); assert r.status_code==200
    return {CSRF_HEADER_NAME: r.json()["csrf_token"]}


# --- Bug 1: completed session -> shift_already_completed; stale token 409; reentry setting; approval reentry ---

def test_scan_after_completed_shift_blocked(env, session_factory):
    client,_,gal = env
    sid = add_staff(session_factory); pid = assign_office(session_factory, sid)
    with session_factory() as s:
        s.add(Attendance(staff_id=sid,type="in",timestamp=datetime(2026,1,5,8,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.add(Attendance(staff_id=sid,type="out",timestamp=datetime(2026,1,5,17,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.commit()
    gal.best_id=sid; gal.best_score=0.95; gal.second_score=0.2
    app.dependency_overrides[signin_router.get_now]=lambda: datetime(2026,1,5,18,0,0)
    r = scan(client)
    assert r.status_code==200
    d=r.json(); assert d["status"]=="ok"
    assert d["allowed_actions"]==[]
    assert d["message"]=="shift_already_completed"


def test_stale_token_after_completed_shift_returns_409(env, session_factory):
    client,_,gal = env
    sid = add_staff(session_factory); pid = assign_office(session_factory, sid)
    gal.best_id=sid; gal.best_score=0.95; gal.second_score=0.2
    # token issued while shift open
    app.dependency_overrides[signin_router.get_now]=lambda: datetime(2026,1,5,8,30,0)
    tok = scan(client).json()["token"]
    # complete the shift
    with session_factory() as s:
        s.add(Attendance(staff_id=sid,type="in",timestamp=datetime(2026,1,5,8,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.add(Attendance(staff_id=sid,type="out",timestamp=datetime(2026,1,5,17,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.commit()
    app.dependency_overrides[clock_router.get_now]=lambda: datetime(2026,1,5,18,0,0)
    r = client.post("/api/clock", json={"token":tok,"action":"in"}, headers={"X-Kiosk-ID":"k1"})
    assert r.status_code==409
    detail=r.json()["detail"]
    assert detail["message"]=="shift_already_completed"
    assert detail["reason_code"]=="reentry"
    # no second session row created by stale token
    with session_factory() as s:
        n=s.scalar(select(func.count()).select_from(Attendance).where(Attendance.staff_id==sid, Attendance.edit_reason.like("token:%"))) or 0
        assert n==0


def test_allow_reentry_setting_allows_second_in(env, session_factory):
    client,_,gal = env
    sid = add_staff(session_factory); pid = assign_office(session_factory, sid)
    with session_factory() as s:
        set_values(s, {"allow_reentry_same_shift": True}); s.commit()
        s.add(Attendance(staff_id=sid,type="in",timestamp=datetime(2026,1,5,8,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.add(Attendance(staff_id=sid,type="out",timestamp=datetime(2026,1,5,17,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.commit()
    gal.best_id=sid; gal.best_score=0.95; gal.second_score=0.2
    app.dependency_overrides[signin_router.get_now]=lambda: datetime(2026,1,5,18,0,0)
    r = scan(client)
    assert r.status_code==200
    assert "in" in r.json()["allowed_actions"]


def test_reentry_approval_creates_row(env, session_factory):
    client,_,_ = env
    sid = add_staff(session_factory); pid = assign_office(session_factory, sid)
    with session_factory() as s:
        s.add(Attendance(staff_id=sid,type="in",timestamp=datetime(2026,1,5,8,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.add(Attendance(staff_id=sid,type="out",timestamp=datetime(2026,1,5,17,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        req=ApprovalRequest(staff_id=sid,action="in",reason_code="reentry",requested_at=datetime(2026,1,5,18,0,0),status="pending")
        s.add(req); s.commit(); rid=req.id
    h=hdrs(client)
    r=client.post(f"/api/admin/approvals/{rid}/approve", json={"note":"ok"}, headers=h)
    assert r.status_code==200, r.text
    with session_factory() as s:
        rows=list(s.scalars(select(Attendance).where(Attendance.staff_id==sid, Attendance.type=="in")).all())
        assert len(rows)==2


# --- Bug 2: out for open out-of-window approved in (prev evening) inherits in shift_date, no early-departure flag ---

def test_out_for_prev_evening_out_of_window_in_inherits_shift_date_no_flag(env, session_factory):
    client,_,gal = env
    sid = add_staff(session_factory); pid = assign_office(session_factory, sid)
    # approved out-of-window in previous evening, no shift resolved at creation -> null shift/flag in that context,
    # but it carries shift_date (from that local day) and represents an open session. Simulate as stored.
    prev_sd = datetime(2026,1,4).date()  # Sunday/no-shift day; open session anchored there
    with session_factory() as s:
        s.add(Attendance(staff_id=sid,type="in",timestamp=datetime(2026,1,4,22,0,0),method="manual",
                         shift_id=None, shift_date=prev_sd, flag="out_of_window", approved_by=1))
        s.commit()
    gal.best_id=sid; gal.best_score=0.95; gal.second_score=0.2
    # clock out next morning (Mon) - must use open session's shift_date (prev_sd), null shift -> no early-departure flag
    app.dependency_overrides[clock_router.get_now]=lambda: datetime(2026,1,5,6,0,0)
    tok = make_token(session_factory, sid, issued_at=datetime(2026,1,5,6,0,0))
    r = client.post("/api/clock", json={"token":tok,"action":"out","confirm":True}, headers={"X-Kiosk-ID":"k1"})
    assert r.status_code==200, r.text
    att = r.json()["attendance"]
    assert att["shift_date"] == prev_sd.isoformat()
    assert att["flag"] is None  # no shift -> no early-departure flag
    # pair shares one shift_date
    with session_factory() as s:
        outs = list(s.scalars(select(Attendance).where(Attendance.staff_id==sid, Attendance.type=="out", Attendance.flag.is_(None))).all())
        assert any(o.shift_date == prev_sd for o in outs)


# --- night shift still pairs correctly (shift_id stored, inherits open session shift_date) ---

def test_night_shift_out_inherits_open_session_and_pairs(env, session_factory):
    client,_,gal = env
    sid = add_staff(session_factory)
    from datetime import time, date
    with session_factory() as s:
        p = Pattern(name="NightT", kind="shared", early_window_min=30, grace_min=10, break_min=30, is_default=False)
        s.add(p); s.flush()
        for wd in range(7): s.add(__import__('app.models',fromlist=['PatternDay']).PatternDay(pattern_id=p.id, weekday=wd, start_time=time(22,0), end_time=time(6,0), overnight=True, enabled=True))
        s.add(Assignment(staff_id=sid, pattern_id=p.id, effective_from=date(2026,1,1), effective_to=None))
        s.commit(); pid=p.id
    with session_factory() as s:
        s.add(Attendance(staff_id=sid,type="in",timestamp=datetime(2026,1,5,22,0,0),method="face",shift_id=pid,shift_date=datetime(2026,1,5).date()))
        s.commit()
    gal.best_id=sid; gal.best_score=0.95; gal.second_score=0.2
    app.dependency_overrides[clock_router.get_now]=lambda: datetime(2026,1,6,5,50,0)
    tok = make_token(session_factory, sid, issued_at=datetime(2026,1,6,5,50,0))
    r = client.post("/api/clock", json={"token":tok,"action":"out","confirm":True}, headers={"X-Kiosk-ID":"k1"})
    assert r.status_code==200, r.text
    att=r.json()["attendance"]
    assert att["shift_date"] == "2026-01-05"  # inherits open session's shift_date
    assert att["shift_id"] == pid
