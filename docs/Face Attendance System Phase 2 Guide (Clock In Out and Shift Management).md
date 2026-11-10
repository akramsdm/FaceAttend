# Face Attendance System: Phase 2 Guide (Clock In/Out and Shift Management)

Nov 8, 2026 · @AKRAM

The first guide produced a working system. This guide upgrades it in eleven small steps: the scanner only identifies, a Clock in or Clock out button states the intent, and shifts set by managers decide what is allowed. Every step starts from the code you already have.

## 1. What changes

| Area | Now | After Phase 2 |
| --- | --- | --- |
| Sign-in | Scan alternates in and out | Scan identifies, then the person presses Clock in or Clock out |
| Validation | Cooldown only | Server checks shift window, open session and approvals |
| Shifts | One global work start | Patterns with days and times, assigned per person, customisable per person and per date |
| Roles | Admin | Admin, manager, staff |
| Mistakes | Fix later by admin | 10-second undo, missing clock-out handling, approval queue |

## 2. Defaults assumed (change any before you start)

| Decision | Default |
| --- | --- |
| Late arrival | Allowed and flagged, not blocked |
| Early clock-in window | 30 minutes before shift start (per pattern) |
| Grace period | 10 minutes after start (per pattern) |
| Shifts per person per day | One |
| Rotating rosters | Out of scope; use patterns with effective dates plus date exceptions |
| Leave and public holidays | Out of scope except a manager marking a day off as an exception |
| Scan token | Valid 20 seconds, single use, bound to the person and the kiosk |
| Undo | 10 seconds after a clock event |
| Missing clock-out | Session closes 4 hours after shift end and is flagged for the manager |
| Night shifts | Belong to the date they start |
| Time source | Server clock in the TIMEZONE setting, never the kiosk clock |

## 3. Data model additions

| Entity | Key fields |
| --- | --- |
| Pattern | id, name, owner\_staff\_id (null for shared patterns, set for a personal copy), early\_window\_min, grace\_min, break\_min, is\_default |
| PatternDay | pattern\_id, weekday (0 to 6), start\_time, end\_time, overnight, enabled |
| Assignment | id, staff\_id, pattern\_id, effective\_from, effective\_to (null while current) |
| ShiftException | id, staff\_id, date, kind (day\_off / custom\_hours), start\_time, end\_time, reason, created\_by |
| ClockToken | jti, staff\_id, kiosk\_id, issued\_at, expires\_at, used\_at |
| ApprovalRequest | id, staff\_id, action, reason\_code (no\_shift / out\_of\_window / early\_out), requested\_at, status, decided\_by, decided\_at, decision\_note |
| Attendance (changed) | adds shift\_id, shift\_date, flag (late / early\_departure / overtime / out\_of\_window / missing\_clock\_out), approved\_by, undone\_at |
| Admin (changed) | role becomes admin or manager; managers carry a department scope |

Shipped default patterns: Office hours (Mon to Fri 08:00 to 17:00), Early, Late, Weekend and Night (22:00 to 06:00 overnight).

## 4. Rules

### 4.1 Which shift applies to a person on a date

Use the first match: a date exception, then the person's personal pattern, then their assigned shared pattern, otherwise no shift. Bulk assigning a department creates one Assignment per person. Changing a pattern ends the old assignment the day before and starts a new one, so history stays correct.

### 4.2 What the buttons allow

| Situation | Clock in | Clock out | Message shown |
| --- | --- | --- | --- |
| Window open, not clocked in | Enabled | Disabled | You are not clocked in |
| Clocked in | Disabled | Enabled | Clocked in since 08:02 |
| Before the early window | Disabled | Disabled | Your shift starts at 08:00; clock-in opens at 07:30 |
| After start plus grace | Enabled, flagged late | Disabled | You are late; this will be recorded |
| No shift today | Disabled | Disabled | No shift assigned; request approval |
| Clock out before shift end | n/a | Enabled with confirmation | Your shift ends at 17:00; clock out early? |
| Clock out after shift end | n/a | Enabled, flagged overtime if beyond a set margin | Recorded |

The server enforces all of this. Disabled buttons are a convenience, not the control.

### 4.3 Validation of patterns

End after start unless marked overnight; no overlapping assignments for one person; one shift per person per day; break shorter than the shift; exceptions need a reason.

## 5. Prompts

Use Prompt 0 from the first guide, plus this addition at the start of every session:

