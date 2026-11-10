# Session Lifecycle — Current State Report

**Date:** 2026-11-10
**Scope:** the attendance session lifecycle as implemented today — kiosk scan/clock, legacy sign-in, approvals, review assignment, auto-close, undo, manual admin edits, reporting, timestamps, and data integrity.
**Companion document:** `docs/Shift Lifecycle Audit - Report.md` is the remediation log (what was broken and how it was fixed). This document describes the resulting behaviour and the flaws that remain.

---

## 1. Executive summary

The lifecycle is now a **server-authoritative state machine**. A scan proves identity only; it does not write attendance. Every write path — `/api/clock`, legacy `/api/signin`, review assignment, manager approval, and auto-close — is expected to go through `app/attendance_service.py`.

The core rules hold in the normal single-process deployment:

- At most one legitimate open session per employee (`open_key` + unique index).
- A clock-out closes the **actual** open clock-in and inherits its `shift_id` / `shift_date`; it never re-resolves "today's" shift.
- A completed shift is rejected with `shift_already_completed`; ordinary re-entry is always rejected by `/api/clock`.
- The only path to a new session on a completed shift is an explicit manager approval.
- Orphan clock-outs return `no_open_session` and write no attendance or audit row.
- There is no minimum clock-out duration; short sessions can be closed.
- The kiosk shows success only when the backend returns a persisted attendance row with an `id`.
- Timestamps are stored as naive UTC and serialised with explicit `Z`; display values carry the configured offset.

The most important **remaining risks** are:

1. **Approvals use approval time, not request time**, and re-resolve the shift at approval time. A request made at 17:00 and approved at 23:00 records 23:00.
2. **The clock-out path is not safe across multiple processes/workers.** The `RLock` is per process and `paired_in_id` has only a non-unique index, so two workers can both close the same session.
3. **Admin manual create/correct endpoints bypass the lifecycle** and can corrupt `open_key` / `paired_in_id` state.

These are detailed in §11 and prioritised in §12.

---

## 2. The lifecycle in words

### 2.1 Identity and intent are separated

An employee looks at the kiosk. `/api/scan` recognises the face, records a `RecognitionAttempt`, computes the authoritative state, and issues a signed, single-use token bound to the employee and kiosk (`ClockToken`, 20-second TTL). It writes **no attendance row**. The token only proves "this is staff N at kiosk K"; it does not authorise an action.

The employee then chooses an action. `/api/clock` re-reads the state from the database, decides whether the action is legitimate, and only then consumes the token and writes exactly one row.

### 2.2 Clock-in

When there is no open session and the shift window permits it, `/api/clock` with `action="in"`:

1. Resolves the current shift (`resolve_today`) and its start date (`shift_date_for`).
2. Checks that no session is already open.
3. Checks that the shift is not already completed.
4. Inserts one `attendance` row with `type="in"`, `shift_id`, `shift_date`, a flag (`late` or none), and `open_key = str(staff_id)`.
5. Returns the persisted row with UTC and local representations.

The unique index on `open_key` is the database-level guarantee that a second concurrent clock-in cannot create a second open session.

### 2.3 Open session

While a session is open, `state_for_staff()` reports `state="open_session"` and `allowed_actions=["out"]`. The shift is rebuilt from the stored `shift_id` on the clock-in row via `resolve_pattern()`, **not** from the current assignment or exception. A later reassignment therefore cannot move an already-open session.

The evaluation while open is:

| Local time relative to shift | Message | Flag | Confirmation |
|---|---|---|---|
| Before shift end | `clock_out_early_confirm` | `early_departure` | Required |
| At/after end, within overtime margin | `recorded` | none | Not required |
| Beyond the overtime margin | `recorded` | `overtime` | Not required |

The kiosk sends `confirm=true` only after the employee accepts the early-departure warning.

### 2.4 Clock-out

A clock-out finds the exact open `in` row, clears its `open_key`, and inserts one `out` row with:

- the same `staff_id`, `shift_id`, and `shift_date` as the open clock-in;
- `paired_in_id` pointing at that clock-in;
- the flag from the evaluation;
- the token's `token_jti` for idempotency.

If there is no open session, the request is rejected with `no_open_session` (HTTP 409) and **nothing is written**. If the shift is already complete and no session is open, a clock-out is rejected with `shift_already_completed`.

There is no minimum duration check.

### 2.5 Completion

With no open session, the service resolves today's shift and its start date, then asks `completed_session_for_shift()` whether a non-undone clock-in on that shift date has a later non-undone clock-out. If yes:

- `state="completed"`;
- message `shift_already_completed`;
- no allowed actions;
- `/api/clock` rejects both `in` and `out`.

