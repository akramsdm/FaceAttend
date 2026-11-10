# Shift & Attendance Lifecycle Audit — Report

**Date:** 2026-11-10
**Scope:** kiosk clock in/out, legacy sign-in, approvals, review queue, attendance pairing/reporting, timestamps, DB integrity, shift reassignment, deployment.
**Result:** Full test suite green — `264 passed, 6 skipped`. Live server redeployed and verified.
**Companion document:** for the current-state lifecycle description, strengths, and residual risks, see `docs/Session Lifecycle - Current State Report.md`. This document remains the remediation history and deployment runbook.

---

## A. Root-cause analysis

### A1. Clock-out resolved "today's" shift instead of the open session's shift
The old `/api/clock` path re-resolved the shift at clock-out time. For overnight shifts the local date rolls over, so the clock-out attached the *next* day's shift (or `NULL`), splitting one night shift into two and producing wrong hours/flags. Clock-outs also did not carry the `shift_id` / `shift_date` of the clock-in they closed.

**Fix:** a clock-out now closes the exact open `in` row found by `find_open_session()` and inherits its `shift_id` / `shift_date`. The start-date is authoritative for night shifts.

### A2. Orphan clock-outs were recorded as attendance
Any `out` request (or legacy `/api/signin`, review assignment, approval) could write an `out` row with no preceding `in`, creating negative/unpaired sessions and corrupting reports.

**Fix:** every write path now checks lifecycle state first. A clock-out with no open session is rejected with `no_open_session` (HTTP 409) and **no** attendance/audit row is written. Legacy `/api/signin`, review assignment and approvals route through the same service.

### A3. Completed shifts could be re-entered silently
A second clock-in on an already completed shift created a duplicate session and double-counted hours.

**Fix:** a completed shift returns `shift_already_completed` and writes no row. `/api/clock` always rejects ordinary re-entry. The kiosk now offers a manager-approval request on the completed state even when `allow_reentry_same_shift` is OFF; the request itself writes no attendance, and only an explicit manager approval creates the new open session.

### A4. No concurrency guard / no idempotency
Two rapid scans could both observe "no open session" and both insert an `in`; a retried request could insert a duplicate event.

**Fix:**
- `Attendance.open_key` (unique while open) enforces one open session per employee at the DB level.
- `Attendance.token_jti` (unique) makes clock requests idempotent; a retry returns the already-persisted row.
- `Attendance.paired_in_id` links each `out` to the exact `in` it closes.
- A process-level `threading.RLock` serialises attendance mutations inside the app.
- `IntegrityError` handlers fall back to `_event_for_jti()` so a lost race returns the winning row instead of a false block.

### A5. Minimum clock-out duration blocked legitimate short sessions
`min_clockout_seconds` / `clock_out_too_soon` rejected quick clock-outs (e.g. mistaken clock-in, shift handover).

**Fix:** the restriction was removed end-to-end (service, settings schema, UI, tests). Any open session can be clocked out.

### A6. Late / after-shift-end clock-ins had no explicit policy
Clock-ins after the shift end were silently accepted (or mis-associated), and a restart-time auto-closer could then write an `out` *before* that late `in`, producing a negative session.

**Fix:**
- Clock-ins after shift end are blocked with `after_shift_end` / `reason_code: out_of_window`; late-within-shift remains allowed and flagged `late`. The actual timestamp is always preserved.
- `close_missing_sessions()` skips any session whose clock-in is already at/after the auto-close cut, so it can never write `out < in`. Such sessions are left open for manual correction.
- A legacy row damaged by the old closer (row 2) was reverted in place (marked undone, reason `reverted:auto_close_before_in:2`) and the original open session restored — **no operational rows were deleted**.

### A7. Timestamps were ambiguous
Naive UTC values were serialised without `Z`, while some local values could be mistaken for UTC, and kiosk display appended `Z` to non-UTC values.

**Fix:** DB stays naive UTC; API emits explicit UTC (`Z`) via the shared `iso_utc()` helper and Pydantic `field_serializer`s; `local_time` is offset-aware; the kiosk displays in `Africa/Kampala` without inventing a `Z`.

### A8. Stale token / false-success UI
The kiosk could show success before the backend persisted the action, and stale tokens did not revalidate.

