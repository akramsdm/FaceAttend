from fastapi import FastAPI

app = FastAPI(title="Face Attendance System")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