```text
The codebase is already functional and tested. Do not break existing behaviour; the existing tests must stay green. Change only what the step asks. Never claim something works unless you ran it, and paste raw output. Defaults for this phase: late arrivals allowed and flagged; one shift per person per day; scan token valid 20 seconds and single use; undo window 10 seconds; night shifts belong to the date they start; server clock only.
```

### Step A: read-only analysis of the current code

```text
Step A (read-only, change nothing). Report with file paths, function names and line numbers:
1. Where the in/out decision is made today (the alternate rule and the cooldown), and every place that calls it.
2. The current Attendance, Admin and Setting models and the migration approach (create_all, Alembic, or other). Which settings hold work start and grace?
3. The current /api/signin response shape and how static/signin.html uses it.
4. How admin roles and require_admin work, and how the admin pages are organised.
5. Which existing tests touch signin, attendance, late flags and exports (names only).
6. Anything that assumes a calendar day rather than a shift date (late logic, hours pairing, monthly report).
Finish with a short plan of the smallest migration for the data model in section 3 of the Phase 2 guide, and list the risks. Stop.
```

### Step B: schema migration

```text
Step B: back up the database file first and tell me the command. Then add the tables and columns from the data model (Pattern, PatternDay, Assignment, ShiftException, ClockToken, ApprovalRequest; new Attendance columns; Admin role with department scope) using the project's migration approach found in Step A. Existing rows must survive: existing attendance rows get nullable new columns; existing admins become role 'admin'. Seed the five default patterns (Office hours, Early, Late, Weekend, Night). Include a downgrade path or a documented restore from backup.
Test on a COPY of the database: upgrade, check old data still reads, run the existing tests. Paste raw output. Stop.
```

### Step C: shift rules module (pure logic)

```text
Step C: create app/shifts.py with pure functions and no database or web code.
- resolve_shift(staff_id, date, data) following the order exception, personal pattern, shared pattern, none; returns start, end, overnight, early_window, grace, break.
- shift_date_for(timestamp, shift) so a night shift belongs to the date it starts.
- evaluate(now, shift, open_session, margin_overtime) returns allowed_actions, a message code, and the flag that would be applied (late, early_departure, overtime, none).
- validate_pattern(pattern) per section 4.3.
Table-driven tests covering every row of the rules table in section 4.2, exact boundaries (the minute the window opens, the minute grace ends), a night shift crossing midnight, a day-off exception, no assignment, an assignment ending yesterday. Paste pytest -v output. Stop.
```

### Step D: patterns, assignments, exceptions and the manager role (API)

```text
Step D: API with require_role. Managers: create, edit and delete patterns they own; create a personal copy of a pattern for one person ('customise for this person'); assign a pattern to selected staff or a department (one Assignment per person, ending the previous one the day before); add date exceptions (day off or custom hours) with a required reason; GET a person's resolved shifts for a date range (the week preview). Managers only see staff in their department scope; admins see all. All writes go to AuditLog. Validate with validate_pattern.
Tests: role scoping, bulk assign closes old assignments, personal copy does not change the shared pattern, exception wins over pattern, validation errors. Paste pytest -v output. Stop.
```

### Step E: manager interface

```text
Step E: static pages for managers (plain HTML/JS, same style and CSRF handling as the admin pages): pattern list; a weekly grid editor (seven rows with an on/off toggle, start, end and an overnight checkbox, plus early window, grace and break); assign page (pick pattern, pick people or a department, effective date, summary before confirming); 'customise for this person' button; exceptions form; a 'preview this week' view per person. Text labels, keyboard-usable, clear validation messages.
No backend changes unless a field is missing. Give me manual steps for each page. Stop.
```

### Step F: scan endpoint and token

```text
Step F: replace the in/out logic in /api/signin. New POST /api/scan (image, kiosk_id): runs the existing recognition and quality checks; on a match returns status, name, today's shift summary, allowed_actions and message from evaluate(), and a signed single-use ClockToken valid 20 seconds bound to the person and kiosk. Never return scores or other staff names. Rejected or ambiguous attempts keep the existing logging and review-queue behaviour. Keep the old route working only if existing tests need it, and say so.
Tests with a mocked recognizer: token issued only on a match, token bound to person and kiosk, expired token rejected, used token rejected, allowed_actions per situation. Paste pytest -v output. Stop.
```

### Step G: clock, undo and approval requests