If the `allow_reentry_same_shift` setting is ON, the state becomes `reentry_required`, but the allowed actions are still empty. This is deliberate: the setting exposes the approval path, it does not grant automatic re-entry.

### 2.6 Re-entry and manager approval

The kiosk's completed state offers **Request approval** even when the setting is OFF. The request creates a pending `ApprovalRequest` row and writes no attendance.

A manager can approve or reject:

- **Approve an `in`:** the service refuses if a session is already open; otherwise it resolves the shift and inserts a new open `in` row with `approved_by`, `edit_reason="approved_request:<id>"`, and the approval flag.
- **Approve an `out`:** the service finds the actual open session; if none exists it returns `no_open_session`; otherwise it closes that session with the manager's approval recorded.
- **Reject:** only the request status changes; no attendance is written.

The approval request row keeps its id and changes status to `approved` or `rejected`. Attendance ids are never overwritten.

### 2.7 Auto-close of missing clock-outs

`close_missing_sessions()` closes an open session when the shift end plus `missing_close_hours` (default 4) has passed. It:

- resolves the shift from the stored open-session identity;
- refuses to write an `out` earlier than its `in` (legacy late clock-ins are left open for a manager);
- is idempotent via `edit_reason="auto_missing:<in_id>"`;
- writes an `out` with flag `missing_clock_out`, linked to the clock-in;
- records an audit entry.

Missing-clock-out rows are **excluded** from worked time and departure status until a manager corrects the time. It runs at application startup and can be run on a schedule via `scripts/close_sessions.py`.

### 2.8 Undo

A clock event can be undone within 10 seconds:

- undoing a clock-in is refused if a clock-out is already paired to it;
- undoing a clock-out re-opens the paired clock-in if no other session is currently open;
- undo is idempotent (`already_undone`).

### 2.9 Reporting

The sessions/reporting layer groups events by `(staff_id, shift_date)`. It prefers the explicit `paired_in_id` link; legacy rows fall back to chronological pairing. It computes arrival/departure status at read time, excludes missing clock-outs from hours, and tags orphan outs. The monthly CSV sums paired session durations per day.

---

## 3. State machine

```
                    scan (identity only, no row)
                              |
                              v
                        +-----------+
                        |  PENDING  |  no open session, shift not completed
                        +-----------+
                         |    |    |
             clock-in    |    |    | clock-in outside window / completed
             in window    |    |    +------------------------------+
                         v    |                                   v
                   +-----------+                         +----------------+
                   | CLOCKED_IN|                         |    BLOCKED     |
                   +-----------+                         | no_shift /     |
                    |        |                           | before_window /|
          clock-out |        | auto-close                | after_shift_end|
          (any time)|        | (end + N hours)           | completed      |
                    v        v                           +----------------+
              +-----------+  +-----------+
              | COMPLETED |<-| COMPLETED |  flag missing_clock_out
              +-----------+  +-----------+
                    |
                    | manager approves re-entry
                    v
              +-----------+
              | CLOCKED_IN|  (new row, approved_by set)
              +-----------+
                    |
                    v
              +-----------+
              | COMPLETED |
              +-----------+
```

### State table

| State | Meaning | Allowed actions | Writes |
|---|---|---|---|
| `no_session` | No open session; shift not completed; within window | `in` (or `out` only if legacy open row) | Clock-in creates `in` |
| `open_session` | An `in` row is open | `out` | Clock-out creates `out` |
| `completed` | A paired session exists on this shift date | none (approval request only) | none |
| `reentry_required` | Completed, setting ON; approval still required | none (approval request only) | none |
| `no_shift` | No shift resolved | none (approval request only) | none |
| `before_window` | Too early | none (approval request only) | none |
| `after_shift_end` | Shift has ended | none (approval request only) | none |

The kiosk additionally distinguishes `confirm`, `stale`, `network`, and generic `error` results; success requires HTTP 200 **and** `attendance.id`.

---

## 4. Shift resolution and dates

`resolve_shift()` uses first-match-wins precedence:

1. **Date exception** for that staff member and date.
2. **Personal pattern** owned by that staff member.
3. **Active shared assignment** covering that date.
4. **No shift.**

A day-off exception yields no shift. A personal pattern with no enabled day yields no shift; it does not fall through to a shared assignment.

`overnight` is derived from the times: `end_time <= start_time`. Stale `overnight` flags are ignored everywhere (resolution, exception spans, API serialisation, pattern writes, admin editors). This is why a same-day 10:00–18:00 pattern is no longer treated as ending the next day.

`shift_date_for()` returns the shift's **start date**. For an overnight shift, a timestamp after midnight but still within the shift belongs to the previous calendar date. That start date is what the clock-out inherits, so a night shift is one session, not two.

