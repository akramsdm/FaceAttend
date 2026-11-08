# Face Attendance System

A single-site face-recognition attendance web app for offices and workplaces.
Staff sign in and out at a kiosk by looking at a webcam; administrators enrol staff, review
attendance, resolve ambiguous cases, and manage settings.

## Stack

- Python 3.11+, FastAPI, uvicorn
- SQLAlchemy + SQLite
- OpenCV YuNet (detection) and SFace (embeddings), pre-trained ONNX models
- Plain HTML/JavaScript served by FastAPI

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Download models

The ONNX models are required and are **not committed** (ignored under `models/*.onnx`):

```powershell
python scripts/download_models.py
```

This downloads `models/face_detection_yunet_2023mar.onnx` and
`models/face_recognition_sface_2021dec.onnx`. The app and evaluation script load the same
Recognizer path and will fail fast if a model file is missing.

## Create an admin

Usernames are echoed; passwords are read with `getpass` and never printed. Passwords must be at
least 12 characters and are hashed with argon2:

```powershell
python scripts/create_admin.py
```

## Run

```powershell
$env:APP_ENV = "development"   # local default
uvicorn app.main:app --reload
```

- Health: <http://localhost:8000/health> -> `{"status":"ok"}`
- Admin UI: <http://localhost:8000/admin/>
- Kiosk sign-in page: <http://localhost:8000/static/signin.html>

The SQLite database (`attendance.db` by default) and tables are created at startup. `*.db`,
`var/`, `eval_data/`, `eval_results/`, `.env`, and model files are gitignored.

## Kiosk full-screen instructions

1. Open <http://localhost:8000/static/signin.html> on the kiosk machine/browser.
2. Allow camera access (camera APIs require `localhost` or HTTPS).
3. Use the browser full-screen mode (F11 on Windows/Chrome/Edge) for an unattended kiosk display.
4. The kiosk captures a frame periodically (no overlapping requests), shows a 4s result, and never
   displays scores or other staff names. It sends `X-Kiosk-ID` with each `POST /api/signin`.
5. Optional **head-turn liveness challenge** (admin setting `spoof_challenge`, default off): when
   enabled, a matched sign-in may request a random left/right turn and submit a second frame.
   Landmark-based confirmation helps stop a **still photo** but is **not certified anti-spoofing**
   and **may not stop a video** replay.

## Admin interface

| Page | URL |
| --- | --- |
| Sign in | <http://localhost:8000/admin/login.html> |
| Dashboard / staff | <http://localhost:8000/admin/> |
| Enrolment | <http://localhost:8000/admin/enrol.html> |
| Attendance | <http://localhost:8000/admin/attendance.html> |
| Review queue | <http://localhost:8000/admin/review.html> |
| Settings | <http://localhost:8000/admin/settings.html> |
| Audit log | <http://localhost:8000/admin/audit.html> |

State-changing admin API calls require a server-side session cookie and `X-CSRF-Token`. Sessions
expire after `SESSION_TIMEOUT_MINUTES` and are lost on restart. Five failed logins lock the
account for `LOCKOUT_MINUTES`.

## Settings

Live (DB-backed) settings are editable in the admin UI and take effect without restart (signin
reads match/ambiguity/cooldown live): `match_threshold`, `ambiguity_margin`, `cooldown_seconds`,
`work_start`, `grace_minutes`, `review_retention_days`, `spoof_challenge`.

