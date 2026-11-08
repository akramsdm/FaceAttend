# Face Attendance System: Product Requirements Document

Nov 8, 2026 · @AKRAM

A web app for offices and similar institutions, where staff sign in by looking at a camera and administrators manage staff, review attendance and tune recognition. One AI function: face recognition with a pre-trained model, evaluated on real test data.

## 1. Overview

The Face Attendance System records staff attendance through face recognition. It has two interfaces:

- **Staff sign-in interface:** a kiosk-style page where a person looks at the camera and is signed in or out.
- **Admin management interface:** a protected area for enrolling staff, reviewing attendance, handling failed recognitions and exporting records.

The AI component is a pre-trained face detection and embedding model. No model is trained from scratch. The project's AI contribution is choosing the model, setting the decision threshold from measured results, testing failure cases, and addressing privacy and bias.

### Target setting: offices and similar institutions

The system is designed for workplaces with roughly 20 to 500 staff, where an office administrator (HR or the office manager) runs it and staff sign in at a shared device at the entrance. These office conditions add the following requirements:

| ID | Requirement | Priority |
| --- | --- | --- |
| OF-1 | Kiosk mode: the sign-in page runs full screen on a shared tablet or laptop at the entrance, resets itself after each person and recovers without a login. | Must |
| OF-2 | Morning rush: recognition and the on-screen result complete fast enough for a queue (target under 5 seconds per person, ready for the next person immediately). | Must |
| OF-3 | Working hours: the admin sets a start time and a grace period; arrivals after it are flagged as late. | Should |
| OF-4 | Departments and shifts: staff belong to a department and a default shift; reports can be filtered by both. | Should |
| OF-5 | Joiners and leavers: the admin can enrol new staff quickly and deactivate leavers the same day, keeping their history. | Must |
| OF-6 | Visitors and unenrolled people are never recorded; an unrecognised person is directed to reception, and there is no visitor log in this version. | Must |
| OF-7 | HR export: a monthly CSV per person with days present, late arrivals and hours worked, for use in HR or payroll systems. | Should |
| OF-8 | Notice: a visible sign at the entrance states that face recognition is used for attendance, and staff are briefed and consent before enrolment. | Must |
| OF-9 | Fallback: a manual sign-in route at reception for anyone who declines face recognition or is not recognised. | Must |

To confirm: the number of staff, the number of entrances, the working-hours pattern (fixed or shifts), and what device the entrance kiosk will be.

## 2. Problem Statement

Manual attendance (paper registers, roll calls) is slow, easy to falsify through buddy punching, and hard to audit. Card systems fix some of this but cards can be shared or lost. Face recognition can verify that the person present is the person signing in, with no hardware beyond a webcam.

## 3. Goals and Non-Goals

### Goals

- Sign a staff member in or out in under 5 seconds with a single look at the camera.
- Give admins full control: enrol, edit, deactivate and delete staff, and correct attendance records.
- Choose the match threshold from a measured test, reporting false accepts and false rejects.
- Handle failure safely: unrecognised faces never create records silently, and admins can resolve them.
- Treat biometric data responsibly: consent, minimal storage and deletion on request.

### Non-Goals

- Training or fine-tuning a face model.
- Fingerprint scanning. A browser cannot read fingerprints, and device unlock (WebAuthn) has no AI component. It is listed as a possible extension only.
- Payroll, leave management or shift scheduling.
- Native mobile apps or dedicated kiosk hardware.
- Production-grade liveness detection. A basic spoof check is in scope; certified anti-spoofing is not.

## 4. Users

| Role | Needs | Interface |
| --- | --- | --- |
| Staff member | Sign in and out quickly, see that it worked, recover if not recognised | Staff sign-in interface |
| Administrator | Enrol staff, review and correct attendance, resolve failed recognitions, export reports, control data | Admin management interface |
| Course assessor | See the AI design, evaluation and ethics clearly | Report and demo |

## 5. Scope

| Capability | In scope | Notes |
| --- | --- | --- |
| Face enrolment | Yes | 3 to 5 images per person, consent recorded |
| Face sign-in and sign-out | Yes | Core AI function |
| Admin login and staff management | Yes | Password-protected |
| Attendance log, filters and export | Yes | CSV export |
| Review queue for unrecognised attempts | Yes | Admin resolves manually |
| Basic spoof check | Yes | Photo-on-screen test; blink check if time allows |
| Threshold evaluation | Yes | False accept and false reject analysis |
| Fingerprint or device biometrics | No | Extension only |
| Multi-site or multi-camera | No | Single site |

## 6. Staff Sign-In Interface

A single page that opens the webcam and shows clear feedback. It needs no password. The camera is the credential.