`resolve_today()` considers today's shift and yesterday's overnight shift (while still before its end), then prefers the one whose window contains the current time. `next_shift_for()` searches up to 21 days ahead for the first shift starting after now, skipping the current shift date; it is display context only and never authorises an action.

---

## 5. Path-by-path behaviour

### 5.1 `/api/scan`

- Performs recognition, spoof challenge, quality checks, and attempt logging.
- Computes `state_for_staff()`.
- Issues a 20-second single-use token bound to staff and kiosk.
- Returns name, department, shift summary, next-shift summary, allowed actions, message, state, and token.
- **Never writes attendance.**

### 5.2 `/api/clock`

- Serialises mutations with a process-level `RLock`.
- Returns the original row if the token's `jti` already has an attendance row (idempotency).
- Verifies token signature, kiosk binding, expiry, and unused status.
- Re-reads authoritative state; rejects any action not currently allowed.
- Requires `confirm=true` for an early clock-out.
- Consumes the token only after the action is known to be legitimate.
- Re-checks open session and completed shift inside the transaction.
- Commits; on `IntegrityError` (unique `open_key` or `token_jti`), rolls back and either returns the winning idempotent row or reports `session_already_open`.
- Returns `attendance` with `timestamp` ending in `Z`, `local_time`, and offset-aware `local_iso`.

Stale/expired tokens revalidate the current state: if the action is now blocked, the response is a 409 with the blocked detail; otherwise it is a 400 `invalid_token`.

### 5.3 Legacy `/api/signin`

Kept for compatibility. It recognises the face, applies a cooldown, then:

- if a session is open, closes it;
- if the shift is completed, returns 409 `shift_already_completed`;
- if the shift is before-window or after-end, returns 409;
- otherwise records a clock-in.

It enforces the lifecycle but does **not** require early-departure confirmation and does not use a clock token.

### 5.4 Approval request / approve / reject

- `POST /api/clock/request` creates a pending request; token optional; no attendance.
- `POST /api/admin/approvals/{id}/approve` calls `create_attendance_for_approval()`.
- `POST /api/admin/approvals/{id}/reject` only changes status.
- Both are role-gated (admin/manager) and department-scoped for managers.
- Approval writes exactly one attendance row and an audit entry.

### 5.5 Review assignment

`POST /api/admin/review/{attempt_id}/resolve` with `action="assign"`:

- evaluates the lifecycle **at the attempt timestamp**;
- if a session was open then, closes it, refusing an attempt timestamp earlier than the clock-in;
- if the shift was completed, returns 409;
- otherwise records a clock-in at the attempt timestamp.

This prevents a review assignment from creating an orphan clock-out or a duplicate open session.

### 5.6 Auto-close

Runs at startup and via `scripts/close_sessions.py`. It only considers clock-ins from the last 7 days, closes at `shift end + missing_close_hours`, skips sessions whose clock-in is at/after that cut, and records an audit entry. It does not count missing clock-outs as worked time.

### 5.7 Undo

`POST /api/clock/undo` accepts a token or event id, binds the token to the event's staff and kiosk, enforces the 10-second window, prevents undoing a clock-in that already has a clock-out, and reopens the paired clock-in when undoing a clock-out (if no other session is open).

### 5.8 Manual admin attendance

`POST /api/admin/attendance` and `PATCH /api/admin/attendance/{id}` still write or edit rows directly. They do **not** call the lifecycle service. This is the largest integrity gap in the current implementation (see F4/F5).

---

## 6. Data model and integrity

`Attendance` carries the lifecycle fields:

| Field | Purpose |
|---|---|
| `shift_id` | Pattern that applied to the session (inherited by clock-out) |
| `shift_date` | Shift start date (authoritative for overnight grouping) |
| `flag` | `late`, `early_departure`, `overtime`, `missing_clock_out`, etc. |
| `token_jti` | Unique idempotency key for a kiosk request |
| `paired_in_id` | Exact clock-in closed by this clock-out |
| `open_key` | `str(staff_id)` while an `in` is open; unique |
| `approved_by` | Manager who authorised the row |
| `undone_at` | Soft undo marker |

Migrations are additive and idempotent (`app/db.py`): columns are added only if absent, and unique indexes are created with `IF NOT EXISTS`. Existing rows are never deleted or rewritten. SQLite permits many `NULL`s in a unique index, so legacy rows are unaffected.

Concurrency protection has two layers:

1. `ATTENDANCE_LOCK` (a process-level `threading.RLock`) serialises mutations in one process.
2. Unique indexes on `open_key` and `token_jti` provide database-level protection against duplicate open sessions and duplicate token writes.

`paired_in_id` currently has only a **non-unique** index. That matters for multi-process clock-out safety (F3).

---

## 7. Timezone and serialisation

