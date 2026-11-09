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

## Enrolment and duplicate-face protection

Admins enrol 3–5 frames per staff member (`POST /api/admin/staff/{id}/enrol`, `app/routers/staff.py`,
checks in `app/enrolment_checks.py`; captured images are not saved). Per-image quality checks:
decode, exactly one face (`no_face`/`multiple_faces`), minimum face width (`MIN_FACE_WIDTH`, default
120), blur (Laplacian variance vs `BLUR_THRESHOLD`, default 100.0), then SFace embedding (L2-normalized;
cosine similarity, higher = more similar). At least 3 acceptable images are required.

Safety checks run **before** deleting/replacing the target’s existing embeddings; on any failure
**nothing is deleted or stored, no Consent row, no commit, gallery unchanged**, and an audit entry
is written (ids only, never vectors/scores):
- **Mixed-person batch (422):** each accepted embedding is compared to the first using cosine
  similarity with the live `match_threshold`. Any pair below the threshold rejects with “The photos
  appear to show different people. Retake all photos of one person.” (`enrol_blocked_mixed_people`).
- **Cross-staff duplicate (409):** queries `FaceEmbedding` joined to `Staff` directly (not the
  gallery cache) for every staff member **except the target, including inactive staff, excluding
  anonymised**. Best score per other staff = max cosine over that staff’s vectors × new embeddings
  (same as `rules.match`). Score **>= live `duplicate_threshold` blocks** (exact threshold blocks;
  just below passes). Detail names the matched staff (`<name> (<code>), inactive` when applicable)
  but **never reveals scores**; a manager whose `department_scope` does not include the matched
  staff sees only `staff code <code>` (`enrol_blocked_duplicate`, records matched staff id).
- **Re-enrolment of the same staff member** still works and replaces that staff’s embeddings (own
  `staff_id` excluded). `duplicate_threshold` (live, DB-backed, 0–1, default = `match_threshold`)
  is validated/audited like other settings; no override option. Gallery refreshes after a successful
  commit; the gallery cache holds only active, non-anonymised staff.

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
| Dashboard | <http://localhost:8000/admin/dashboard.html> |
| Staff | <http://localhost:8000/admin/> |
| Person (linked from names) | <http://localhost:8000/admin/person.html?id=...> |
| Enrolment | <http://localhost:8000/admin/enrol.html> |
| Attendance | <http://localhost:8000/admin/attendance.html> |
| Review queue | <http://localhost:8000/admin/review.html> |
| Approvals & missing clock-outs | <http://localhost:8000/admin/approvals.html> |
| Reports | <http://localhost:8000/admin/reports.html> |
| Shifts (People + Schedules) | <http://localhost:8000/admin/shifts/index.html> |
| Settings | <http://localhost:8000/admin/settings.html> |
| Audit log | <http://localhost:8000/admin/audit.html> |

## Settings

Live (DB-backed) settings are editable in the admin UI and take effect without restart (signin/
clock/enrolment reads use live values where applicable): `match_threshold`, `ambiguity_margin`,
`duplicate_threshold` (enrolment cross-staff duplicate block, 0–1, default = `match_threshold`),
`cooldown_seconds`, `work_start`, `grace_minutes`, `review_retention_days`, `spoof_challenge`,
`missing_close_hours` (hours after shift end before an open session is auto-closed as missing
clock-out; default 4), and follow-up rule `followup_late_count`/`followup_period_days` (default 3/30,
flag N+ late arrivals in a period; validated/audited).

## Dashboard, metrics, and reports

**Metric definitions (shift-aware).** Attendance resolves against the schedule (exception → personal
→ assigned shared → none). A day is **absent** only once its shift **end has passed** (future/in-
progress scheduled days are not absent). **Day off / not scheduled** excluded from absence.
**Undone** (`undone_at`) ignored. **Night shifts** group by `shift_date` (start date). **Late** =
first non-undone/non-`missing_clock_out` in after `shift start + grace_min` (honors `flag='late'`).
**On-time rate** = on-time clock-ins / clock-ins. **Attendance rate** = present / scheduled ended
days. **Hours worked** = completed in→out pairs minus `break_min`; **missing clock-outs counted
separately and excluded from hours** (never worked time). **Overtime** = count/flag (minute-level
duration not stored; `overtime_hours` 0.0). Server `TIMEZONE`.