| ID | Requirement | Priority |
| --- | --- | --- |
| SI-1 | Show a live camera preview with a framing guide and an instruction ("Look at the camera"). | Must |
| SI-2 | Detect one face, capture a frame and send it for recognition. Reject frames with no face or more than one face, with a clear message. | Must |
| SI-3 | On a confident match, show the person's name, the action (signed in or signed out) and the time, then reset for the next person within 5 seconds. | Must |
| SI-4 | Decide sign-in vs sign-out automatically: first successful scan of the day is sign-in, the next is sign-out. A manual toggle is available if the rule is wrong. | Must |
| SI-5 | On no match or low confidence, show "Not recognised", offer a retry, and after 3 failed attempts log the attempt to the admin review queue. | Must |
| SI-6 | Prevent duplicate records: ignore a second successful scan by the same person within a short cooldown (default 60 seconds). | Must |
| SI-7 | Show a clear error and a manual contact message if the camera is blocked or the service is down. | Must |
| SI-8 | Run basic spoof protection (see section 8). | Should |
| SI-9 | Work on a laptop webcam and a phone browser. | Should |
| SI-10 | Never display confidence scores or other staff names to the person signing in. | Must |

## 7. Admin Management Interface

Access requires an administrator account. All admin actions are recorded in an audit log.

### 7.1 Authentication and access

| ID | Requirement | Priority |
| --- | --- | --- |
| AD-1 | Admin login with username and password (hashed), session timeout and logout. | Must |
| AD-2 | At least one admin role. A second read-only role (viewer) is optional. | Could |
| AD-3 | Lock out repeated failed logins for a short period. | Should |

### 7.2 Staff management

| ID | Requirement | Priority |
| --- | --- | --- |
| AD-4 | Create a staff record: name, staff ID, department (optional), active status. | Must |
| AD-5 | Enrol faces: capture 3 to 5 images through the webcam with quality checks (one face, sufficient size, not blurry). Store embeddings, not the photos, unless the admin explicitly keeps them. | Must |
| AD-6 | Record consent: who consented, when, and the wording shown. Enrolment is blocked without it. | Must |
| AD-7 | Edit staff details, re-enrol faces, deactivate (keeps history) or permanently delete (removes embeddings and personal data). | Must |
| AD-8 | Search and list staff with their enrolment status. | Should |

### 7.3 Attendance records

| ID | Requirement | Priority |
| --- | --- | --- |
| AD-9 | View attendance by date, person and department, with sign-in and sign-out times. | Must |
| AD-10 | Manually add or correct a record; every manual change stores the admin, time and reason. | Must |
| AD-11 | Export to CSV for a chosen date range. | Must |
| AD-12 | Dashboard summary: present today, absent today, late arrivals, recognition success rate. | Should |

### 7.4 Recognition review and settings

| ID | Requirement | Priority |
| --- | --- | --- |
| AD-13 | Review queue of failed or low-confidence attempts, with the captured frame (kept briefly), the closest candidate and a choice to assign to a person or dismiss. | Must |
| AD-14 | Adjustable match threshold and cooldown, with a short explanation of the effect on false accepts and false rejects. | Must |
| AD-15 | Audit log of admin actions and data deletions. | Must |
| AD-16 | Automatic deletion of review-queue frames after a retention period (default 7 days). | Should |

## 8. AI Component: Face Recognition

### 8.1 Pipeline

1. **Detect:** a pre-trained face detector locates the face in the frame.
2. **Quality check:** reject frames with no face, multiple faces, a very small face or heavy blur.
3. **Embed:** a pre-trained model converts the face to a numeric vector (embedding).
4. **Match:** compute the distance from the new embedding to every enrolled embedding; take the closest person.
5. **Decide:** if the distance is below the threshold, accept; otherwise reject and route to retry or review.

The model library (for example `face_recognition` or DeepFace) is chosen in the first milestone and recorded with its version. Matching is a distance calculation, not a trained classifier.

### 8.2 Requirements

| ID | Requirement | Priority |
| --- | --- | --- |
| AI-1 | Use a pre-trained model; record library, model name and version in every attendance record. | Must |
| AI-2 | Match against enrolled embeddings with a configurable distance threshold. | Must |
| AI-3 | Ambiguity rule: if the best and second-best candidates are too close, treat the result as not recognised. | Should |
| AI-4 | Basic spoof check: reject a face shown on a phone screen or printed photo, using a blink or head-movement challenge if time allows; otherwise document the limitation. | Should |
| AI-5 | Log each recognition attempt with outcome, distance and time (no image retained for successful matches). | Must |
| AI-6 | Provide an evaluation script that runs the pipeline over a labelled test set and reports metrics and a threshold sweep. | Must |

## 9. Evaluation Plan

- **Participants:** 8 to 10 volunteers who give written consent, enrolled with 3 to 5 images each.
- **Genuine attempts:** each volunteer signs in several times across conditions: normal light, dim light, backlit, glasses on and off, slight head angle, different day.
- **Impostor attempts:** each volunteer tries to sign in as someone else (or is checked against all other enrolled people) to measure false accepts.
- **Spoof attempts:** photo of an enrolled person on a phone screen and on paper.
- **Metrics:** false accept rate (FAR), false reject rate (FRR) and accuracy at each threshold, a threshold sweep chart, and results broken down by condition.
- **Threshold choice:** pick the value that keeps FAR very low (a wrong person being accepted is the worse error) and report the resulting FRR. Document the choice and the sample size.
- **Limitations to state:** small, non-representative sample; results do not generalise to other populations or cameras.

