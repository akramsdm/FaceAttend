# Face Attendance System

A single-site face-recognition attendance web app for offices and workplaces.
Staff identify themselves at a kiosk by face, then explicitly **clock in or clock out** (Phase 2);
shifts set by managers control what actions are allowed. Administrators/managers enrol staff,
manage shifts, review attendance/approvals, resolve ambiguous cases, and manage settings.

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
- Kiosk clock page: <http://localhost:8000/static/signin.html>

The SQLite database (`attendance.db` by default) and tables are created at startup (idempotent
SQLite `ALTER`s/backfills; default shared shift patterns seeded). At startup the app also runs
review-frame cleanup and missing-clock-out session closing. `*.db`, `var/`, `eval_data/`,
`eval_results/`, `.env`, and model files are gitignored.

## Roles

- **admin** — full access (all staff/departments), settings/audit, staff CRUD/enrolment, attendance,
  review queue, approvals/missing clock-outs, and all shift/schedule management.
- **manager** — scoped to their `department_scope`: assign schedules and make day changes for
  in-scope staff; edit schedules they created and personal schedules of in-scope staff (the 5 shipped
  default shared schedules are admin-edit-only; managers can duplicate them); view resolved shifts/
  history for in-scope staff; review/decide approval requests and correct missing clock-outs for
  their department; cannot access out-of-scope staff.
- **staff** — interact only via the kiosk clock flow (no self-service login).

State-changing admin API calls require a server-side session cookie and `X-CSRF-Token`. Sessions
expire after `SESSION_TIMEOUT_MINUTES` and are lost on restart. Five failed logins lock the
account for `LOCKOUT_MINUTES`.

## Schedule concepts (Phase 3, People + Schedules)

- **Schedule (Pattern)** — a shared weekly template (`kind=shared`) with `PatternDay` rows (on/off,
  start/end, `Ends next day`/overnight) and windows `early_window_min` (Clock-in opens, default 30),
  `grace_min` (Counts as late after, default 10), `break_min` (default 0). Five default shared
  schedules are seeded (Office Mon–Fri 08:00–17:00, Early, Late, Weekend, Night 22:00–06:00
  overnight). Schedules are managed on the **Schedules** tab (Edit/Duplicate/Delete; Delete disabled
  while in use). Personal copies never appear on Schedules.
- **Assignment** — assigns a *shared* schedule to a person with an effective date range (Assignment
  only, no copy). Reassigning ends the previous assignment the day before (history preserved).
- **Copy-on-write (personal schedule)** — when a person's week differs from their assigned base
  schedule, saving creates a `kind=personal` copy (`based_on_pattern_id`, owner staff, `created_by`);
  **identical save = reset** to the base (no copy). Personal edits create effective-dated new
  versions (old archived). Unreferenced personal copies are garbage-collected
  (`scripts/cleanup_personal_patterns.py`); archived copies referenced by history/attendance are kept.
  Attendance rows are never modified.
- **Day change (exception, reason required)** — `day_off` or custom hours for a date (one exception
  per staff per day); overrides the schedule. Supports single date or a range (≤ 62 days). **Holiday
  (bulk day off)** can mark everyone or a department off for a date/range.
- **Resolution order** (first match wins; Phase 2 rules engine unchanged): day change (exception) →
  personal schedule → assigned shared schedule → no schedule. **Night shifts** belong to the date
  they **start**; hours/grouping use `shift_date` (never split across midnight). **Server clock in
  `TIMEZONE` only**. One shift per person per day.
- **Dry-run impact previews** — assign, save-week (People Edit week), bulk/range day change, and
  holiday return affected people, assignments that would end, exceptions that would be replaced, and
  a warning when dates already have attendance; **`dry_run=true` writes nothing** and confirm is
  disabled until previewed.
- **Delete guards / history** — delete is blocked while a schedule has current/future assignments
  (shows count) and when referenced by assignments/attendance history (history preserved; attendance
  never touched). People view shows a 7-day week strip (custom/changed badges) and a time-ordered
  history (assignments, personal versions, day changes).

