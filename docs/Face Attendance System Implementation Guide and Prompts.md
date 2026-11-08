# Face Attendance System: Implementation Guide and Prompts

Nov 8, 2026 · @AKRAM

Fifteen small build steps, each with a prompt you paste into your coding assistant (Claude Code or similar) and a check you run yourself before moving on. Something demoable exists from step 9.

## 1. How to use this guide

- Do one step at a time and stop at the end of each prompt.
- Every prompt demands raw command output. If the assistant says 'done' without pasted output, ask for it.
- Commit after each passing step: `git add -A && git commit -m 'step N'`.
- If a step fails twice, use the debug prompt in section 7 instead of rewording the original.
- Paste Prompt 0 at the start of every new assistant session.

## 2. Decisions to lock first

| Decision | Choice | Why |
| --- | --- | --- |
| Backend | Python 3.11+, FastAPI | Quick to build, easy to test |
| Database | SQLite through SQLAlchemy | No server to run |
| Face detection | OpenCV YuNet (`cv2.FaceDetectorYN`) | pip install only, runs on CPU, returns landmarks |
| Face embedding | OpenCV SFace (`cv2.FaceRecognizerSF`) | Same library, no dlib or TensorFlow to compile |
| Frontend | Plain HTML and JavaScript served by FastAPI | No build step; uses the browser camera API |
| Auth | Server-side session cookie, hashed passwords | Simple and safe enough for the project |
| Running | Local `uvicorn`, no Docker | Avoids the setup problems of the last project |
| Fallback model | `face_recognition` or DeepFace | Only if SFace performs poorly on your webcam (decided after step 2) |

Both OpenCV models are pre-trained ONNX files from the OpenCV model zoo. Step 2 proves they work on your machine before anything depends on them. Browsers allow the camera only on HTTPS or localhost.

## 3. Project layout

```text
attendance/
  app/  main.py config.py db.py models.py security.py recognition.py rules.py
        routers/  auth.py staff.py signin.py attendance.py review.py settings.py
  static/  signin.html  admin/ (login, staff, enrol, attendance, review, settings)  js/  css/
  models/       (downloaded .onnx files, not committed)
  scripts/      download_models.py create_admin.py evaluate.py cleanup.py
  tests/  eval_data/  README.md  requirements.txt
```

## 4. Prompts: foundation (steps 0 to 4)

### Prompt 0: project rules (every session)

```text
You are helping me build a face-recognition attendance web app for offices (university AI course project). Stack: Python 3.11+, FastAPI, SQLAlchemy + SQLite, OpenCV (YuNet detector and SFace embeddings, both pre-trained ONNX), plain HTML/JS frontend served by FastAPI. No Docker, Celery or external services.
Rules:
1. Do only the step I give you, then stop.
2. Match the existing code style, keep functions small, add pytest tests for new logic.
3. Never claim something works unless you ran it. Paste raw command output (tests, curl, script output). If something was not done or not run, write 'not done'.
4. Store face embeddings, not photos, except temporary review-queue frames that expire.
5. Never log passwords, embeddings or raw images.
6. List any assumption you made at the end.
```

### Step 1: skeleton

```text
Step 1: project skeleton. Create the layout (app/, static/, models/, scripts/, tests/, eval_data/, README.md, requirements.txt, .gitignore ignoring .venv, *.db, models/*.onnx, eval_data/, var/). FastAPI app with GET /health returning {status: ok}. config.py reads settings from environment with defaults: DATABASE_URL, SECRET_KEY, MATCH_THRESHOLD, COOLDOWN_SECONDS, MAX_FAILED_ATTEMPTS, REVIEW_RETENTION_DAYS, TIMEZONE. One pytest test for /health.
Verify and paste: pip install -r requirements.txt, pytest -v, start uvicorn app.main:app, curl localhost:8000/health. Stop.
```

### Step 2: recognition spike (the decision gate)

