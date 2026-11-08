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

## Admin account

Create an administrator. The username is echoed, but the password is read with `getpass` and is
never printed:

```powershell
python scripts/create_admin.py
```

Passwords must be at least 12 characters and are hashed with argon2. The admin API is:

| Method | Path | Session | CSRF | Meaning |
| --- | --- | --- | --- | --- |
| `POST` | `/api/admin/login` | no | no | Start a session; returns the CSRF token |
| `GET` | `/api/admin/me` | yes | no | Current admin and CSRF token |
| `POST` | `/api/admin/logout` | yes | yes | End the session |

Send the token from login or `/api/admin/me` in the `X-CSRF-Token` header on every state-changing
admin request. Sessions are kept in server memory, expire after `SESSION_TIMEOUT_MINUTES`, and are
lost when the server restarts. Five failed logins lock the account for `LOCKOUT_MINUTES`.

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
| `SECRET_KEY` | development-only placeholder | Reserved for future signed values; sessions use random opaque tokens |
| `MATCH_THRESHOLD` | `0.363` | Minimum face-match score (SFace cosine similarity) |
| `AMBIGUITY_MARGIN` | `0.05` | If the best two people are closer than this, treat the result as ambiguous |
| `COOLDOWN_SECONDS` | `60` | Ignore duplicate scans from the same person within this window |
| `MAX_FAILED_ATTEMPTS` | `5` | Failed admin logins before lockout |
| `LOCKOUT_MINUTES` | `5` | How long an admin account stays locked |
| `SESSION_TIMEOUT_MINUTES` | `30` | Server-side admin session lifetime |
| `SESSION_COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS |
| `REVIEW_RETENTION_DAYS` | `7` | Retention period for review-queue frames |
| `TIMEZONE` | `UTC` | Timezone used for the attendance day |
| `MIN_FACE_WIDTH` | `120` | Reject detected faces narrower than this many pixels |
| `BLUR_THRESHOLD` | `100.0` | Reject face crops with a Laplacian variance below this value |