## Clock flow (kiosk)

1. **Scan** — kiosk posts frames to `POST /api/scan` (quality + recognition; same logging/review
   behavior for rejected/ambiguous; optional head-turn liveness). On a match it returns the person’s
   name, today’s shift summary, `allowed_actions`/message (server-evaluated), and a signed **single-
   use clock token valid 20 seconds**, bound to the person and kiosk. Never returns scores or other
   names.
2. **Confirm** — the person chooses **Clock in** or **Clock out** (disabled actions show the reason;
   a 20s countdown resets on expiry). Clock out before shift end requires confirmation.
3. **Commit** — `POST /api/clock` verifies/consumes the token, **recomputes** shift rules on the
   server, and refuses anything not in `allowed_actions` (creates the attendance row with
   `shift_id`/`shift_date`/flag such as `late`, `early_departure`, `overtime`). Repeating the same
   token is idempotent (never a second row). After success the kiosk shows **Undo** for **10 seconds**
   (`POST /api/clock/undo`, sets `undone_at`, never deletes the row).
4. **Blocked** — when not allowed (`no_shift` / out of window / early out path), the kiosk offers
   **Request approval** (`POST /api/clock/request`); managers approve/reject in the Approvals queue
   (approval can create the attendance row with the appropriate flag/`approved_by`, audited).

**Missing clock-outs:** `scripts/close_sessions.py` (also run at startup) closes any open session
**4 hours after shift end** (setting `missing_close_hours`, default 4), writing an auto `out`
flagged `missing_clock_out` (audited, idempotent). Missing clock-outs are **never counted as worked
time automatically**; a manager enters the real time with a **required reason** (row kept, audited).
Proposed schedule: run `.\.venv\Scripts\python.exe scripts\close_sessions.py` every 30 minutes
(Task Scheduler/cron).

The legacy `POST /api/signin` (calendar-day in/out) is kept working for existing tests/clients; the
Phase 2 kiosk uses `/api/scan` + `/api/clock`.
## Kiosk full-screen instructions

1. Open <http://localhost:8000/static/signin.html> on the kiosk machine/browser.
2. Allow camera access (camera APIs require `localhost` or HTTPS).
3. Use the browser full-screen mode (F11 on Windows/Chrome/Edge) for an unattended kiosk display.
4. Flow: **scan** (camera loop → `/api/scan`, no overlapping requests) → on match, a **confirmation
   screen** shows name, shift summary and time with **Clock in / Clock out** (disabled actions show a
   reason, 20s token countdown resets on expiry; early clock-out asks for confirmation) → on success
   shows the result ~4s with **Undo** for 10s, then resets. Blocked cases can **Request approval** and
   show a reception contact line. The kiosk never displays scores or other staff names and sends
   `X-Kiosk-ID`.
5. Optional **head-turn liveness challenge** (admin setting `spoof_challenge`, default off): when
   enabled, a matched scan may request a random left/right turn and submit a second frame.
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
| Approvals & missing clock-outs | <http://localhost:8000/admin/approvals.html> |
| Shifts (patterns/assign/exceptions/preview) | <http://localhost:8000/admin/shifts/index.html> |
| Settings | <http://localhost:8000/admin/settings.html> |
| Audit log | <http://localhost:8000/admin/audit.html> |

## Settings

Live (DB-backed) settings are editable in the admin UI and take effect without restart (signin/
clock-related reads use live values where applicable): `match_threshold`, `ambiguity_margin`,
`cooldown_seconds`, `work_start`, `grace_minutes`, `review_retention_days`, `spoof_challenge`,
`missing_close_hours` (hours after shift end before an open session is auto-closed as missing
clock-out; default 4).

