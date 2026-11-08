from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.cleanup import cleanup_review_frames
from app.approvals import close_missing_sessions
from app.db import SessionLocal, init_db
from app.recognition import GalleryCache, Recognizer
from app.routers.attendance import router as attendance_router
from app.routers.auth import router as auth_router
from app.routers.review import router as review_router
from app.routers.approvals import router as approvals_router
from app.routers.clock import router as clock_router
from app.routers.settings import router as settings_router
from app.routers.shifts import router as shifts_router
from app.routers.signin import router as signin_router
from app.routers.staff import router as staff_router
from app.security import SessionStore
from app.settings_store import get_value

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    with SessionLocal() as session:
        cleanup_review_frames(session, int(get_value(session, "review_retention_days")))
        try:
            close_missing_sessions(session)
        except Exception:
            pass
    app.state.sessions = SessionStore()
    app.state.recognizer = Recognizer()
    app.state.gallery = GalleryCache()
    yield


app = FastAPI(title="Face Attendance System", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(staff_router)
app.include_router(shifts_router)
app.include_router(signin_router)
app.include_router(attendance_router)
app.include_router(review_router)
app.include_router(settings_router)
app.include_router(clock_router)
app.include_router(approvals_router)
app.mount(
    "/admin",
    StaticFiles(directory=ROOT / "static" / "admin", html=True),
    name="admin",
)
app.mount(
    "/static",
    StaticFiles(directory=ROOT / "static"),
    name="static",
)



@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