**Fix:** a pure state module (`static/signin_state.js`) classifies server responses; the UI only shows success when a persisted attendance row is returned. Stale tokens revalidate and return HTTP 409. Distinct states exist for no session / open / completed / re-entry / success / rejected / stale / network.

### A9. Deployment drift and unset timezone
The running server was originally started with `--reload` while `watchfiles` was missing, so the reloader never restarted the worker and it kept serving pre-audit code. Separately, `TIMEZONE` was unset, so `get_settings()` fell back to `UTC` and every shift window was evaluated three hours away from `Africa/Kampala`.

**Fix:** `watchfiles` was added to `requirements.txt`; the final server runs **without** `--reload` under `APP_ENV=development` and `TIMEZONE=Africa/Kampala`. `app/config.py` reads only the process environment (there is no `.env` loader), so the variable must be exported by whatever starts the process (shell, service, container). The worker was restarted after the final source edits and its start time checked against file mtimes.

### A10. After a shift ended, the kiosk blocked without saying when to return
`after_shift_end` and `shift_already_completed` returned a bare refusal. Employees could not see the shift end they had missed or the next scheduled shift, and had no path to request approval.

**Fix:** `state_for_staff()` now resolves `next_shift` (the first shift starting after now, skipping the current `shift_date`) whenever the shift has ended or is complete. `/api/scan` exposes `next_shift_summary` / `next_shift_start` / `next_shift_end`. The kiosk renders "Shift ended", "Next shift starts …", and offers "Request approval" for both `after_shift_end` and `shift_already_completed`; completed/re-entry copy includes the next-shift context. Shift wall-clock values are formatted locally instead of being re-parsed as UTC.

### A11. A stale "ends next day" flag distorted same-day shifts
Pattern 7 stored Wednesday as 10:00–18:00 with `overnight=1` left over from an earlier night-shift configuration. Resolution trusted the flag, so the shift was treated as ending Thursday 18:00 — a 32-hour span and the wrong "next shift" time.

**Fix:** overnight is now derived from the times everywhere (`end_time <= start_time`) — shift resolution, exception resolution, pattern/API serialisation, personal-week comparison/writes, and both admin editors. Existing stale flags are ignored without rewriting or deleting history; editors no longer save contradictory flags.

### A12. Shift reassignment (Schedules > People) crashed after a day change/holiday
`/api/admin/shifts/people` returned HTTP 500 `TypeError: 'ShiftException' object is not subscriptable` whenever the requested week contained a day change or holiday. `_shifts_data_for()` returned ORM objects for exceptions while `patterns` / `assignments` were plain dicts, and the response builder indexed them like mappings.

**Fix:** `_shifts_data_for()` now returns the built exception dicts, matching the other collections. Regression test `test_people_survives_day_change_exception` saves a day-change exception and reloads the week.

---

## B. Implementation changes

### New module — `app/attendance_service.py`
Single source of truth for the lifecycle:

| Function | Responsibility |
|---|---|
| `find_open_session()` | Locate the one legitimate open `in` (explicit `open_key`, legacy fallback). |
| `state_for_staff()` | Authoritative state: open session, completed shift, shift, evaluation, allowed actions. |
| `resolve_today()` / `resolve_shift_for_open_session()` | Resolve shift by local date; inherit open-session shift for clock-out. |
| `next_shift_for()` | First shift starting after now; powers the "next shift" kiosk copy after a shift ends. |
| `completed_session_for_shift()` | Detect an already-completed shift. |
| `record_event()` | Persist `in`/`out` with `shift_id`, `shift_date`, `token_jti`, `open_key`, `paired_in_id`. |
| `close_open_session()` | Close the actual open row, inherit shift, clear `open_key`, link `paired_in_id`. |
| `blocked_detail()` / `reason_code_for_message()` | Stable machine-readable rejection payloads. |

### Schema & migration — `app/db.py`, `app/models.py`
New columns (idempotent `ALTER TABLE`, existing rows untouched):
- `token_jti VARCHAR(64)` — idempotency key.
- `paired_in_id INTEGER REFERENCES attendance(id)` — out→in link.
- `open_key VARCHAR(64)` — unique while a session is open.

