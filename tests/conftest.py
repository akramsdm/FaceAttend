from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  registers the tables before create_all
from app.db import Base, create_db_engine, get_session
from app.main import app
from app.models import Admin
from app.security import CSRF_HEADER_NAME, SessionStore, hash_password

ADMIN_PASSWORD = "correct horse battery staple"


@pytest.fixture()
def engine() -> Iterator[Engine]:
    database_engine = create_db_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool
    )
    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


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
    for attribute in ("sessions", "recognizer", "gallery"):
        if hasattr(app.state, attribute):
            delattr(app.state, attribute)


@pytest.fixture()
def admin_id(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        admin = Admin(username="alice", password_hash=hash_password(ADMIN_PASSWORD))
        session.add(admin)
        session.commit()
        return admin.id


@pytest.fixture()
def admin_password() -> str:
    return ADMIN_PASSWORD


@pytest.fixture()
def auth_headers(client: TestClient, admin_id: int) -> dict[str, str]:
    response = client.post(
        "/api/admin/login",
        json={"username": "alice", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    return {CSRF_HEADER_NAME: response.json()["csrf_token"]}


@pytest.fixture()
def authenticated_client(
    client: TestClient, auth_headers: dict[str, str]
) -> TestClient:
    client.headers.update(auth_headers)
    return client