```text
Step 2: recognition spike, standalone, no web app.
1. scripts/download_models.py downloads YuNet (face_detection_yunet_2023mar.onnx) and SFace (face_recognition_sface_2021dec.onnx) from the OpenCV model zoo into models/ and prints file sizes.
2. scripts/spike.py: read the webcam with cv2.VideoCapture(0); detect faces with cv2.FaceDetectorYN; when exactly one face, align and embed it with cv2.FaceRecognizerSF (alignCrop, then feature). Key 'e' stores the current embedding as 'enrolled'; every later frame prints the cosine similarity to it.
3. Print per-frame detect and embed timings.
Paste: the OpenCV version, and one run where I enrol myself then show (a) myself, (b) a different person or a photo, with the similarity values and timings. If this OpenCV build lacks FaceDetectorYN or FaceRecognizerSF, say so and propose the smallest fallback (face_recognition or DeepFace) without installing it. Stop.
```

Decision gate: keep this stack only if your own face scores clearly higher than other people or photos, and each frame takes about a second or less on your CPU. OpenCV's documentation gives a cosine threshold near 0.363 as a starting point; yours is set from data in step 14.

### Step 3: database

```text
Step 3: database models (SQLAlchemy, SQLite, tables created at startup). Tables:
- Staff: id, staff_code (unique), name, department, shift, active, anonymised, created_at
- Consent: id, staff_id, text_version, given_at, recorded_by_admin_id
- FaceEmbedding: id, staff_id, vector (float32 bytes), model_name, model_version, created_at
- Attendance: id, staff_id, type (in/out), timestamp, method (face/manual/review), distance, model_version, edited_by, edit_reason
- RecognitionAttempt: id, timestamp, kiosk_id, outcome (matched/rejected/ambiguous/no_face/spoof), best_staff_id, score, frame_path (nullable, temporary), resolved
- Admin: id, username (unique), password_hash, failed_logins, locked_until, created_at
- AuditLog: id, admin_id, action, target, detail, timestamp
- Setting: key, value
Implement delete_staff_data(staff_id): remove embeddings and consent, blank personal fields, set anonymised=true, keep attendance rows. Tests: create and read each model, embedding round-trip to numpy, delete_staff_data behaviour. Paste pytest -v output. Stop.
```

### Step 4: recognition service

```text
Step 4: app/recognition.py and app/rules.py.
- Recognizer class that loads YuNet and SFace once. analyse_frame(image_bytes) returns status (ok, no_face, multiple_faces, too_small, blurry), face box and landmarks. Quality checks: minimum face width in pixels and a Laplacian-variance blur check, both configurable.
- embed(frame, face) returns a normalised numpy vector.
- rules.py (pure functions, no model): match(embedding, gallery) returns best staff id, best score, second-best score; decide(best, second, threshold, margin) returns matched, rejected or ambiguous. Ambiguous when the best two candidates are within the margin.
- A gallery cache loaded from the database, with refresh() called after enrol or delete.
Tests: rules with synthetic vectors (empty gallery, exact boundary of threshold, ambiguity margin, single person); analyse_frame on 3 fixture images I will add to tests/fixtures (one face, no face, two faces). Paste pytest -v output and the timing of one analyse+embed call. Stop.
```

## 5. Prompts: admin and sign-in (steps 5 to 9)

### Step 5: admin authentication

```text
Step 5: admin auth. scripts/create_admin.py (prompts for username and password, never prints the password). POST /api/admin/login and /logout using a server-side session cookie (HttpOnly, SameSite=Lax), session expiry, argon2 or bcrypt hashing, lockout after 5 failed logins for 5 minutes. A require_admin dependency and a CSRF token header check on all state-changing admin routes. Write each login, failed login and logout to AuditLog.
Tests: correct login, wrong password, lockout then unlock after expiry, route without a session returns 401, missing CSRF returns 403. Paste pytest -v output. Stop.
```

### Step 6: staff API and enrolment

```text
Step 6: staff API (all require_admin). CRUD for staff; deactivate; permanent delete via delete_staff_data (audited). POST /api/admin/staff/{id}/enrol: multipart with consent=true and 3 to 5 images. Block without consent. Run analyse_frame on each image and reject bad ones with a reason per image; store embeddings only for accepted images (minimum 3 accepted); record a Consent row (text version, time, admin); refresh the gallery. Re-enrolling replaces the old embeddings. Never save the uploaded images.
Tests: enrol with good images (mocked recognizer is fine), without consent, with too few accepted images, re-enrol replaces, delete removes embeddings. Paste pytest -v output and one curl of the enrol call. Stop.
```

