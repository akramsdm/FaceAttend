# Face Attendance System: Phase 3 Guide (Simpler, Robust Shift Management)

Nov 9, 2026 · @AKRAM

The shift rules engine from Phase 2 is sound and stays. This phase fixes how shifts are stored, listed and edited: personal schedules stop cluttering the Schedules list, everyday tasks live on one screen, and the robust features (per-person customisation, day changes, bulk and history) stay available. Twelve small steps, each with a prompt.

## 1. What stays, what changes

| Stays | Changes |
| --- | --- |
| `resolve_shift`, `evaluate`, `shift_date_for`, night-shift handling | Personal copies are hidden from the Schedules list and managed automatically |
| Effective-dated assignments and history | Assigning a shared schedule never creates a copy |
| Day exceptions that override schedules | Four admin pages become two tabs: People and Schedules |
| Roles, department scope, audit log, token and clock flow | Wording, permissions and delete rules are made consistent |

## 2. The clutter problem and its cause

The Patterns table currently mixes two kinds of rows: shared schedules (Early, Late, Night, Office hours, Weekend) and personal copies such as `Early (personal:staff01)`. A personal copy is created by the Customise action, and your report shows the assign flow can also lead to one. Both end up in the same list that managers edit and delete from.

Target behaviour: a copy exists only when a person's week genuinely differs from their base schedule, it is marked as personal, and it is never shown in the Schedules list.

| Concept | Stored as | Where it is visible |
| --- | --- | --- |
| Schedule | Pattern with `kind = shared` | Schedules tab |
| Custom week for one person | Pattern with `kind = personal`, `based_on_pattern_id`, owner staff | Only on that person's row as 'Custom (based on Office hours)' |
| Which schedule someone follows | Assignment with effective dates | People tab; History panel |
| One-off change to a date | ShiftException | People tab as a 'Changed' badge; calendar |

## 3. Rules and defaults (change any before you start)

| Decision | Default |
| --- | --- |
| Assigning a shared schedule | Creates an Assignment only, never a copy |
| When a personal copy is created | Only when a saved week differs from the base schedule (copy-on-write) |
| Saving a week identical to the base | Treated as a reset: personal copy removed, base assigned |
| Edits to a personal schedule | Applied from an effective date (default today) as a new version; the old version is archived and kept for history |
| Unreferenced personal copies | Deleted automatically; archived versions that history or attendance references are kept |
| Default schedules (the five shipped) | Editable by admins only; managers can duplicate them |
| Manager edit rights | Schedules they created, and personal schedules of staff in their department |
| Deleting a schedule | Blocked while any current or future assignment uses it, with a count |
| Day changes | Reason required; ranges up to 62 days; one exception per day underneath |
| Holidays | A bulk day off for everyone or one department |
| One shift per person per day | Unchanged |
| Out of scope | Split shifts, rosters, leave balances, shift swaps, staff requests (listed in section 9) |

## 4. Backend rules to implement

- **Copy-on-write:** `save_personal_week(staff, days, windows, effective_from)` compares against the base; identical means reset, different means a new personal version, assigned from `effective_from`.
- **Reset:** ends the personal assignment the day before, assigns a chosen shared schedule, archives the personal version, then garbage-collects anything unreferenced.
- **Listing:** the schedules endpoint returns only `kind = shared`. A separate people endpoint returns each person's schedule label, source (shared, custom, changed) and the seven resolved days of a chosen week in one call.
- **Impact preview:** assign, save-week and bulk day-change accept `dry_run=true` and return who is affected, which assignments would end and any conflicts, without writing.
- **History:** an endpoint lists a person's assignments, personal versions and exceptions with dates, actor and reason.
- **Permissions:** `created_by` on patterns; one `can_edit_pattern(admin, pattern)` function used everywhere.
- **Validation on save:** end after start unless the shift ends next day, break shorter than the shift, no overlapping assignments, reason on every day change.
- **Past dates:** a change touching dates that already have attendance returns a warning that reports for those days may differ.

## 5. Interface design

### 5.1 People tab (the home screen)

| Element | Behaviour |
| --- | --- |
| Table | Person, department, schedule label, a mini week strip (seven days as times or Off), badges Custom or Changed |
| Filters | Department, schedule, search, 'working today' |
| Bulk | Tick people, then **Assign schedule**; starts today by default, a date picker under Advanced; confirmation names who is affected and whose current schedule ends |
| Row: Edit week | Weekly grid pre-filled; saving creates a personal version and assigns it; **Reset to base** button |
| Row: Change a day | Dialog: date or range, day off or different hours, reason; shows any clash |
| Row: History | Side panel with schedules, custom versions and day changes over time |
| Holiday | Button to set a day off for everyone or a department |

