from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.recognition import GalleryCache, Recognizer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    app.state.recognizer = Recognizer()
    app.state.gallery = GalleryCache()
    yield


app = FastAPI(title="Face Attendance System", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