New indexes:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_token_jti ON attendance(token_jti);
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_open_key ON attendance(open_key);
CREATE INDEX IF NOT EXISTS ix_attendance_paired_in_id ON attendance(paired_in_id);
```
SQLite allows many `NULL`s in a unique index, so historical rows are preserved.

### Endpoints routed through the service
- `app/routers/clock.py` — `/api/clock` (in/out), `/api/clock/undo`, `/api/clock/request`; idempotent replay, 409 blocked payloads, `IntegrityError` fallback.
- `app/routers/signin.py` — `/api/scan` advertises `state`; legacy `/api/signin` enforces the same lifecycle (no orphan outs, no reopening completed shifts).
- `app/routers/approvals.py` + `app/approvals.py` — approval-created rows and missing-clock-out auto-close go through the service; auto-close never precedes the `in`.
- `app/routers/review.py` — review assignment cannot create an orphan out; returns 409 `attempt_before_open_session` if the attempt predates the open clock-in.
- `app/routers/attendance.py` — `_pair_sessions` / `_build_sessions` honour `paired_in_id`; `AttendanceRead` serialises `timestamp` as explicit UTC.

### Shift policy — `app/shifts.py`
- `resolve_pattern()` added.
- `evaluate()` gains `completed_session_for_shift` and the `after_shift_end` block (`reason_code: out_of_window`).
- `min_clockout_seconds` removed everywhere.
- `overnight` is derived from `end_time <= start_time` in pattern and exception resolution; a stored flag cannot make a same-day span cross midnight.
- `/api/scan` carries `next_shift_summary` / `next_shift_start` / `next_shift_end` when the shift has ended or is complete.

### Shift reassignment — `app/routers/shifts.py`
- `_shifts_data_for()` returns exception dicts (not ORM objects), matching `patterns` and `assignments`.
- Pattern/assignment/exception/week-save/history endpoints verified against a copy of the live DB after saving a day change.

### Frontend — `static/signin.js`, `static/signin_state.js`, `static/signin.html`
- Pure classifier `SigninState.clockResult()` / `blockedCopy()` drives all UI states.
- Success only when a persisted attendance row is present.
- Explicit handling for blocked, stale (409), network and re-entry-approval states.
- After a shift ends or is already complete, shows the shift end, the next shift start and an approval-request action.
- Completed/re-entry states show "Shift status: Completed" instead of a misleading "Time remaining in shift" timer.
- No client-side `Z` appended to local wall-clock values; shift times are formatted as local wall-clock values and durations use the configured `Africa/Kampala` offset.
- Admin editors derive "ends next day" from the times and never save a contradictory flag.

### Diagnostics — `scripts/diagnose_attendance.py`
Read-only by default. Reports:
- orphan clock-outs,
- legacy open sessions (`NULL open_key`),
- **all** open sessions,
- **negative** sessions (out before in),
- duplicate explicit open sessions,
- stale `open_key` on closed sessions,
- clock-outs missing `paired_in_id`.

Opt-in `--backfill-links` only fills safe `NULL` out→in links.

---

## C. Tests & results

### Added / updated
| File | Coverage |
|---|---|
| `tests/test_attendance_lifecycle.py` (21 tests) | full state machine, orphan rejection, shift inheritance, night shift, re-entry approval (including completed-state request with the setting OFF), after-shift-end, next-shift resolution, idempotency, auto-close guard, pairing. |
| `tests/test_shifts.py` | pure shift rules; stale `overnight` flags on same-day pattern and exception spans are ignored. |
| `tests/test_clock_bugs.py` | regressions for the reported clock bugs. |
| `tests/test_attendance_sessions.py` | session pairing/building. |
| `tests/test_shift_people_history.py` | People endpoint survives a saved day change; assignment/history flows. |
| `tests/test_attendance.py` | `test_attendance_timestamps_serialise_as_explicit_utc`. |
| `tests/test_frontend_state.py` (1 test) | runs the Node state tests under pytest. |
| `tests/js/signin_state.test.js` (5 tests) | no false success, blocked/stale/network mapping, distinct copy, next-shift context. |

### Final full suite
```
$ .\.venv\Scripts\python.exe -m pytest -q
264 passed, 6 skipped, 2 warnings in 45.74s (0:00:45)
```

### Frontend state tests
```
$ node --test tests\js\signin_state.test.js
tests 5
pass 5
fail 0
```

### Live DB diagnostics (`TIMEZONE=Africa/Kampala`)
```
Scanned 3 attendance rows in the last 7 day(s).
Orphan clock-outs: 0
Open sessions on legacy fallback (NULL open_key): 0
All open sessions (awaiting clock-out or correction): 0
Negative sessions (clock-out before clock-in): 0
Employees with duplicate explicit open sessions: 0
Stale open_key on closed sessions: 0
Clock-outs missing paired_in_id link: 0
```
Live rows: `id=1` clock-in (late) paired with `id=3` clock-out (overtime) for 2026-11-09; `id=2` is the reverted auto-close artifact (undone, reason recorded). No rows were deleted.

### Live state verification (`TIMEZONE=Africa/Kampala`, copy of live DB)
```
08:40 EAT Tue | no_session      | shift 08:00–17:00 (same day) | next: -
17:30 EAT Tue | after_shift_end | shift 08:00–17:00 (same day) | next: Wed 10:00–18:00 (same day)
23:00 EAT Tue | after_shift_end | shift 08:00–17:00 (same day) | next: Wed 10:00–18:00 (same day)
18:30 EAT Wed | after_shift_end | shift 10:00–18:00 (same day) | next: Thu 22:00–Fri 06:00 (overnight)
```
The stale Wednesday `overnight=1` is ignored: the Wednesday shift ends the same day, and the Thursday night shift is correctly overnight.

### Timestamp serialisation proof
```
{"id":1,...,"timestamp":"2026-01-05T09:00:00Z","local_time":"2026-01-05T12:00:00+03:00",...}
```
Naive DB UTC → explicit `Z`; local display → `+03:00` under `Africa/Kampala`.

---

## D. Risks & mitigations

| Risk | Severity | Mitigation / status |
|---|---|---|
| Historical orphan/negative rows already in production DB | High | Run `scripts/diagnose_attendance.py` read-only first; correct manually or via approvals. Never delete history. Current live DB: 0 orphans, 0 negatives. |
| Clock-ins recorded after shift end stay open (auto-close refuses) | Medium | Intentional; surface in "missing clock-out" admin queue for manager correction. Documented in diagnostics. |
| `allow_reentry_same_shift` enabled without process discipline | Medium | Default OFF; requires manager approval; ordinary `/api/clock` still rejects. |
| SQLite write contention under many kiosks | Medium | In-process `RLock` + unique indexes; single-host deployment assumed. Move to a server DB before multi-host scale-out. |
| Timezone misconfiguration in a new environment | High | `TIMEZONE` defaults to `UTC`; there is no `.env` loader, so the launcher must export `TIMEZONE=Africa/Kampala`. Timestamps are UTC in DB and `Z` on the wire regardless. |
| Stale `overnight` flag in existing pattern rows | Medium | Resolution and editors now derive overnight from the times; stale flags are ignored without rewriting history. |
| `APP_ENV=development` left in production | High | Production requires `APP_ENV=production` **and** a non-default `SECRET_KEY`. |
| Server run with `--reload` and no `watchfiles` silently serves stale code | High | Run without `--reload`; verify process start time vs. file mtimes after deploy (see E). |
| Legacy `/api/signin` consumers | Low | Kept for compatibility but now lifecycle-safe; no-shift in/out behaviour preserved for existing tests. |
| `.db.bak-*` files and uncommitted changes in the working tree | Low | Operational backups are gitignored/untracked; commit code changes deliberately. |

---

## E. Deployment & verification runbook

### Prerequisites
```powershell
cd D:\work\faceattendance
$env:APP_ENV = "production"                 # development only for local
$env:TIMEZONE = "Africa/Kampala"
$env:SECRET_KEY = "<non-default, >=32 chars>"   # production only
```

> `app/config.py` reads only the process environment; it does **not** load `.env`. Export these variables in the shell, service definition, or container that starts `uvicorn`. If `TIMEZONE` is omitted the app silently runs in `UTC`.

### 1. Stop the old server
```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'python|uvicorn' -and $_.CommandLine -match 'uvicorn|app.main' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 3
Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
# Expect: no output (port free)
```

### 2. Start without `--reload`
```powershell
New-Item -ItemType Directory -Force -Path var | Out-Null
Start-Process -FilePath "D:\work\faceattendance\.venv\Scripts\uvicorn.exe" `
  -ArgumentList @("app.main:app","--host","127.0.0.1","--port","8000") `
  -WorkingDirectory "D:\work\faceattendance" `
  -RedirectStandardOutput "D:\work\faceattendance\var\uvicorn.out.log" `
  -RedirectStandardError  "D:\work\faceattendance\var\uvicorn.err.log" `
  -WindowStyle Hidden
Start-Sleep -Seconds 9
```

