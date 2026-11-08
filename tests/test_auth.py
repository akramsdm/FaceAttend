from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_session
from app.main import app
from app.models import Admin, AuditLog, utcnow
from app.security import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SessionStore,
    hash_password,
)

PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "wrong password"


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    def override_get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.state.sessions = SessionStore(ttl=timedelta(minutes=30))
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()
    if hasattr(app.state, "sessions"):
        del app.state.sessions


@pytest.fixture()
def admin_id(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        admin = Admin(username="alice", password_hash=hash_password(PASSWORD))
        session.add(admin)
        session.commit()
        return admin.id


def login(
    client: TestClient,
    username: str = "alice",
    password: str = PASSWORD,
):
    return client.post(
        "/api/admin/login",
        json={"username": username, "password": password},
    )


def audit_entries(
    session_factory: sessionmaker[Session],
) -> list[AuditLog]:
    with session_factory() as session:
        return list(session.scalars(select(AuditLog).order_by(AuditLog.id)))


def test_correct_login_sets_cookie_and_writes_audit(
    client: TestClient,
    admin_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    response = login(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["admin"]["id"] == admin_id
    assert payload["admin"]["username"] == "alice"
    assert payload["admin"].get("role", "admin") == "admin"
    assert payload["csrf_token"]

    set_cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert client.cookies.get(SESSION_COOKIE_NAME)

    entries = audit_entries(session_factory)
    assert [(entry.action, entry.admin_id) for entry in entries] == [
        ("login", admin_id)
    ]
    assert entries[0].target == "alice"
    assert PASSWORD not in (entries[0].detail or "")


def test_wrong_password_is_rejected_and_counted(
    client: TestClient,
    admin_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    response = login(client, password=WRONG_PASSWORD)

    assert response.status_code == 401
    assert client.cookies.get(SESSION_COOKIE_NAME) is None

    with session_factory() as session:
        admin = session.get(Admin, admin_id)
        assert admin is not None
        assert admin.failed_logins == 1
        assert admin.locked_until is None

    entries = audit_entries(session_factory)
    assert [(entry.action, entry.admin_id) for entry in entries] == [
        ("login_failed", admin_id)
    ]
    assert WRONG_PASSWORD not in (entries[0].detail or "")


def test_unknown_username_is_rejected_and_audited(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = login(client, username="nobody")

    assert response.status_code == 401
    entries = audit_entries(session_factory)
    assert len(entries) == 1
    assert entries[0].action == "login_failed"
    assert entries[0].admin_id is None
    assert entries[0].target == "nobody"


def test_lockout_then_unlock_after_expiry(
    client: TestClient,
    admin_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    for _ in range(5):
        assert login(client, password=WRONG_PASSWORD).status_code == 401

    with session_factory() as session:
        admin = session.get(Admin, admin_id)
        assert admin is not None
        assert admin.failed_logins == 5
        assert admin.locked_until is not None
        remaining = admin.locked_until - utcnow()
        assert timedelta(minutes=4, seconds=50) < remaining <= timedelta(minutes=5)

    assert login(client).status_code == 423
    assert client.cookies.get(SESSION_COOKIE_NAME) is None

    with session_factory() as session:
        admin = session.get(Admin, admin_id)
        assert admin is not None
        admin.locked_until = utcnow() - timedelta(seconds=1)
        session.commit()

    assert login(client).status_code == 200

    with session_factory() as session:
        admin = session.get(Admin, admin_id)
        assert admin is not None
        assert admin.failed_logins == 0
        assert admin.locked_until is None


def test_route_without_session_returns_401(client: TestClient) -> None:
    assert client.post("/api/admin/logout").status_code == 401
    assert client.get("/api/admin/me").status_code == 401


def test_missing_csrf_returns_403(
    client: TestClient,
    admin_id: int,
    session_factory: sessionmaker[Session],
) -> None:
    assert login(client).status_code == 200
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token

    assert client.post("/api/admin/logout").status_code == 403
    assert (
        client.post(
            "/api/admin/logout", headers={CSRF_HEADER_NAME: "wrong"}
        ).status_code
        == 403
    )
    assert client.get("/api/admin/me").status_code == 200

    csrf_token = client.get("/api/admin/me").json()["csrf_token"]
    response = client.post(
        "/api/admin/logout", headers={CSRF_HEADER_NAME: csrf_token}
    )

    assert response.status_code == 200
    assert client.cookies.get(SESSION_COOKIE_NAME) is None

    client.cookies.clear()
    assert (
        client.get(
            "/api/admin/me", cookies={SESSION_COOKIE_NAME: token}
        ).status_code
        == 401
    )

    assert [entry.action for entry in audit_entries(session_factory)] == [
        "login",
        "logout",
    ]