Shift-aware reporting: late = first clock-in after **shift start + grace** (falls back to global
`work_start`/`grace_minutes` when no resolved shift); monthly CSV adds early departure/overtime/
missing-clock-out flags and hours worked per shift (`in→out` minus break), grouped by `shift_date`
(night shifts not split). Dashboard counts present/absent **against the assigned shift** and shows
late/missing clock-out.

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
## Known limitations

- **Not certified anti-spoofing.** The optional head-turn challenge (landmark-based) reduces still-
  photo risk but may not stop a video replay; treat it as a liveness aid, not a security guarantee.
- **One shift per person per day.** No split shifts/rosters/leave management; resolution is
  exception -> personal -> assigned shared -> none.
- **Token & undo windows.** Scan issues a single-use clock token valid **20 seconds** (bound to
  person + kiosk); after a successful clock the kiosk allows **Undo for 10 seconds** (undo sets
  `undone_at`, never deletes).
- **Overtime margin is 0** in the clock flow (`evaluate(..., margin_overtime=0)`), so overtime is
  flagged at shift end.
- **Missing clock-outs are never counted as worked time automatically.** Auto-close writes a
  `missing_clock_out` out at the cut-off (audited) but it is excluded from hour pairing/totals; a
  manager must enter the real time with a required reason.
- **Sessions in memory.** Admin sessions/CSRF are in-process and do not survive restart.
- **No password change UI.** Rotate admin passwords by updating the DB (hash with the app's bcrypt).
- **SQLite `check_same_thread=False`.** Suitable for a single-site app; avoid heavy concurrent
  writers.
- **Legacy endpoint kept.** `POST /api/signin` (calendar-day in/out) remains for compatibility; the
  Phase 2 kiosk uses `/api/scan` + `/api/clock`.
- **Face model quality depends on enrolment images** and lighting/camera; ambiguous/no-match cases go
  to the review queue with saved frames (retention-limited).

## Recognition evaluation

The recognition evaluator uses the **same `Recognizer`/threshold path as the app**
(`scripts/evaluate.py` -> `app/eval_metrics.py`). In this environment, `eval_data/` contains **no
genuine/impostor pairs**, so runs produce `enrolled=0 genuine=0 impostor=0 rejected=0 FAR=0 FRR=0`
(`eval_results/20261108T181401Z/`, re-run `eval_results/20261109T113446Z/`); missing data exits `2`.
**No FAR/FRR results were fabricated and no baseline was altered; recognition logic/results are
unchanged by Phase 2/Phase 3/kiosk UI work** (no recognizer/threshold-path changes). To produce real
FAR/FRR, populate `eval_data/` and re-run `scripts/evaluate.py`.

## Everything not done (Phase 3 status / out of scope)

- **Step J (optional, not implemented):** read-only weekly calendar by department + "working today"
  list UI built from `/api/admin/shifts/people`. Manual QA of the new People/Schedules UI flows
  against a live browser/camera is not automated here.
- **`apply` of `scripts/cleanup_personal_patterns.py` on the real DB not run in this step** (dry-run
  reported 0 personal patterns; apply requires explicit confirmation).
- **Real recognition re-evaluation** (no labeled `eval_data/` here) — unchanged code path only;
  FAR/FRR not measured (re-run produced all zeros, results unchanged).
- **Docx sync** — Phase 2/3 `.docx/.md` guides left untracked as found.
- **Pushing to `origin/main`** (branch ahead locally; commits not pushed).
- **Out of scope (product):** split shifts, rosters, leave/swaps, staff self-service requests;
  multiple shifts per person per day; overtime margin configuration in UI (clock flow uses margin 0).
- **Production hardening (unchanged):** reverse proxy/HTTPS/cert management, persistent session store,
  Alembic migrations, SQLite-concurrency/WSGI model for heavier load, secret rotation.
- **Automated scheduling** of `scripts/close_sessions.py` (30-minute Task Scheduler/cron proposed,
  not installed by code).
- **Password change UI** / in-memory admin sessions (lost on restart).

## License

Internal / not specified. Add a `LICENSE` file before external distribution.