### 5.2 Schedules tab

- A card per shared schedule with a plain-language summary, 'Used by 12 people', and Edit, Duplicate and Delete (disabled while in use).
- Week editor: presets (Mon to Fri 9 to 5), a 'copy Monday to all weekdays' button, an 'ends next day' label instead of an overnight checkbox, and Advanced for clock-in opens, counts as late after and break.
- Personal copies never appear here.

### 5.3 Optional views

A weekly calendar by department and a 'working today' list, both built from the same resolved-shift data.

### 5.4 Wording

| Now | Becomes |
| --- | --- |
| Pattern | Schedule |
| Exception | Day change |
| Early window | Clock-in opens (minutes before start) |
| Grace | Counts as late after (minutes) |
| Customise for this person | Edit week |

## 6. Prompts

Add this to the start of every session, with Prompt 0 from the first guide:

```text
The codebase is functional and tested. Do not rewrite app/shifts.py or change clock, scan or attendance logic. Change only what the step asks. The existing tests must stay green. Never claim something works unless you ran it; paste raw output and say 'not done' for anything not done. Back up the SQLite file before any data change and tell me the command.
```

### Step A: read-only diagnosis

```text
Step A (read-only, change nothing). Report with file paths, function names and line numbers:
1. Every code path that creates a Pattern row with owner_staff_id set (customise endpoint, assign endpoint, any frontend call that triggers it). Does POST /assignments ever create a copy? Quote the lines.
2. Run a query and paste results: all patterns with id, name, owner_staff_id, and the number of assignments, exceptions and attendance rows referencing each.
3. How GET /patterns builds its list and what the Patterns page renders, including why personal rows appear in the same table.
4. For each personal pattern, whether its days and windows are identical to its source pattern (compare to the shared pattern named in its name prefix).
5. How edit and delete permissions are checked for each role, and whether delete checks references.
End with the smallest plan to reach the target model in section 2 of the Phase 3 guide. Stop.
```

### Step B: schema and backfill

```text
Step B: first print the backup command and run it. Then add to patterns: kind ('shared' or 'personal'), based_on_pattern_id (nullable FK), created_by (nullable FK to admins), archived_at (nullable). Backfill: rows with owner_staff_id set become kind='personal' and, where the name prefix matches a shared pattern, based_on_pattern_id points at it; the five seeded patterns are kind='shared'. Use the project's idempotent ALTER approach; run it twice to prove it is idempotent.
Test on a COPY of the database. Paste the before and after row counts and the existing test results. Stop.
```

### Step C: cleanup of existing personal copies

```text
Step C: scripts/cleanup_personal_patterns.py with --dry-run as the default and --apply to write. For each personal pattern: if its days and windows are identical to its base shared pattern, repoint its assignments to the base and delete the copy; if different, keep it as kind='personal'; if nothing references it, delete it. Print a table of every decision and the reason. Never touch attendance rows. Record each action in AuditLog.
Run --dry-run on a COPY of the database and paste the report. Do not apply until I confirm. Stop.
```

### Step D: copy-on-write service and reset

```text
Step D: add a service module (not in the router) with save_personal_week(staff, days, windows, effective_from), reset_to_schedule(staff, pattern_id, effective_from) and gc_personal_patterns(). Rules: assigning a shared pattern never copies; a saved week identical to the base is a reset; a different week creates a new personal version and assigns it from effective_from, ending the previous assignment the day before and archiving the old personal version; archived versions referenced by assignments, exceptions or attendance are kept; others are deleted. Change POST /patterns/{id}/customise/{staff_id} to call this service and assign automatically; change GET /patterns to return only kind='shared'.
Tests: assign does not create a row, identical save is a reset, a different save creates one personal row, a second edit archives the first, reset removes the personal copy, gc keeps referenced rows, history is preserved. Paste pytest -v output. Stop.
```

### Step E: permissions and delete guard

```text
Step E: implement can_edit_pattern(admin, pattern) in one place: admin may edit any; manager may edit patterns with created_by equal to themselves and personal patterns of staff in their department; the five shipped defaults are admin-only (managers can duplicate). Set created_by on create. DELETE /patterns/{id} returns 409 with the number of current and future assignments when in use. Make the router use the function everywhere.
Tests for each role and case, including the manager who created a shared pattern being able to edit it. Paste pytest -v output. Stop.
```