> If you must use `--reload`, install `watchfiles` first (`pip install watchfiles`); otherwise the reloader never fires.

### 3. Verify health, schema and static assets
```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Compress
# {"status":"ok"}

$o = Invoke-RestMethod http://127.0.0.1:8000/openapi.json
$o.components.schemas.SettingsUpdate.properties.PSObject.Properties.Name -contains 'min_clockout_seconds'   # False
$o.components.schemas.SettingsUpdate.properties.PSObject.Properties.Name -contains 'allow_reentry_same_shift' # True
$o.components.schemas.ScanResponse.properties.PSObject.Properties.Name -contains 'state'                       # True

(Invoke-WebRequest http://127.0.0.1:8000/static/signin_state.js -UseBasicParsing).StatusCode   # 200
(Invoke-WebRequest http://127.0.0.1:8000/static/signin.html      -UseBasicParsing).Content -match 'signin_state.js'  # True
(Invoke-WebRequest http://127.0.0.1:8000/static/signin.js        -UseBasicParsing).Content -match 'SigninState.clockResult'  # True
```

### 4. Verify the database was migrated without data loss
```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('attendance.db'); [print(r) for r in c.execute('select id,type,timestamp,flag,shift_id,shift_date,paired_in_id,open_key,undone_at,edit_reason from attendance order by id')]"
```
Expected: all historical rows present; new columns populated where applicable.

