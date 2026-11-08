"""Password hashing, server-side sessions and admin authorization dependencies."""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import Admin, utcnow

SESSION_COOKIE_NAME = "fa_session"
CSRF_HEADER_NAME = "X-CSRF-Token"

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def as_naive_utc(value: datetime) -> datetime:
    """Normalise timestamps read from SQLite, which drops timezone information."""

    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class AdminSession:
    token: str
    admin_id: int
    csrf_token: str
    expires_at: datetime


class SessionStore:
    """Opaque session tokens kept in server memory, never in the cookie."""

    def __init__(
        self,
        ttl: timedelta | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._ttl = ttl or timedelta(minutes=get_settings().session_timeout_minutes)
        self._clock = clock
        self._sessions: dict[str, AdminSession] = {}
        self._lock = Lock()

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    def create(self, admin_id: int) -> AdminSession:
        session = AdminSession(
            token=secrets.token_urlsafe(32),
            admin_id=admin_id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=self._clock() + self._ttl,
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> AdminSession | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expires_at <= self._clock():
                del self._sessions[token]
                return None
            return session

    def delete(self, token: str | None) -> AdminSession | None:
        if not token:
            return None
        with self._lock:
            return self._sessions.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "sessions", None)
    if store is None:
        store = SessionStore()
        request.app.state.sessions = store
    return store


def get_current_session(
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> AdminSession:
    session = store.get(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def _admin_for(session: AdminSession, db: Session) -> Admin:
    admin = db.get(Admin, session.admin_id)
    if admin is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return admin


def require_admin(
    session: AdminSession = Depends(get_current_session),
    db: Session = Depends(get_session),
) -> Admin:
    return _admin_for(session, db)


def require_admin_csrf(
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER_NAME),
    session: AdminSession = Depends(get_current_session),
    db: Session = Depends(get_session),
) -> Admin:
    if not csrf_token or not secrets.compare_digest(csrf_token, session.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return _admin_for(session, db)