**Today KPIs** (`GET /api/admin/metrics/summary`, `/api/admin/attendance/dashboard`): present
(scheduled + valid in), `absent_so_far` (scheduled, ended, no in), late, `currently_clocked_in`,
`missing_clock_outs`, `pending_approvals`, plus range attendance/on-time rate, total hours,
late/overtime counts.

**Dashboard** (`static/admin/dashboard.html` + `js/dashboard.js`, Chart.js 4.4.7 **vendored**
`static/vendor/chart.umd.min.js`, no CDN): range (Today/7/30/Custom) + department filter (all);
KPI links; charts (daily trend Present/Late/Absent, arrival histogram vs shift start, late by
weekday, hours/attendance rate by department) — text titles/axis labels, patterned fills (not
colour-only), **View as table**, loading/empty/error; ranked lists with person links. Data:
`/api/admin/metrics/{summary,trend,breakdown,arrivals,people}` (admin/manager, manager dept-scoped,
366-day cap, constant queries). `/api/admin/metrics/insights` (2D plain rules, max 5) exists; the
dashboard Insights UI panel is hidden.

**Person page** (`static/admin/person.html?id=`, `GET /api/admin/metrics/person/{id}`,
`/person/{id}/export.csv`, manager dept-scoped): range; summary cards (attendance/on-time rate,
late count + **avg late minutes**, hours, overtime, early departures, missing clock-outs); month
calendar **coloured + text-labelled** (on time/late/absent/day off/missing clock-out/not
scheduled); arrival-offset-by-day line chart (min vs shift start, negative=early); events table
with flags + **manager edits/reasons**; current schedule + **source**; **Export this person’s CSV**
+ **print-friendly view**. Linked from dashboard/staff/attendance/reports names.

**Reports** (`static/admin/reports.html`, `GET /api/admin/metrics/reports/late-arrivals|attendance-summary`,
`format=json|csv`, manager dept-scoped): late-arrivals and attendance-summary for any range/
department; **CSV export matches on-screen numbers/totals exactly** (same payload + `TOTAL` row).
Existing: `GET /api/admin/attendance/export.csv`, `/report/monthly.csv` (shift-aware; missing
clock-outs excluded from hours; night shifts by `shift_date`).

**Follow-up rules (configurable, audited; prompt for conversation, not an automatic penalty).**
`followup_late_count`/`followup_period_days` (default **3 in 30**, validated/audited) flag **N+ late
arrivals in the period** with a **“Needs follow-up”** badge on dashboard/reports/person
(`GET /api/admin/metrics/followup`; `needs_followup`/`followup` on responses). UI wording: prompt for
a conversation, not an automatic penalty. **No automated notifications.** Operational follow-up via
Approvals/missing-clock-out queue (auto-close `missing_close_hours` after shift end, default 4,
`missing_clock_out` never worked time; manager corrects with required reason, audited).

## Known limitations

- **Not certified anti-spoofing.** Optional head-turn challenge helps against still photos but may
  not stop video replay; a liveness aid, not a security guarantee.
- **One shift per person per day.** No split shifts/rosters/leave; resolution exception → personal
  → assigned shared → none.
- **Token & undo windows.** Single-use clock token **20s** (person+kiosk); **Undo 10s** after success
  (`undone_at`, never deletes).
- **Overtime margin 0** in clock flow; overtime reported as **count** (`overtime_hours` 0.0).
- **Missing clock-outs never counted as worked time.** Auto-close audited; manager enters real time
  with a required reason.
- **Sessions in memory** (no restart survival). **No password change UI.** **SQLite
  `check_same_thread=False`** (single-site). Legacy `POST /api/signin` kept (Phase 2 uses `/api/scan`
  + `/api/clock`). Enrolment/face quality depends on images/lighting; ambiguous/no-match → review
  queue (retention-limited). **Single site** (no multi-site/HA).