### 5. Run diagnostics (read-only)
```powershell
$env:TIMEZONE = "Africa/Kampala"
.\.venv\Scripts\python.exe scripts\diagnose_attendance.py --days 7
```
Expect `Orphan clock-outs: 0` and `Negative sessions: 0`. Only run `--backfill-links` after reviewing the report.

### 6. Re-run the test suite
```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests\js\signin_state.test.js
```

### 7. Confirm the process is current
```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'python|uvicorn' } |
  Select-Object ProcessId,ParentProcessId,Name,CreationDate,CommandLine | Format-List
Get-Item app\routers\clock.py,app\routers\review.py | Select-Object Name,LastWriteTime
```
The process `CreationDate` must be **later** than the newest source file's `LastWriteTime`.

### Verified on this host
- Server restarted **2026-11-10 08:57:40** (`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`, no `--reload`), with `APP_ENV=development` and `TIMEZONE=Africa/Kampala` exported by the launcher. Listening PID 15588.
- Source edits deployed before restart: `app/shifts.py` 08:47, `app/routers/shifts.py` 08:57, `app/shift_service.py` 08:49, `app/attendance_service.py` 08:42, `app/routers/signin.py` 08:42, `static/signin.js` 08:57.
- `/health` → `{"status":"ok"}`; OpenAPI and static assets verified; open session preserved; 0 orphans, 0 negative sessions.
- Live-DB state checks confirm same-day Wednesday 10:00–18:00 and overnight Thursday 22:00–06:00; stale `overnight=1` on Wednesday ignored.
- Full suite `264 passed, 6 skipped`; lifecycle `21 passed`; Node state tests `5 passed`.

---

## Rollback
1. Stop the server (step E1).
2. Restore the pre-change database from the relevant `attendance.db.bak-*` (do **not** overwrite production without a fresh backup).
3. `git checkout -- <files>` for code, or redeploy the previous commit.
4. Restart without `--reload` and re-run E3–E5.

New columns/indexes are additive and nullable, so old code continues to read the migrated database.