The project convention is **naive UTC in the database**. Helpers convert at the boundary:

- `utc_to_local()` / `local_to_utc()` use the configured `TIMEZONE`.
- `iso_utc()` attaches UTC to naive values and converts aware values before appending `Z`. It never appends `Z` to a non-UTC value.
- Clock responses include `timestamp` (UTC `Z`), `local_time`, and `local_iso` (offset-aware).
- Shift wall-clock values (`shift_start`, `shift_end`, next-shift fields) are naive local values and are intentionally serialised **without** `Z`.
- The kiosk formats display times in the configured zone.

The configuration risk is that `app/config.py` defaults `TIMEZONE` to `UTC` and has no `.env` loader. If the process is started without `TIMEZONE=Africa/Kampala`, all shift evaluation moves three hours and late/early decisions become wrong (F7).

---

## 8. Kiosk behaviour

`static/signin_state.js` is a pure, testable classifier. `clockResult()` only returns `success` when:

```js
status === 200 && data.status === "ok" && data.attendance && data.attendance.id
```

It distinguishes:

| Result | UI meaning |
|---|---|
| `success` | Persisted attendance row returned |
| `confirm` | Early clock-out confirmation required |
| `blocked` | Server refused with a stable message code |
| `stale` | Token expired or already used; scan again |
| `network` | No HTTP response |
| `error` | Other server error |

`blockedCopy()` provides distinct copy and approval affordances for `no_shift`, `before_window`, `after_shift_end`, `shift_already_completed`, `reentry_approval_required`, `no_open_session`, and `session_already_open`. Completed/re-entry states show the shift end and next shift instead of "time remaining".

---

## 9. What is good

1. **One authoritative lifecycle module.** Clock, scan, legacy sign-in, approvals, review, and auto-close share the same helpers. This removed the previous drift where each endpoint had its own idea of "open".
2. **Server-side authorisation.** The token proves identity only. The server re-evaluates state on every clock request, so a stale or replayed token cannot force an invalid action.
3. **Correct clock-out semantics.** A clock-out closes the exact open row and inherits its shift identity. Night shifts stay one session, and a later reassignment cannot move an open session.
4. **Database-level guards.** Unique `open_key` and `token_jti`, plus `paired_in_id`, turn the most dangerous duplicates into constraint failures rather than silent corruption.
5. **Idempotency.** A retried clock request returns the already-persisted row instead of creating a second event; the `IntegrityError` fallback handles a lost race.
6. **No false success.** The kiosk requires a persisted row id before showing success, and stale tokens revalidate to a 409 rather than a misleading success.
7. **Orphan clock-outs are impossible through the normal paths.** The service refuses to write an `out` without an open `in`; review and approval paths apply the same rule.
8. **Short sessions work.** Removing the minimum duration fixed a real operational blocker.
9. **Explicit late/after-end policy.** Late within the shift is allowed and flagged; after shift end is blocked with a clear reason and an approval path.
10. **Safe auto-close.** It never writes an `out` before its `in`, is idempotent, audited, and excluded from worked-time totals.
11. **Timezone discipline in the main paths.** UTC is stored and serialised explicitly; local display values are not mislabelled as UTC.
12. **Useful context after a shift.** Next-shift resolution and the completed/re-entry copy tell employees when to return and how to request approval.
13. **Additive, history-preserving migrations.** No operational rows are deleted, and the diagnostics script is read-only by default.
14. **Test coverage.** The lifecycle has dedicated tests for orphan rejection, shift inheritance, night shifts, re-entry, next-shift resolution, idempotency, auto-close ordering, and pairing.
15. **Testable UI state.** The pure Node module keeps the lifecycle-to-UI mapping out of the DOM and independently testable.

---

## 10. Known flaws and residual risks

### 10.1 Summary table

| ID | Flaw | Severity | Area |
|---|---|---|---|
| F1 | Approval records at approval time, not request time | High | Approvals |
| F2 | Approval re-resolves today's shift instead of the request's shift | High | Approvals |
| F3 | Clock-out is not safe across multiple processes/workers | High | Concurrency |
| F4 | Manual attendance create/patch bypasses the lifecycle | High | Admin integrity |
| F5 | Missing-clock-out correction can set `out` before `in` | Medium | Admin integrity |
| F6 | Duplicate pending approval requests are possible | Medium | Approvals |
| F7 | `TIMEZONE` defaults to UTC with no `.env` loader | High | Deployment |
| F8 | Auto-close runs only at startup unless scheduled | Medium | Operations |
| F9 | 20-second token TTL makes approval requests time-sensitive | Medium | Kiosk UX |
| F10 | Idempotency lookup happens before signature/kiosk verification | Low | Security |
| F11 | `list_missing_clockouts` serialises timestamps without explicit `Z` | Low | Timestamps |
| F12 | Approved re-entry creates multiple sessions on one shift date | Medium | Reporting |
| F13 | Legacy `/api/signin` skips early-departure confirmation and can mask blocked state | Medium | Legacy |
| F14 | Completion detection is event-order based, not session-id based | Low | Lifecycle |
| F15 | `/api/clock/request` accepts `staff_id` without a token | Low | Authorisation |
| F16 | Replaying a token with a different action returns the original action | Low | Idempotency |
| F17 | Startup auto-close swallows all exceptions | Low | Observability |
| F18 | Auto-close ignores open sessions older than 7 days | Low | Operations |