### Step 7: admin UI part 1 (login, staff, enrolment)

```text
Step 7: admin interface, plain HTML/JS in static/admin. Pages: login; staff list (search, active status, enrolment status, add, edit, deactivate, delete with confirmation); enrolment page with the webcam, a visible consent checkbox and wording, capture of 4 frames with a countdown, per-image accept or reject feedback, and a retake option. Handle the CSRF token and session expiry (redirect to login). Text labels, not colour alone; keyboard-usable forms.
No backend changes unless a field is missing. Give me the manual steps to check each page, then stop.
```

### Step 8: sign-in API

```text
Step 8: POST /api/signin (image, kiosk_id header). No login required.
- analyse_frame problems return a short message code (no_face, multiple_faces, too_small, blurry).
- Matched: first successful scan of the local day (TIMEZONE) is 'in', the next is 'out'. A second match for the same person inside COOLDOWN_SECONDS returns 'already_recorded' and creates no row.
- Rejected or ambiguous: log a RecognitionAttempt; after 3 rejections from the same kiosk within 2 minutes, save the frame to var/review/ and mark it for review.
- Every attempt is logged with outcome and score; frames are kept only for rejected or ambiguous attempts.
- Response returns only: status, name (matched only), action, time. Never scores, never other staff names.
- If the recognizer is unavailable return 503 with a clear message.
Tests with a mocked recognizer: in then out, cooldown, ambiguous, unknown person, three rejections create a review item, inactive staff is never matched, response contains no score. Paste pytest -v output. Stop.
```

### Step 9: kiosk sign-in page

```text
Step 9: static/signin.html and its JS. Full-screen kiosk page: live camera preview, a framing guide, one line of instruction. Capture a frame about every 1.5 seconds while waiting, send it to /api/signin, never send overlapping requests. Show the result large (name, 'Signed in' or 'Signed out', time) for 4 seconds, then reset. Show clear messages for no face, multiple faces, not recognised, already recorded, camera blocked and service unavailable. A 'Sign in manually' link shows the reception contact text. No scores or other names shown.
Give me manual test steps (enrolled person, stranger, two faces, covered camera, backend stopped), then stop.
```

Check yourself: enrol yourself and a friend, sign in and out on the kiosk page, then try a stranger. Commit. The core demo works from here.

## 6. Prompts: operations, quality and finish (steps 10 to 15)

### Step 10: attendance API and exports

```text
Step 10: attendance API (require_admin). GET /api/admin/attendance with filters (date range, staff, department). POST a manual record and PATCH a correction, both requiring a reason and writing an AuditLog entry and edited_by. GET /export.csv for a date range. GET /report/monthly.csv per person with days present, late arrivals and hours worked (sum of in/out pairs; an unpaired 'in' is flagged, not counted). Late means the first 'in' of the day is after WORK_START plus GRACE_MINUTES (settings).
Tests: filters, manual record and correction need a reason, hours from pairs, unpaired in, late flag at the exact boundary, CSV columns. Paste pytest -v output and a sample of each CSV. Stop.
```

### Step 11: review queue, settings, audit, retention (API)

```text
Step 11: (require_admin) GET review queue (unresolved attempts with frame, best candidate and score); POST resolve: assign to a staff member (creates an Attendance row with method 'review', in or out by the same daily rule) or dismiss; deleting the frame on resolve. GET and PUT settings (match threshold, ambiguity margin, cooldown, work start, grace minutes, retention days) with range validation, each change audited; the gallery and rules read the live values. GET audit log with paging. scripts/cleanup.py deletes review frames older than the retention period, also called at startup.
Tests: resolve both ways, validation errors, a settings change takes effect without restart, cleanup deletes only expired frames, audit entries exist. Paste pytest -v output. Stop.
```

### Step 12: admin UI part 2

```text
Step 12: admin pages for attendance (filters, table, manual add and edit with a required reason, CSV buttons), review queue (frame, candidate, assign or dismiss), settings (each value with a one-line explanation of the effect on false accepts and false rejects), audit log, and a dashboard (present today, absent today, late arrivals, recognition success rate). Plain HTML/JS, CSRF handled, text labels, keyboard-usable.
No backend changes unless a field is missing. Give me manual steps for each page, then stop.
```