## Recognition evaluation

Uses the **same `Recognizer`/threshold path as the app** (`scripts/evaluate.py` → `app/eval_metrics.py`).
`eval_data/` has **no genuine/impostor pairs**; runs yield `enrolled=0 genuine=0 impostor=0 rejected=0
FAR=0 FRR=0` (`20261108T181401Z`, `20261109T113446Z`, `20261109T121932Z`, re-run `20261109T135936Z`);
missing data exits `2`. **No FAR/FRR fabricated, no baseline altered; recognition logic/results
unchanged** by Phase 2/3/kiosk UI/Steps 1B/2A–2C/2D/3A/3B. Populate `eval_data/` and re-run for real FAR/FRR.

## Everything not done (Step 3C status / out of scope)

- **Step J (optional, not implemented):** read-only weekly calendar by department + “working today”
  (from `/api/admin/shifts/people`).
- **No automated follow-up notifications/reminders** (badge/prompt for conversation + approvals/
  missing-clock-out queue). **Insights UI panel hidden** (endpoint `/api/admin/metrics/insights`
  exists).
- **`scripts/cleanup_personal_patterns.py --apply` on the real DB not run** (dry-run 0; needs
  confirmation + DB backup).
- **Real recognition FAR/FRR not measured** (no labeled `eval_data/`); eval runs all zeros
  (`20261108T181401Z`–`20261109T135936Z`). Recognition unchanged.
- **Manual browser/QA** of People/Schedules, enrolment duplicate/mixed-person, kiosk/camera,
  dashboard/person/reports rendering not automated. **Metric numbers verified** against a hand-built
  **3-person/2-week** dataset (one late, one absent, one night shift, one missing clock-out) via
  `app.metrics` — comparison table (hand-expected vs actual, all matched):
  | Subject | Metric | Hand-expected | Actual (`app.metrics`) |
  | --- | --- | ---:| ---:|
  | P1 Late (Eng, Mon–Fri x2, grace 10, break 60) | present / absent | 10 / 0 | 10 / 0 |
  | P1 (late Tue 2026-01-06 08:20) | late / late_min / on_time_rate / hours | 1 / 20 / 0.9 / 79.6667 | 1 / 20 / 0.9 / 79.6667 |
  | P2 Absent (Eng, absent Wed 2026-01-07) | present / absent / attendance_rate / hours | 9 / 1 / 0.9 / 72.0 | 9 / 1 / 0.9 / 72.0 |
  | P3 Night (Ops, 22:00–06:00 overnight, break 30, missing out 2026-01-08 shift) | present / absent / missing / hours | 10 / 0 / 1 / 67.5 | 10 / 0 / 1 / 67.5 |
  | Team (3 people, 10 ended weekdays each) | sched_ended / present / absent / late / hours / missing | 30 / 29 / 1 / 1 / 219.1667 / 1 | 30 / 29 / 1 / 1 / 219.1667 / 1 |
  (Range 2026-01-05–2026-01-16, `now` after end. Night shift groups by `shift_date`; missing clock-out
  excluded from hours. Dashboard today-KPI formulas previously verified vs a hand-built temp-DB case.)
- **Metrics scoping:** `/api/admin/metrics/*` admin/manager + dept scope; existing
  `/api/admin/attendance/dashboard|export.csv|report/monthly.csv` still `require_admin` (unchanged).
- **Pushing to `origin/main`** (ahead locally). **Local artifacts untracked:** `.kilo/`, `_wtest.py`,
  `attendance.db.bak-*`, Phase 2/3 `.docx/.md`.
- **Out of scope (product):** split shifts, rosters, leave/swaps, staff self-service; multiple
  shifts/person/day; overtime-minute config (margin 0). Production hardening unchanged (HTTPS/proxy,
  persistent sessions, Alembic, heavier SQLite/WSGI, secret rotation, password-change UI, scheduling
  `scripts/close_sessions.py`).


## License

Internal / not specified. Add a `LICENSE` file before external distribution.