### 10.2 Details

#### F1 — Approval records at approval time, not request time

`create_attendance_for_approval()` receives `now=utcnow()` from the approve endpoint and writes that timestamp. The request's `requested_at` is ignored.

**Impact:** an employee who requests an out-of-window clock-in at 17:00 and is approved at 23:00 gets a 23:00 clock-in. An approved clock-out requested at 17:00 but approved later records the later time. Hours, lateness, and departure flags are therefore wrong, and a cross-midnight approval can land on the wrong local day.

**Recommendation:** store the requested effective time and shift context on the approval; write the attendance at the requested time (or an explicit manager-entered effective time), not the decision time.

#### F2 — Approval re-resolves today's shift

For an approved clock-in, the service calls `resolve_today()` at approval time and uses `shift_date_for(local, shift)`. The `ApprovalRequest` row stores no `shift_id` or `shift_date`.

**Impact:** approving a request the next day (or after a reassignment/exception change) attaches the new session to a different shift, or to no shift. The approved session can then look like a different day's work or become unpaired in reporting.

**Recommendation:** add `shift_id` and `shift_date` (and optionally the resolved window) to `ApprovalRequest` at request time; use them when creating the approved attendance. Re-resolve only as a fallback for legacy requests.

#### F3 — Clock-out is not safe across multiple processes/workers

`ATTENDANCE_LOCK` is an in-process `RLock`. The unique `open_key` index protects concurrent **clock-ins**, but clock-out has no equivalent constraint:

- two workers can both read the same open `in`;
- both clear `open_key` (the second update is a no-op);
- both insert an `out` with the same `paired_in_id`;
- `paired_in_id` is indexed but **not unique**.

**Impact:** duplicate clock-outs and double-counted sessions if the app is run with multiple uvicorn workers, multiple hosts, or a process manager that starts more than one instance. SQLite itself serialises writes, but it does not prevent both transactions from inserting different `out` rows.

**Recommendation:** add a partial unique index on `(paired_in_id)` for non-undone `out` rows (SQLite supports partial indexes), and/or make the close a conditional `UPDATE ... WHERE open_key IS NOT NULL` and check the affected row count before inserting. Alternatively, document and enforce a single-worker deployment until moving to a server database.

#### F4 — Manual attendance create/patch bypasses the lifecycle

`POST /api/admin/attendance` inserts an `Attendance` row directly with no `open_key`, `paired_in_id`, or shift context. `PATCH /api/admin/attendance/{id}` can change `type` without updating `open_key` or `paired_in_id`.

**Impact:** changing an open `in` to `out` leaves `open_key` set on an `out` row. `find_open_session()` no longer sees an open session (it filters `type="in"`), but the unique index still holds `str(staff_id)`, so the next legitimate clock-in fails with `IntegrityError` and is reported as `session_already_open`. Manual `in` rows without `open_key` are only found by the legacy fallback, and a second manual `in` can hide the first. This can corrupt state without any lifecycle guard.

**Recommendation:** route manual create/correct through the lifecycle service, or explicitly recompute `open_key`/`paired_in_id` and reject transitions that would create two open sessions or an out without an in. Add regression tests for type flips and manual inserts.

#### F5 — Missing-clock-out correction can set `out` before `in`

`correct_missing_clockout()` updates the auto-close row's timestamp and flag after checking only that the row is flagged `missing_clock_out`. It does not verify the new timestamp is at/after the paired clock-in or within the shift.

**Impact:** a manager can create a negative session (`out < in`), which the monthly report will exclude or mis-total.

**Recommendation:** load the paired clock-in, require `corrected_out >= in.timestamp`, and validate against the shift window (or require an explicit override reason).

#### F6 — Duplicate pending approval requests

`POST /api/clock/request` always inserts a new row. Re-scanning or tapping the button repeatedly creates multiple pending requests for the same employee/action/reason.

**Impact:** manager queues fill with duplicates; approving one leaves the others pending, and a later approval can create an unexpected second session.

**Recommendation:** deduplicate by `(staff_id, action, reason_code, shift_date, status="pending")` or supersede the previous pending request. Store shift context at the same time (F2).

