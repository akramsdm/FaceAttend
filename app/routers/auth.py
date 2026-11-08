"""Admin login, logout and session status endpoints."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.db import get_session
from app.models import Admin, utcnow
from app.security import (
    SESSION_COOKIE_NAME,
    AdminSession,
    SessionStore,
    as_naive_utc,
    get_current_session,
    get_session_store,
    require_admin,
    require_admin_csrf,
    verify_password,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, repr=False)


class AdminInfo(BaseModel):
    id: int
    username: str


class LoginResponse(BaseModel):
    admin: AdminInfo
    csrf_token: str


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_session),
    store: SessionStore = Depends(get_session_store),
) -> LoginResponse:
    settings = get_settings()
    admin = db.scalar(select(Admin).where(Admin.username == payload.username))
    if admin is None:
        record_audit(db, None, "login_failed", payload.username, "unknown username")
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid username or password")

    now = utcnow()
    if admin.locked_until is not None:
        if now < as_naive_utc(admin.locked_until):
            record_audit(db, admin.id, "login_failed", admin.username, "account locked")
            db.commit()
            raise HTTPException(
                status_code=423,
                detail="Account locked; try again later",
            )
        admin.locked_until = None
        admin.failed_logins = 0

    if not verify_password(admin.password_hash, payload.password):
        admin.failed_logins += 1
        detail = (
            f"failed attempt {admin.failed_logins} of {settings.max_failed_attempts}"
        )
        if admin.failed_logins >= settings.max_failed_attempts:
            admin.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            detail = f"{detail}; locked for {settings.lockout_minutes} minutes"
        record_audit(db, admin.id, "login_failed", admin.username, detail)
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid username or password")

    admin.failed_logins = 0
    admin.locked_until = None
    session = store.create(admin.id)
    record_audit(db, admin.id, "login", admin.username)
    db.commit()

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.token,
        max_age=int(store.ttl.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return LoginResponse(
        admin=AdminInfo(id=admin.id, username=admin.username),
        csrf_token=session.csrf_token,
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    admin: Admin = Depends(require_admin_csrf),
    db: Session = Depends(get_session),
    store: SessionStore = Depends(get_session_store),
) -> dict[str, str]:
    store.delete(request.cookies.get(SESSION_COOKIE_NAME))
    record_audit(db, admin.id, "logout", admin.username)
    db.commit()

    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me")
def me(
    admin: Admin = Depends(require_admin),
    session: AdminSession = Depends(get_current_session),
) -> dict[str, AdminInfo | str]:
    return {
        "admin": AdminInfo(id=admin.id, username=admin.username),
        "csrf_token": session.csrf_token,
    }
