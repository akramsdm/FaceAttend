from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.recognition import GalleryCache, Recognizer
from app.routers.auth import router as auth_router
from app.routers.signin import router as signin_router
from app.routers.staff import router as staff_router
from app.security import SessionStore

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    app.state.sessions = SessionStore()
    app.state.recognizer = Recognizer()
    app.state.gallery = GalleryCache()
    yield


app = FastAPI(title="Face Attendance System", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(staff_router)
app.include_router(signin_router)
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