#### F7 — `TIMEZONE` defaults to UTC with no `.env` loader

`get_settings()` reads `TIMEZONE` from the process environment and defaults to `"UTC"`. There is no `.env` loading in `app/config.py`.

**Impact:** starting the service without the variable silently evaluates every shift three hours off in Kampala. Late/early/after-end decisions, shift dates, and reports all shift. This already caused a live deployment issue once.

**Recommendation:** set `TIMEZONE` in the service/container definition, validate it at startup, and expose it in `/health` so drift is visible. Consider failing fast outside development when it is unset.

#### F8 — Auto-close runs only at startup unless scheduled

`close_missing_sessions()` is called once in the FastAPI lifespan. The repo provides `scripts/close_sessions.py` for a schedule, but nothing in the app schedules it.

**Impact:** a long-running server will not auto-close missing clock-outs until restart. Sessions stay open and the kiosk keeps treating the employee as clocked in.

**Recommendation:** schedule the script every 15–30 minutes (Task Scheduler/cron) or run an in-process periodic task. Monitor its output.

#### F9 — 20-second token TTL makes approval requests time-sensitive

The scan token lives for 20 seconds. The completed-state approval button uses that token. If the employee reads the message and taps later, the request fails with `invalid_token`/`expired` and the kiosk asks them to scan again.

**Impact:** avoidable friction at the kiosk, especially for the exact users who need help (completed/after-end).

**Recommendation:** allow a short, explicit re-scan flow in the UI (already partially present), or issue a separate approval token with a longer TTL, or allow the request endpoint to accept a fresh scan. Do not silently extend the clock token.

#### F10 — Idempotency lookup happens before signature/kiosk verification

`/api/clock` extracts the `jti` from the token payload and, if an attendance row already exists for it, returns that row **before** verifying the signature, expiry, or kiosk binding.

**Impact:** a forged or tampered token whose payload reuses a known `jti` can retrieve that attendance row. The `jti` is random, so exploitability is low, and the attacker generally needs a leaked token, but the check ordering is wrong.

**Recommendation:** verify the token first, then perform the idempotency lookup. If the token is used, only return the row when the signature, staff, and kiosk all match.

#### F11 — `list_missing_clockouts` serialises timestamps without explicit `Z`

`app/routers/approvals.py` returns `a.timestamp.isoformat().replace("+00:00", "Z")`. Stored values are naive UTC, so the result has **no** `Z` or offset.

**Impact:** this endpoint violates the project's own timestamp invariant and can be misread by clients as local time.

**Recommendation:** use the shared `iso_utc()` helper, as the other attendance endpoints do.

#### F12 — Approved re-entry creates multiple sessions on one shift date

An approved re-entry inserts a second `in`/`out` pair on the same `shift_date`. `_pair_sessions()` and the monthly report group by shift date and will show two sessions for that day; `completed_session_for_shift()` remains true.

**Impact:** reporting can show two sessions and sum both durations for one shift date. That may be legitimate extra work, but it is not explicitly labelled as an approved re-entry in the monthly totals, and `days_present` still counts one day. Managers may misinterpret the extra hours.

**Recommendation:** decide the product rule (count both sessions as worked time vs. only the first), tag approved re-entry sessions explicitly, and add a monthly-report test for this case. At minimum, surface both event ids and the approval reference.

#### F13 — Legacy `/api/signin` skips early-departure confirmation and can mask blocked state

The legacy endpoint closes an open session immediately, without the `confirm_required` step used by `/api/clock`. It also checks `attendance_within_cooldown()` **before** the lifecycle state. Within the cooldown window (default 60 seconds), a re-entry attempt can return `already_recorded` for the recent event instead of the lifecycle's `shift_already_completed`.

**Impact:** legacy clients can clock out early without confirmation, and can receive an inconsistent status for a completed shift. The legacy endpoint is still reachable.

**Recommendation:** deprecate and remove it, or route it through the same `blocked_detail()` and confirmation logic as `/api/clock`. Move the cooldown check after the lifecycle decision.

#### F14 — Completion detection is event-order based, not session-id based

`completed_session_for_shift()` scans non-undone rows for a clock-in followed by a later clock-out on the same shift date. It does not require the clock-out to be paired to that clock-in.

**Impact:** a legacy or manually inserted orphan `out` after an `in` can mark the shift complete even though the session was never properly closed. With the normal lifecycle this is unlikely because orphan outs are refused and `paired_in_id` is set.

**Recommendation:** prefer `paired_in_id` when present; fall back to ordering only for legacy rows. Add a test with an orphan out.

#### F15 — `/api/clock/request` accepts `staff_id` without a token

