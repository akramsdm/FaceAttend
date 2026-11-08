# Face Attendance System

A single-site face-recognition attendance web app for offices (university AI course project).
Staff sign in and out at a kiosk by looking at a webcam; administrators enrol staff, review
attendance and manage recognition settings.

## Stack

- Python 3.11+, FastAPI, uvicorn
- SQLAlchemy + SQLite (added in step 3)
- OpenCV YuNet (detection) and SFace (embeddings), pre-trained ONNX models
- Plain HTML/JavaScript served by FastAPI

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

```powershell
uvicorn app.main:app --reload
```

Health check: <http://localhost:8000/health> returns `{"status": "ok"}`.

The SQLite database (`attendance.db` by default) and its tables are created automatically at
startup. The file is ignored by git.

## Recognition spike

Download the pre-trained ONNX models (ignored by git), then run the standalone webcam check:

```powershell
python scripts/download_models.py
python scripts/spike.py
```

Press `e` to enrol the current face and `q` to quit. The spike prints detection and embedding
timings per frame, plus the cosine similarity to the enrolled face.

## Tests

```powershell
pytest -v
```

## Layout

```text
app/          FastAPI application and configuration
static/       HTML, CSS and JavaScript pages
models/       Downloaded OpenCV ONNX models (not committed)
scripts/      Setup, admin, evaluation and cleanup scripts
tests/        pytest tests
eval_data/    Local evaluation images (not committed)
var/          Runtime files such as temporary review frames
```

## Configuration

Settings are read from environment variables with development defaults:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./attendance.db` | SQLAlchemy database URL |
| `SECRET_KEY` | development-only placeholder | Session signing secret; set a real value outside development |
| `MATCH_THRESHOLD` | `0.363` | Minimum face-match score (SFace cosine similarity) |
| `COOLDOWN_SECONDS` | `60` | Ignore duplicate scans from the same person within this window |
| `MAX_FAILED_ATTEMPTS` | `5` | Failed admin logins before lockout |
| `REVIEW_RETENTION_DAYS` | `7` | Retention period for review-queue frames |
| `TIMEZONE` | `UTC` | Timezone used for the attendance day |