```text
Step G: POST /api/clock (token, action in/out). Verify and consume the token, recompute evaluate() on the server, and refuse anything not in allowed_actions. Clock out before shift end needs confirm=true. Create the Attendance row with shift_id, shift_date and the flag. Idempotent: repeating the same token returns the same result, never a second row. POST /api/clock/undo (token or event id) works for 10 seconds after the event, sets undone_at and never deletes the row. If the action is blocked for no_shift, out_of_window or early_out, POST /api/clock/request creates an ApprovalRequest.
Tests: each allowed and blocked case, double press, expired token, confirm needed for early out, undo within and after 10 seconds, request creation. Paste pytest -v output. Stop.
```

### Step H: kiosk interface

```text
Step H: rewrite the kiosk flow in static/signin.html and its JS. Step 1: camera scan loop as now, calling /api/scan. Step 2 on a match: a confirmation screen with name, shift summary and time, and two large buttons Clock in and Clock out. Disabled buttons show the reason from the message. A 20 second countdown ends in a reset. Clock out before shift end asks for confirmation. After a successful action show the result for about 4 seconds with an Undo button for 10 seconds, then reset. When blocked, show 'Request approval' (calls /api/clock/request) and a reception contact line. Handle camera blocked, service unavailable, expired token. No scores or other names ever shown.
Give me manual test steps for every row of the rules table. Stop.
```

### Step I: approvals and missing clock-outs

```text
Step I: (1) approval queue API and page for managers (department scope): list pending requests with the person, reason, time and today's shift; approve (creates the attendance row with flag out_of_window or early_departure and approved_by) or reject with a note; both audited. (2) scripts/close_sessions.py, also run at startup and on a schedule you propose, closing any open session 4 hours after shift end (a setting), marking the row missing_clock_out and listing it for the manager, who can enter the real time with a required reason. Missing clock-outs are never counted as worked time automatically.
Tests: approve, reject, department scoping, session closing at the exact cut-off, correction needs a reason. Paste pytest -v output. Stop.
```

### Step J: reports

```text
Step J: update attendance views and exports to use shifts. Late means the first clock-in is after the shift start plus grace. Add early departure and overtime flags, hours worked per shift (clock-in to clock-out minus break), missing clock-outs listed separately and excluded from totals, and group by shift_date so night shifts do not split. Update the monthly CSV columns and the dashboard counts (present, absent against the assigned shift, late, missing clock-out).
Tests: night shift hours across midnight, break subtraction, late at the exact boundary, absent only when a shift was assigned. Paste pytest -v output and a sample CSV. Stop.
```

### Step K: regression, smoke test and docs

```text
Step K: run the full test suite and paste the counts; any failure must be diagnosed as old or new (compare with the commit before Phase 2). Run the recognition evaluation script and confirm results are unchanged. Update the README: roles, shift concepts, the clock flow, settings, and a Known Limitations section (one shift per day, no rosters or leave, token and undo windows). List everything you did not do. Stop.
```

## 6. Manual test matrix

| Scenario | Expected result |
| --- | --- |
| Staff scans 45 minutes before shift | Both buttons disabled, message shows when clock-in opens |
| Scans inside the early window | Clock in enabled, recorded on time |
| Scans 20 minutes after start | Clock in enabled, recorded as late |
| Clocked in, scans again | Clock in disabled; Clock out enabled |
| Clock out at 15:00 on a 17:00 shift | Confirmation shown; recorded as early departure |
| Presses a button 30 seconds after the scan | Token expired, must scan again |
| Presses Undo within 10 seconds | Event marked undone, row kept |
| Person with no shift scans | Request approval offered; manager sees it |
| Manager sets a day off for one person | That person cannot clock in that day |
| Night shift worker clocks out at 06:00 | One shift, dated by its start day, hours correct |
| Forgets to clock out | Closed after the cut-off, listed for the manager, excluded from hours |
| Manager assigns Early pattern to a department | Each person gets an assignment; old ones end yesterday |
| Manager customises one person's Friday | Shared pattern unchanged; only that person affected |

## 7. Order and time

Steps A to E first (analysis, data, rules, manager API and screens), then F to H (the new clock flow), then I to K. If time is short, cut in this order: the weekly preview, department bulk assign, overtime flags. Keep the token check, server-side validation and the approval path, because those are what make the flow safe against mistakes.

If a step fails twice, use the debug prompt from the first guide (section 7) instead of rewording the step.