If no token is supplied, the endpoint accepts any `staff_id` and creates a pending approval request, subject only to the kiosk header.

**Impact:** anyone who can reach the kiosk API can create approval noise for any employee. Managers still decide, so no attendance is written, but the queue can be spammed.

**Recommendation:** require a valid token or an authenticated kiosk identity for request creation; rate-limit per kiosk/staff.

#### F16 — Replaying a token with a different action returns the original action

If a token was used for `in`, replaying the same token with `action="out"` hits the idempotency branch and returns the original `in` row with `status="ok"`.

**Impact:** a buggy or malicious client can interpret the response as a successful clock-out when no clock-out occurred.

**Recommendation:** store the action with the idempotency record (or compare the attendance `type` to the requested action) and return a 409 `token_action_mismatch` when they differ.

#### F17 — Startup auto-close swallows all exceptions

`app/main.py` wraps `close_missing_sessions()` in `except Exception: pass`.

**Impact:** schema or data problems that prevent auto-close are invisible in the logs.

**Recommendation:** log the exception with context and surface a health/metrics signal; do not silently pass.

#### F18 — Auto-close ignores open sessions older than 7 days

`close_missing_sessions()` only scans clock-ins from the last 7 days (`cutoff = now - timedelta(days=7)`).

**Impact:** a session older than 7 days can remain open forever and keep the employee in `open_session`. This is rare but possible after long downtime.

**Recommendation:** make the window configurable, or scan all open sessions (there should be very few) and rely on the cut logic.

---

## 11. Recommended fixes, prioritised

### P0 — correctness and integrity

1. **Approval effective time and shift context (F1, F2).**
   Add `shift_id`, `shift_date`, and an effective timestamp to `ApprovalRequest`; capture them at request time. Write approved attendance at the requested/effective time, not the decision time. For approved clock-outs, close at the requested time or an explicit manager-entered time.
2. **Cross-process clock-out safety (F3).**
   Add a partial unique index on `paired_in_id` for non-undone `out` rows, and/or perform a conditional `UPDATE` on `open_key` and verify the affected row count before inserting the `out`. Document the supported worker model.
3. **Manual attendance integrity (F4, F5).**
   Route manual create/correct through the lifecycle service, or recompute and validate `open_key`/`paired_in_id` on every edit. Reject `out < in` in missing-clock-out correction.

### P1 — operational and UX correctness

4. **Make timezone explicit and observable (F7).**
   Set `TIMEZONE` in the deployment definition; validate at startup; expose it in `/health`; fail fast outside development if unset.
5. **Schedule auto-close and log failures (F8, F17, F18).**
   Run `scripts/close_sessions.py` every 15–30 minutes; replace the silent `except` with logged errors; make the lookback window configurable.
6. **Deduplicate approval requests and fix token handling (F6, F9).**
   Supersede pending duplicates, store shift context, and provide a deliberate re-scan or longer-lived approval token flow.
7. **Harden legacy and reporting paths (F11, F12, F13, F14).**
   Use `iso_utc()` in `list_missing_clockouts`; tag approved re-entry sessions and add a monthly-report test; route or retire `/api/signin`; prefer `paired_in_id` in completion detection.

### P2 — hardening

8. **Verify before idempotency (F10, F16).**
   Check signature, kiosk, expiry, and action before returning an existing row.
9. **Authenticate approval requests (F15).**
   Require a token or authenticated kiosk identity; rate-limit.
10. **Add regression tests for the gaps.**
    Multi-engine concurrent clock-out, manual type flip, approval requested-vs-approved time, approved re-entry reporting, and missing-clock-out correction ordering.

---

## 12. Verification performed

### Automated tests

```
.\.venv\Scripts\python.exe -m pytest -q -rs
264 passed, 6 skipped, 2 warnings in 46.25s
```

The 6 skips are all recognition fixture images that are not present in `tests/fixtures/` (`one_face.jpg`, `no_face.jpg`, `two_faces.jpg`, and variants). They are unrelated to the lifecycle. The Node kiosk-state tests pass (5 tests) when Node is installed.

### Live health and diagnostics

```
GET http://127.0.0.1:8000/health  ->  {"status":"ok"}

scripts/diagnose_attendance.py (TIMEZONE=Africa/Kampala, read-only):
Scanned 5 attendance rows in the last 90 day(s).
Orphan clock-outs: 0
Open sessions on legacy fallback (NULL open_key): 0
All open sessions (awaiting clock-out or correction): 0
Negative sessions (clock-out before clock-in): 0
Employees with duplicate explicit open sessions: 0
Stale open_key on closed sessions: 0
Clock-outs missing paired_in_id link: 0
```

No changes were made by the diagnostics script.

### What the tests cover