## 10. Data Model

| Entity | Key fields |
| --- | --- |
| Staff | id, staff\_id, name, department, active, consent\_id, created\_at |
| Consent | id, staff\_id, consent\_text\_version, given\_at, recorded\_by |
| FaceEmbedding | id, staff\_id, vector, model\_name, model\_version, created\_at |
| Attendance | id, staff\_id, type (in/out), timestamp, method (face/manual), distance, model\_version, edited\_by, edit\_reason |
| RecognitionAttempt | id, timestamp, outcome (matched/rejected/ambiguous/spoof), best\_staff\_id, distance, frame\_ref (temporary) |
| Admin | id, username, password\_hash, role, last\_login |
| AuditLog | id, admin\_id, action, target, timestamp |

## 11. System Architecture

1. **Browser frontend:** two pages, the staff sign-in page and the admin area, using the browser camera API.
2. **Backend API:** receives frames, runs the recognition pipeline, applies the decision rules, and serves admin functions.
3. **Recognition module:** detection, quality checks, embedding and matching, loaded once at startup.
4. **Database:** SQLite for the course project (Postgres is a drop-in alternative).
5. **Storage:** embeddings in the database; temporary frames for the review queue with automatic expiry.

Suggested stack: Python with Flask or FastAPI, OpenCV, `face_recognition` or DeepFace, SQLite, plain HTML and JavaScript for the pages. No Docker, queues or workers are needed; recognition runs inside the API process and the model loads once at startup.

## 12. Security, Privacy and Ethics

| Risk | Mitigation |
| --- | --- |
| Biometric data is sensitive | Store embeddings, not photos; encrypt the database at rest where possible; restrict access to admins |
| No consent | Consent is required and recorded before enrolment; staff can withdraw and have data deleted |
| Wrong person accepted (buddy punching, lookalikes) | Strict threshold chosen from evaluation, ambiguity rule, cooldown, review queue |
| Wrong person rejected | Retry, manual admin override, no penalty from a failed scan alone |
| Spoofing with a photo or video | Basic spoof check and documented limits |
| Bias across skin tones, ages, lighting | Test across the volunteer group, report differences honestly, keep a manual alternative for everyone |
| Function creep | Use only for attendance; no other analytics on faces; audit log of admin actions |
| Data retention | Review-queue frames expire automatically; deleted staff lose all embeddings |
| Admin account compromise | Hashed passwords, lockout, session timeout |

A manual attendance route must always be available for anyone who declines face recognition.

## 13. Non-Functional Requirements

- **Speed:** recognition result within 3 seconds of capture on a laptop CPU.
- **Reliability:** if recognition is unavailable, the sign-in page says so and admins can record attendance manually.
- **Usability:** the sign-in flow needs no instructions beyond one on-screen line; admin tasks take few steps.
- **Accessibility:** readable text, clear colours with text labels, keyboard-usable admin forms.
- **Reproducibility:** fixed test set and evaluation script so results can be re-run.
- **Cost:** open-source libraries only.

## 14. Success Metrics

| Metric | Target |
| --- | --- |
| End-to-end sign-in time | Under 5 seconds |
| False accept rate at the chosen threshold | Report the measured value; aim for 0 in the volunteer test |
| False reject rate at the chosen threshold | Report the measured value; aim for under 10 percent in normal light |
| Spoof test (photo on screen) | Report pass or fail honestly |
| Admin tasks (enrol, correct a record, export) | All completable without editing code |
| Evaluation | Threshold sweep and per-condition results in the report |

Targets are provisional until the first evaluation run.

## 15. Milestones

| Day | Goal |
| --- | --- |
| 1 | Pick the model library; detect, embed and match one face from the webcam on your laptop; confirm it works in your lighting |
| 2 | Staff sign-in page, attendance log and cooldown rules; admin login and staff enrolment with consent |
| 3 | Admin attendance view, manual correction, export, review queue; collect volunteer test data |
| 4 | Run the evaluation and threshold sweep; basic spoof test; write-up, screenshots, demo rehearsal |

If time runs short, cut in this order: dashboard summary, viewer role, blink check, review-queue image preview.

## 16. Risks

| Risk | Likelihood | Response |
| --- | --- | --- |
| Recognition performs poorly on your webcam or lighting | Medium | Test on day 1; add a lighting guide and quality checks |
| Too few volunteers | Medium | Use 5 as a minimum and state the limitation |
| Browser camera permissions fail on some devices | Medium | Use HTTPS or localhost; show a clear fallback message |
| Time overrun | High | Follow the cut order above; the core is sign-in, admin enrolment and the evaluation |
| Course requires training a model | Low | Check the brief; if needed, train a small classifier on top of the embeddings |

## 17. Open Questions

- Does the assignment require training or fine-tuning a model, or is using a pre-trained model acceptable?
- How many days remain before the deadline, and how many volunteers can you test with?
- Is a second "biometric" method required? If so, WebAuthn device unlock can be added as a non-AI second factor.
- Which library will you use for embeddings (`face_recognition` or DeepFace), and does it install cleanly on your machine?
- Should sign-out be automatic (second scan of the day) or a separate button?
