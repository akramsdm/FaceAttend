"""Signed single-use clock tokens bound to person and kiosk (Phase 2 / Step F)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ClockToken

TOKEN_TTL_SECONDS = 20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(mac)


@dataclass
class TokenStatus:
    ok: bool
    reason: str = "ok"
    jti: str | None = None
    staff_id: int | None = None
    kiosk_id: str | None = None


def issue_clock_token(db: Session, *, staff_id: int, kiosk_id: str, now: datetime | None = None,
                      ttl_seconds: int = TOKEN_TTL_SECONDS) -> tuple[str, ClockToken]:
    now = now or _utcnow()
    jti = secrets.token_hex(16)
    exp = now + timedelta(seconds=ttl_seconds)
    row = ClockToken(jti=jti, staff_id=staff_id, kiosk_id=kiosk_id, issued_at=now, expires_at=exp)
    db.add(row)
    db.flush()
    payload = f"{jti}.{staff_id}.{kiosk_id}.{int(exp.replace(tzinfo=timezone.utc).timestamp())}"
    sig = _sign(payload, get_settings().secret_key)
    token = f"{_b64url(payload.encode('utf-8'))}.{sig}"
    return token, row


def verify_clock_token(db: Session, token: str, *, kiosk_id: str, now: datetime | None = None) -> TokenStatus:
    now = now or _utcnow()
    try:
        body_b64, sig = token.rsplit(".", 1)
        payload = _b64url_decode(body_b64).decode("utf-8")
    except Exception:
        return TokenStatus(False, "invalid_token")
    if not hmac.compare_digest(sig, _sign(payload, get_settings().secret_key)):
        return TokenStatus(False, "invalid_token")
    try:
        jti, staff_s, kid, exp_s = payload.split(".", 3)
        staff_id = int(staff_s)
        exp_ts = int(exp_s)
    except Exception:
        return TokenStatus(False, "invalid_token")
    if kid != kiosk_id:
        return TokenStatus(False, "wrong_kiosk", jti=jti, staff_id=staff_id, kiosk_id=kid)
    row = db.get(ClockToken, jti)
    if row is None or row.staff_id != staff_id or row.kiosk_id != kid:
        return TokenStatus(False, "invalid_token", jti=jti, staff_id=staff_id, kiosk_id=kid)
    if row.used_at is not None:
        return TokenStatus(False, "used", jti=jti, staff_id=staff_id, kiosk_id=kid)
    if row.expires_at <= now or exp_ts <= int(now.replace(tzinfo=timezone.utc).timestamp()):
        return TokenStatus(False, "expired", jti=jti, staff_id=staff_id, kiosk_id=kid)
    return TokenStatus(True, "ok", jti=jti, staff_id=staff_id, kiosk_id=kid)


def consume_clock_token(db: Session, token: str, *, kiosk_id: str, now: datetime | None = None) -> TokenStatus:
    st = verify_clock_token(db, token, kiosk_id=kiosk_id, now=now)
    if not st.ok or st.jti is None:
        return st
    row = db.get(ClockToken, st.jti)
    if row is None:
        return TokenStatus(False, "invalid_token")
    row.used_at = now or _utcnow()
    db.flush()
    return st