Environment variables (development defaults):

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_ENV` | `development` | `development`/`dev`/`test`/`local` allow the dev default secret; other values require a real `SECRET_KEY` |
| `DATABASE_URL` | `sqlite:///./attendance.db` | SQLAlchemy database URL |
| `SECRET_KEY` | dev placeholder (dev only) | Reserved for future signed values; **must be set to a non-default value outside development** |
| `MATCH_THRESHOLD` | `0.363` | Minimum face-match score (SFace cosine similarity) |
| `AMBIGUITY_MARGIN` | `0.05` | Best-vs-second gap below this is treated as ambiguous |
| `COOLDOWN_SECONDS` | `60` | Duplicate scan window for the same person |
| `MAX_FAILED_ATTEMPTS` | `5` | Failed admin logins before lockout |
| `LOCKOUT_MINUTES` | `5` | Admin lockout duration |
| `SESSION_TIMEOUT_MINUTES` | `30` | Server-side admin session lifetime |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` when serving over HTTPS |
| `REVIEW_RETENTION_DAYS` | `7` | Retention for review-queue frames (startup/cleanup) |
| `TIMEZONE` | `UTC` | Attendance day timezone |
| `MIN_FACE_WIDTH` | `120` | Minimum detected face width (px) |
| `BLUR_THRESHOLD` | `100.0` | Laplacian variance threshold for blur |
| `WORK_START` | `09:00` | Late boundary base (monthly report) |
| `GRACE_MINUTES` | `15` | Late grace (monthly report) |
| `MAX_UPLOAD_BYTES` | `5242880` | Upload size limit (kiosk image/challenge, enrolment images) |

## How to run tests

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

The 6 `tests/test_recognition.py` skips are expected when real fixture images/models are not
available in that context. Warnings are from `fastapi`/`starlette` `TestClient` deprecations.

## How to run the evaluation

Evaluation uses the **same** `app.recognition.Recognizer` analyse/embed path as the app and does
**not** invent images or results.

Input layout (gitignored):

```text
eval_data/<person>/enrol/*.jpg
eval_data/<person>/<condition>/*.jpg   (e.g. normal, dim, backlit, glasses, angle, other_day)
eval_data/spoof/<person>/*.jpg         (optional)
```

Run:

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate --help
.\.venv\Scripts\python.exe -m scripts.evaluate --data eval_data --out eval_results --steps 101
```

Outputs in `eval_results/<timestamp>/`: `results.csv`, `sweep.csv`, `metrics.json` (includes the
lowest threshold where FAR is 0 on the sample and FRR at that point), `score_histogram.png`
(genuine vs impostor), `far_frr_curve.png`. Probe images failing quality checks are listed
separately and excluded from scoring. If `--data` is missing, the script exits with code 2 and
states that no results were invented.

## Demo seed (no fake faces)

Creates staff records only from names you pass in (no images, no embeddings):

```powershell
python scripts/seed_demo.py "Ada Lovelace" "Grace Hopper" --prefix DEMO
```

## Privacy / security notes

- Enrolment/sign-in uploads are read in memory and **not saved** (review frames are saved only for
  rejected/ambiguous attempts per the review-queue threshold/retention rules). Uploads are
  size-limited (`MAX_UPLOAD_BYTES`).
- No secrets or embeddings/vectors are logged or printed by app/scripts. Passwords are hashed
  (argon2); the create-admin script never prints passwords.
- `SECRET_KEY` has no insecure default outside development (`APP_ENV` not a dev/test value raises).
- Staff deletion anonymises the record, removes embeddings and consent, and keeps attendance
  history (per data model).

## Known limitations

- **Small test sample:** recognition quality/thresholds depend on real evaluation data; automated
  tests use mocks/synthetic cases and do not certify real-world accuracy.
- **Lighting sensitivity:** detection/embedding quality degrades in dim/backlit or poor camera
  conditions; quality checks (face size/blur) reduce but do not eliminate this.
- **No certified anti-spoofing:** the optional head-turn challenge helps against still photos but
  is not certified and may not stop video replay or sophisticated attacks.
- **Single site:** designed for one office/site (single SQLite DB, in-memory sessions, local kiosk
  assumptions), not multi-site/high-availability deployment.

## Layout

```text
app/          FastAPI application, routers, models, recognition/rules
static/       HTML, CSS and JavaScript (admin + kiosk)
models/       Downloaded OpenCV ONNX models (not committed)
scripts/      download_models, create_admin, seed_demo, evaluate, cleanup, spike
tests/        pytest tests
eval_data/    Local evaluation images (not committed)
eval_results/ Evaluation outputs (not committed)
var/          Runtime files such as temporary review frames
```
