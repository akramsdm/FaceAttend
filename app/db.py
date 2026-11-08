"""Database engine, session factory and startup table creation."""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_engine(url: str, **engine_options) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, **engine_options)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", enable_sqlite_foreign_keys)
    return engine


engine = create_db_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    from app import models  # noqa: F401  registers the tables on Base.metadata

    Base.metadata.create_all(bind=engine)
    # SQLite: add Phase 2 columns on existing DBs (create_all won't alter tables)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            def _cols(table: str) -> set[str]:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                return {r[1] for r in rows}

            if "admins" in {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}:
                ac = _cols("admins")
                if "role" not in ac:
                    conn.exec_driver_sql("ALTER TABLE admins ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'admin'")
                if "department_scope" not in ac:
                    conn.exec_driver_sql("ALTER TABLE admins ADD COLUMN department_scope VARCHAR(100)")
                conn.exec_driver_sql("UPDATE admins SET role='admin' WHERE role IS NULL OR role=''")

            if "attendance" in {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}:
                at = _cols("attendance")
                for col, ddl in [
                    ("shift_id", "ALTER TABLE attendance ADD COLUMN shift_id INTEGER REFERENCES patterns(id)"),
                    ("shift_date", "ALTER TABLE attendance ADD COLUMN shift_date DATE"),
                    ("flag", "ALTER TABLE attendance ADD COLUMN flag VARCHAR(30)"),
                    ("approved_by", "ALTER TABLE attendance ADD COLUMN approved_by INTEGER REFERENCES admins(id)"),
                    ("undone_at", "ALTER TABLE attendance ADD COLUMN undone_at DATETIME"),
                ]:
                    if col not in at:
                        conn.exec_driver_sql(ddl)

    # seed default patterns idempotently
    from app.models import Pattern, PatternDay
    from datetime import time as _time
    from sqlalchemy import select as _select
    defaults = [
        ("Office hours", [(wd, _time(8, 0), _time(17, 0), False) for wd in range(0, 5)], 30, 10, 60),
        ("Early", [(wd, _time(6, 0), _time(14, 0), False) for wd in range(0, 5)], 30, 10, 30),
        ("Late", [(wd, _time(14, 0), _time(22, 0), False) for wd in range(0, 5)], 30, 10, 30),
        ("Weekend", [(5, _time(9, 0), _time(17, 0), False), (6, _time(9, 0), _time(17, 0), False)], 30, 10, 30),
        ("Night", [(wd, _time(22, 0), _time(6, 0), True) for wd in range(0, 7)], 30, 10, 30),
    ]
    with SessionLocal() as session:
        existing = {n for (n,) in session.execute(_select(Pattern.name).where(Pattern.owner_staff_id.is_(None))).all()}
        for name, days, early, grace, brk in defaults:
            if name in existing:
                continue
            p = Pattern(name=name, owner_staff_id=None, early_window_min=early, grace_min=grace, break_min=brk, is_default=True)
            session.add(p); session.flush()
            for wd, st, en, ov in days:
                session.add(PatternDay(pattern_id=p.id, weekday=wd, start_time=st, end_time=en, overnight=ov, enabled=True))
        session.commit()