### Step F: day changes, ranges, holidays and dry run

```text
Step F: POST /exceptions accepts a date or a start and end date (max 62 days) and creates one exception per day; reason required; custom hours need start and end. Add POST /exceptions/bulk for a holiday: a date range plus either everyone or a department (scope-filtered). Add dry_run=true to assign, save-week, exceptions and bulk exceptions: it returns affected people, assignments that would end, exceptions that would be replaced and a warning when dates already have attendance, and writes nothing.
Tests: range creates the right number of rows, bulk respects department scope, dry run writes nothing, past-attendance warning appears. Paste pytest -v output. Stop.
```

### Step G: people and history endpoints

```text
Step G: GET /api/admin/shifts/people?week_start&department&search&working_today returns, per staff in scope: id, name, department, schedule label, source (shared, custom, changed), based_on name, and the seven resolved days of that week. It must run in a constant number of queries, not one per person; show the query count in a test. GET /staff/{id}/history returns assignments, personal versions and exceptions with dates, actor and reason in time order.
Tests for scope filtering, working_today, a night shift in the week strip, and the history order. Paste pytest -v output. Stop.
```

### Step H: People tab

```text
Step H: build the People tab in static/admin/shifts/index.html and its JS per section 5.1: table with the mini week strip and badges, filters, bulk Assign schedule with a start date under Advanced and a dry-run confirmation listing who is affected, row actions Edit week (grid pre-filled, Reset to base), Change a day (date or range, reason, clash warning), History panel, and a Holiday button. Same shell, CSRF and error handling as the other admin pages. Text labels, keyboard use, the wording table in section 5.4.
No backend changes unless a field is missing. Give me manual steps for each action. Stop.
```

### Step I: Schedules tab and the week editor

```text
Step I: build the Schedules tab per section 5.2: cards with summary and 'Used by N people', Edit, Duplicate, Delete (disabled in use). The week editor has presets, 'copy Monday to all weekdays', an 'ends next day' label that sets overnight automatically, and Advanced for clock-in opens, counts as late after and break with the current defaults. Reuse the same editor component for Edit week on the People tab. Personal copies never appear.
Give me manual steps. Stop.
```

### Step J: calendar and working today (optional)

```text
Step J (optional): a weekly calendar by department and a 'working today' list built from the people endpoint, showing start, end, source badges and day changes. Read-only. Give me manual steps. Stop.
```

### Step K: cleanup, encoding and regression

```text
Step K: remove or redirect the old Assign, Exceptions and Preview pages and update the admin navigation. Add <meta charset='utf-8'> and re-save the shift pages as UTF-8 so the garbled characters are gone. Apply the cleanup script only after I have confirmed the dry run. Run the full suite and compare with the commit before Phase 3; run the recognition evaluation script and confirm results are unchanged. Update the README: schedule concepts, copy-on-write, roles, day changes, holidays, history. List everything not done. Stop.
```

## 7. Test matrix

| Scenario | Expected result |
| --- | --- |
| Assign Office hours to 10 people | 10 assignments, zero new pattern rows |
| Edit one person's Friday | One personal version appears on that person's row only; Schedules list unchanged |
| Save a week identical to the base | Treated as a reset, no personal copy remains |
| Edit a personal week again from next Monday | New version from that date; old version archived, history intact |
| Reset to Office hours | Personal copy gone from active use; assignment history intact |
| Delete a schedule in use | Refused with the number of people using it |
| Manager edits a schedule they created | Allowed; shipped defaults refused with a duplicate option |
| Day off for a 5-day range | Five exceptions, one reason, clock-in blocked those days |
| Company holiday | Everyone in scope has a day off; managers affect only their department |
| Dry run of any change | Shows who is affected and writes nothing |
| Change touching a day with attendance | Warning shown before saving |
| Night shift in the week strip | Shown against its start date with an ends-next-day label |
| Clock-in with a changed schedule | Uses the new schedule immediately |

## 8. Order and cutting

Steps A to G are the foundation; do them in order and commit after each. H and I deliver the new experience. If time is short, build H and skip J, and keep the dry-run confirmation, the delete guard and the cleanup script because they prevent mistakes and data clutter.

## 9. Optional extras, not in scope unless you choose them

- Shift swap between two staff (two day changes plus an approval rule).
- Staff self-service requests for a day off, reusing the approval queue.
- Notifications when a schedule changes.
- CSV import and export of rosters.
- Split shifts or rotating rosters (these change the one-shift-per-day rule and affect clock-in logic, reports and tests).
