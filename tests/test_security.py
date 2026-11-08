from datetime import datetime, timedelta

from app.security import SessionStore, hash_password, verify_password


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def test_correct_password_verifies() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password(password_hash, "correct horse battery staple") is True


def test_wrong_password_does_not_verify() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password(password_hash, "wrong password") is False


def test_each_hash_is_salted() -> None:
    first = hash_password("same password")
    second = hash_password("same password")

    assert first != second
    assert verify_password(first, "same password") is True
    assert verify_password(second, "same password") is True


def test_invalid_hash_does_not_verify() -> None:
    assert verify_password("not-an-argon2-hash", "anything") is False


def test_session_store_create_get_and_delete() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, 0))
    store = SessionStore(ttl=timedelta(minutes=30), clock=clock)

    session = store.create(admin_id=7)

    assert session.admin_id == 7
    assert session.token
    assert session.csrf_token
    assert session.token != session.csrf_token
    assert store.get(session.token) == session
    assert store.get(None) is None
    assert store.get("unknown") is None

    assert store.delete(session.token) == session
    assert store.get(session.token) is None
    assert store.delete(session.token) is None


def test_session_expires_after_ttl() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, 0))
    store = SessionStore(ttl=timedelta(minutes=30), clock=clock)
    session = store.create(admin_id=7)

    clock.advance(timedelta(minutes=29, seconds=59))
    assert store.get(session.token) == session

    clock.advance(timedelta(seconds=1))
    assert store.get(session.token) is None
    assert store.get(session.token) is None


def test_clear_removes_all_sessions() -> None:
    store = SessionStore(ttl=timedelta(minutes=30))
    first = store.create(admin_id=1)
    second = store.create(admin_id=2)

    store.clear()

    assert store.get(first.token) is None
    assert store.get(second.token) is None