### Step 13: basic spoof check (optional)

```text
Step 13 (optional): a head-turn challenge. The kiosk asks 'turn your head left' or 'right' at random; the page sends two frames. The backend uses the YuNet landmarks (nose position relative to the eye midpoint) to confirm the head moved the requested way, and rejects otherwise with outcome 'spoof'. Add a setting to turn the challenge on or off. State in the README that this stops a still photo but is not certified anti-spoofing and may not stop a video.
Tests on the landmark logic with synthetic landmark values. Paste pytest -v output. Stop.
```

If time is short, skip the code: run the spoof test by hand (photo on a phone screen and on paper) and report the result and the limitation honestly.

### Step 14: evaluation tooling

```text
Step 14: scripts/evaluate.py using the SAME Recognizer code path as the app. Input layout: eval_data/<person>/enrol/*.jpg and eval_data/<person>/<condition>/*.jpg (conditions such as normal, dim, backlit, glasses, angle, other_day), and optionally eval_data/spoof/<person>/*.jpg.
Procedure: enrol every person from their enrol images into an in-memory gallery; score every probe against every person. Genuine scores are probe against its own person, impostor scores are against everyone else. For thresholds across the observed score range report FAR, FRR and accuracy; report per condition; report spoof pass rate; list probes that failed quality checks separately. Write to eval_results/<timestamp>/: results.csv, sweep.csv, metrics.json, a score histogram PNG (genuine vs impostor) and an FAR/FRR curve PNG, plus the lowest threshold at which FAR is 0 on this sample and the FRR at that point.
Tests for the FAR/FRR arithmetic with synthetic scores, including boundaries. Do not invent images or results. Paste pytest -v output and the script's usage text. Stop.
```

### Step 15: hardening, demo and README

```text
Step 15: finish. README with setup, how to download models, create an admin, run, kiosk full-screen instructions, settings, how to run tests and the evaluation, and a Known Limitations section (small test sample, lighting sensitivity, no certified anti-spoofing, single site). Add a demo seed script that creates staff only from names I pass in (no fake faces). Check that no secrets or embeddings are logged, SECRET_KEY has no insecure default outside development, and uploads respect a size limit. Run the full test suite and a manual smoke test (login, enrol, sign in, sign out, correct a record, export, resolve a review item, delete a staff member). Paste the outputs and list anything not done. Stop.
```

## 7. Debug prompt

```text
Step N is failing. Do not change code yet. 1) Paste the exact command and the full error. 2) State what you expected and what happened. 3) List the two or three most likely causes with the evidence for each from the code or logs. 4) Propose the smallest fix for the most likely cause and wait for my approval. 5) After approval, apply it, rerun the same command and paste the output.
```

## 8. Evaluation data collection

| Item | Plan |
| --- | --- |
| Volunteers | 8 to 10 if possible, 5 at minimum; written consent for each, stating the images are used only for this evaluation and can be deleted |
| Enrolment images | 4 per person in normal light |
| Genuine probes | At least 10 per person across: normal, dim, backlit, glasses on and off, head angle about 20 degrees, a different day |
| Impostor attempts | Produced automatically from other people's probes |
| Spoof attempts | 5 per person: photo on a phone screen, printed photo |
| Storage | `eval_data/` stays out of git and is deleted after the project |

Collect the images while you build steps 8 to 13, so evaluation is not left to the last day.

## 9. Final checklist and report mapping

- Enrol, sign in and sign out work on the kiosk page with a real face.
- A stranger is rejected, and a review item appears after three tries.
- Cooldown prevents duplicate records; late flag and monthly CSV are correct on a small sample.
- Manual corrections need a reason and appear in the audit log.
- Deleting a staff member removes embeddings and consent; review frames expire.
- The full test suite passes; the evaluation has been run on real volunteers.

| Report section | Source |
| --- | --- |
| AI component and model choice | Step 2 results, section 2 |
| Design and architecture | PRD, section 3 layout |
| Evaluation | `eval_results/` (histogram, FAR/FRR curve, per-condition table) |
| Threshold decision | `sweep.csv` and the lowest threshold with FAR 0 |
| Ethics and privacy | Consent flow, deletion, retention, bias discussion across volunteers |
| Limitations | README Known Limitations |