- Orphan clock-outs are rejected with no row.
- Clock-out inherits the open session's shift and shift date.
- Night-shift clock-out after midnight stays on the start date.
- Completed shifts reject re-entry; approval is the only path.
- Next-shift resolution after a shift ends.
- Same-day patterns ignore stale `overnight` flags.
- Auto-close never writes `out < in`, is idempotent, and pairs correctly.
- Session pairing prefers `paired_in_id`.
- Idempotent token replay.
- Kiosk state classification and blocked copy.

### What the tests do **not** currently cover

- Two independent processes closing the same session.
- Approval requested-time vs decision-time behaviour.
- Manual attendance type flips and their effect on `open_key`.
- Approved re-entry in the monthly report.
- Missing-clock-out correction ordering.
- Timezone-unset startup behaviour.

These gaps correspond directly to the P0/P1 findings above.

---

## 13. Deployment notes

- **Start without `--reload`** in production-like runs, and confirm the worker's start time is after the last source change.
- **Export `TIMEZONE=Africa/Kampala`** in the service/container/shell that starts uvicorn. There is no `.env` loader. Without it, the app silently uses UTC.
- **Run a single worker** until F3 is fixed, or add the DB-level clock-out guard. The in-process lock does not protect multiple workers.
- **Schedule `scripts/close_sessions.py`** every 15–30 minutes. Startup-only auto-close is not sufficient for a long-running server.
- **Run `scripts/diagnose_attendance.py`** after deployment and periodically. It is read-only by default and reports orphans, negative sessions, duplicate open sessions, stale `open_key`s, and missing `paired_in_id` links.
- **Keep the database backed up** before applying schema changes. Migrations are additive and idempotent, but the unique indexes will fail to create if pre-existing duplicate non-null values exist; the diagnostics script helps find them first.

---

## 14. Reconciliation with the previous report

`docs/Shift Lifecycle Audit - Report.md` documents the original root causes and the fixes. This report is the current-state view. They agree on the implemented rules and on the test result (`264 passed, 6 skipped`).

Two details are intentionally updated here:

- The earlier report recorded 3 live attendance rows at the time of its diagnostics; the current read-only run sees 5 rows in the 90-day window. The integrity counts are still all zero.
- This report adds the residual-risk analysis (approval timing, multi-process clock-out, manual edits, timezone drift, scheduling) that the remediation log did not cover.

For the remediation history and the deployment runbook, keep using `docs/Shift Lifecycle Audit - Report.md`. For behaviour and current risks, use this document.

---

## Appendix A — Stable message codes

| Code | Meaning | Typical HTTP |
|---|---|---|
| `shift_already_completed` | A paired session already exists for this shift date | 409 |
| `reentry_approval_required` | Completed; setting ON, but approval still required | 409 |
| `session_already_open` | An open session exists; clock out first | 409 |
| `no_open_session` | Clock-out requested with nothing open | 409 |
| `no_shift_request_approval` | No shift resolved | 409 |
| `before_window` | Too early for the clock-in window | 409 |
| `after_shift_end` | Shift has ended | 409 |
| `clock_out_early_confirm` | Early clock-out needs confirmation | 409 |
| `confirm_required` | Generic confirmation envelope | 409 |
| `invalid_token` | Signature, kiosk, or staff invalid | 400 |
| `expired` / `used` | Stale token; revalidate and rescan | 400 (or 409 if now blocked) |

## Appendix B — Key source files

| File | Responsibility |
|---|---|
| `app/attendance_service.py` | Authoritative state, open-session lookup, record/close helpers |
| `app/shifts.py` | Shift resolution, overnight derivation, `evaluate()`, shift dates |
| `app/approvals.py` | Approval row creation, missing-clock-out auto-close |
| `app/routers/clock.py` | `/api/clock`, `/api/clock/request`, undo, idempotency |
| `app/routers/signin.py` | `/api/scan`, legacy `/api/signin` |
| `app/routers/approvals.py` | Approve/reject, missing-clock-out queue and correction |
| `app/routers/review.py` | Review assignment through the lifecycle |
| `app/routers/attendance.py` | Session pairing, reporting, manual attendance |
| `app/clock_tokens.py` | Token issue/verify/consume, TTL, kiosk binding |
| `app/models.py` | `Attendance`, `ApprovalRequest`, `ClockToken` |
| `app/db.py` | Additive migrations and unique indexes |
| `static/signin_state.js` | Pure kiosk state classification |
| `static/signin.js` | Kiosk rendering, confirmation, approval request |
| `scripts/diagnose_attendance.py` | Read-only integrity diagnostics |
| `scripts/close_sessions.py` | Scheduled missing-clock-out close |
| `tests/test_attendance_lifecycle.py` | Lifecycle regression tests |
