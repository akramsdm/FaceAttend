# Face Recognition Implementation Report

**Project:** Face Attendance System  
**Repository:** `D:\work\faceattendance`  
**Audit date:** 2026-11-10  
**HEAD at audit:** `7b63694` (`Step 3C: README metrics/dashboard/person/reports/follow-up, eval+not-done`)  
**Working tree:** dirty/uncommitted (Appendix A, command C2). This report is the only file created by the audit.

---

## 0. Scope, method and claim labels

**Scope.** This is a read-only, evidence-based documentation report for an AI course unit. It focuses on the face-recognition component: the pre-trained models, the recognition pipeline, thresholds, enrolment safeguards, liveness, failure handling, logging, performance, evaluation, tests and responsible-AI issues. It also documents the supporting attendance system sufficiently to explain how the AI component is used.

**Method.** The repository was inspected read-only. No application code, database, settings or existing files were changed. The only file written is this report. Commands that produced measured evidence are reproduced in Appendix A. Personal data, password hashes, embeddings, face images and secrets are not printed; database evidence is aggregate only.

**Claim labels.** Every statement below is labelled with one of the three required categories:

| Label | Meaning in this report |
|---|---|
| **[T] implemented and tested** | Present in the repository and covered by at least one passing automated test, or directly verified by a read-only runtime/repository command performed during this audit. |
| **[I] implemented without tests** | Present in the repository, but no passing automated test covers that specific behaviour, or the only relevant test is skipped. |
| **[N] not built** | Absent from the repository, explicitly out of scope, or not found during the audit. |

**Important evaluation rule.** Development-time image checks and operational logs are **not** a recognition evaluation. A real evaluation requires labelled genuine and impostor attempts collected from consenting volunteers. No such dataset exists in this repository, so this report does **not** estimate FAR, FRR or accuracy and does **not** present the empty evaluation runs as performance evidence.

---

## 1. Executive summary

- **[T]** The AI component is a pre-trained OpenCV **YuNet** face detector plus a pre-trained OpenCV **SFace** face embedder, loaded from two ONNX files in `models/` (`app/recognition.py`, `Recognizer.__init__`). No model is trained or fine-tuned anywhere in the repository.
- **[T]** Recognition is **1-to-N identification**: one probe embedding is compared with every enrolled person's stored embeddings, each person is reduced to their best cosine score, and the best person is accepted, rejected or marked ambiguous (`app/rules.py`, `match` and `decide`; `app/recognition.py`, `GalleryCache`).
- **[T]** The decision threshold is a hand-set, admin-editable cosine-similarity value, default **0.363**, with an ambiguity margin default **0.05** (`app/config.py`; `app/settings_store.py`). The repository does not show that 0.363 was derived from a measured evaluation; it is the OpenCV starting point named in the course guide.
- **[T]** Enrolment has real safeguards: explicit consent, 3–5 images, per-image quality rejection, a mixed-person check and a cross-staff duplicate-face check. On any failure nothing is deleted or stored (`app/routers/staff.py`, `enrol_staff`; `app/enrolment_checks.py`).
- **[T]** There is an optional head-turn liveness challenge, default **off**, using five YuNet landmarks and a normalised nose-offset change of at least **0.08** (`app/spoof.py`; `spoof_challenge` in `app/settings_store.py`). It is a still-photo aid, not certified anti-spoofing.
- **[T]** Every scan attempt is logged in `recognition_attempts` with timestamp, kiosk, outcome, best candidate, score and optional review-frame path (`app/signin.py`, `record_attempt`; `app/models.py`, `RecognitionAttempt`).
- **[T]** Rejected/ambiguous attempts trigger a human review queue after **3** such attempts in **2 minutes**; an admin can assign the attempt to a person or dismiss it, and the frame is deleted on resolution (`app/signin.py`; `app/routers/review.py`).
- **[T]** The full automated suite passes against a copy of the database: **264 passed, 6 skipped, 2 warnings in 82.69 s**. The AI-focused subset is **78 passed, 6 skipped** out of 84 collected.
- **[N]** No real recognition evaluation has been performed: `eval_data/` contains only `.gitkeep`, and all five `eval_results/` runs have zero enrolled people, zero genuine scores and zero impostor scores.
- **[N]** No bias/fairness testing, demographic analysis, model licence documentation or privacy impact assessment was found.
- **[I]** A single-image microbenchmark was measured during this audit: model load **172.9 ms**, detection/quality analysis **23.7–29.1 ms**, embedding **20.1–49.7 ms** on the audit CPU. This is not a throughput or accuracy benchmark.

**Bottom line for the assessor.** The engineering around the AI is stronger than the AI evidence: the system has a clear pre-trained pipeline, configurable thresholds, enrolment checks, attempt logging, review and deletion controls, and a passing test suite. The missing piece is the empirical one. Without a consented, labelled probe set, the report cannot claim a measured FAR/FRR, a justified threshold, or fairness across users.

---

## 2. Models: files, sources, shapes and licences

| Item | Evidence | Label |
|---|---|---|
| Detector file | `models/face_detection_yunet_2023mar.onnx`, **232,589 bytes**, SHA-256 `8F2383E4DD3CFBB4553EA8718107FC0423210DC964F9F4280604804ED2552FA4` | **[T]** |
| Embedder file | `models/face_recognition_sface_2021dec.onnx`, **38,696,353 bytes** (36.9 MiB), SHA-256 `0BA9FBFA01B5270C96627C4EF784DA859931E02F04419C829E83484087C34E79` | **[T]** |
| Source | `scripts/download_models.py` downloads both from `https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/...` (OpenCV Model Zoo) | **[T]** |
| Model names in code | `MODEL_NAME = "SFace"`, `MODEL_VERSION = "2021dec"` (`app/recognition.py`) | **[T]** |
| Embedding shape | Measured `(128,)`, `float32`, L2 norm `1.000000` on `var/lena.jpg` | **[T]** |
| Detector output used | Bounding box `(x, y, w, h)` plus 5 landmarks (10 floats), reshaped to `(5, 2)` (`Recognizer.analyse_frame`) | **[T]** |
| Detector input | Created with `(320, 320)`; `setInputSize((width, height))` is called for each decoded frame (`Recognizer.__init__`, `analyse_frame`) | **[T]** |
| SFace calls | `cv2.FaceRecognizerSF.create(...)`, `alignCrop(frame, face)`, `feature(aligned)` (`app/recognition.py`) | **[T]** |
| YuNet calls | `cv2.FaceDetectorYN.create(path, "", (320, 320), 0.8, 0.3, 5000)`, `setInputSize`, `detect` | **[T]** |
| Licences | No `LICENSE` file at repository root; `README.md` says "Internal / not specified. Add a `LICENSE` file before external distribution." Model-specific licences/citations are not documented in the repository. | **[N]** |
| Model training | No `.fit(`, `train(`, fine-tune, optimizer, `backward(` or epoch code found in first-party Python files | **[N]** |

**Interpretation.** The model files are third-party pre-trained assets. The repository documents where to download them but not their licence terms, citation, training data, intended operating conditions or known demographic limitations. Before external distribution or publication, the upstream OpenCV Model Zoo model cards and licences should be checked and cited; this audit did not verify them. **[N]**

The measured embedding is a 128-dimensional float vector. Cosine similarity is therefore a bounded dot product of two unit vectors, not a probability. A score of 0.363 is a similarity threshold, not "36.3% confidence". **[T]** (arithmetic in `app/rules.py`; measured norm in Appendix A, command C13).

---

## 3. Recognition pipeline

The live kiosk path is `POST /api/scan` in `app/routers/signin.py`; the legacy `POST /api/signin` uses the same `_recognise` helper. The sequence is:

1. **Read and size-check the upload.** `image.file.read()`; if larger than `max_upload_bytes` (default 5,242,880 bytes) the API returns `invalid_image` without decoding (`app/routers/signin.py`, `scan_face`; `app/config.py`). **[T]**
2. **Decode.** `cv2.imdecode(np.frombuffer(...), cv2.IMREAD_COLOR)`; a `None` result raises `ValueError`, which `_recognise` logs as `rejected` and returns `invalid_image` (`Recognizer._decode`; `_recognise`). **[T]**
3. **Detect.** YuNet `detect` on the full frame. Zero faces → `no_face`; more than one → `multiple_faces` (`Recognizer.analyse_frame`). **[T]**
4. **Quality gate.** One face is required; face width below `min_face_width` (default 120 px) → `too_small`; Laplacian variance of the grey crop below `blur_threshold` (default 100.0) → `blurry` (`Recognizer.analyse_frame`, `_laplacian_variance`). **[T]**
5. **Embed.** `alignCrop` then `feature`; the result is flattened and L2-normalised; an all-zero vector raises `ValueError` (`Recognizer.embed`). **[T]**
6. **Match.** `GalleryCache.match_embedding` takes a locked snapshot of the in-memory gallery and calls `rules.match`. The gallery contains only `Staff.active == True` and `Staff.anonymised == False` (`GalleryCache.refresh`). **[T]**
7. **Decide.** `rules.decide(best, second, threshold, margin)` applies the threshold first, then the ambiguity margin (`app/rules.py`). **[T]**
8. **Re-check eligibility.** `_eligible_staff` blocks inactive/anonymised matches even if a stale gallery entry survived; a match that fails this becomes `rejected` (`app/routers/signin.py`, `_recognise`). **[T]**
9. **Optional liveness.** If `spoof_challenge` is on and no challenge frame was supplied, the API returns `challenge_required` with a random `left`/`right`. A supplied challenge is analysed and checked with `confirm_head_turn`; failure logs `spoof` (`_recognise`; `app/spoof.py`). **[T]**
10. **Log the attempt.** `record_attempt` writes `RecognitionAttempt`; `matched` is logged after the decision, and `no_face`/`rejected`/`ambiguous`/`spoof` are logged on their branches (`app/signin.py`, `record_attempt`). **[T]**
11. **Issue a clock token, not attendance.** A successful scan calls `state_for_staff`, then `issue_clock_token` (20 s, single-use, bound to staff and kiosk). Attendance is written only by the later `/api/clock` call (`app/routers/signin.py`, `scan_face`; `app/clock_tokens.py`). **[T]**

**Design consequence.** The scan is an identification step, not an attendance write. This separation is good for auditability: a face match cannot silently create a clock event, and the token binds the confirmed person to the kiosk for a short window. **[T]** (`app/attendance_service.py`; `README.md`, "Clock flow (kiosk)").

---

## 4. Decision rule and parameter table

`rules.match` returns the best staff id, best score and second-best **staff** score. Multiple embeddings for one person are reduced with `max` before ranking, so a person with five enrolment images cannot dominate by count. `rules.decide` is:

```python
if best is None or best < threshold:
    return "rejected"
if second is not None and best - second < margin:
    return "ambiguous"
return "matched"
```

This means a score exactly equal to the threshold is accepted, and ambiguity is only considered after the threshold passes. **[T]** (`app/rules.py`; boundary tests in `tests/test_rules.py`).

| Parameter | Default | Live value in audited DB | Where set / used | Label |
|---|---:|---:|---|---|
| `match_threshold` | 0.363 | 0.363 | `app/config.py`; live override `app/settings_store.py`; `rules.decide` | **[T]** |
| `ambiguity_margin` | 0.05 | 0.05 | same | **[T]** |
| `duplicate_threshold` | `match_threshold` | not stored (falls back to 0.363) | `app/settings_store.py`; `enrol_staff` | **[T]** |
| `min_face_width` | 120 px | env only | `app/config.py`; `Recognizer.analyse_frame` | **[T]** |
| `blur_threshold` | 100.0 | env only | `app/config.py`; `Recognizer.analyse_frame` | **[T]** |
| YuNet detection score | 0.8 | code constant | `Recognizer.__init__` | **[T]** |
| YuNet NMS threshold | 0.3 | code constant | `Recognizer.__init__` | **[T]** |
| YuNet top-k | 5000 | code constant | `Recognizer.__init__` | **[T]** |
| `cooldown_seconds` | 60 | 60 | `app/config.py`; legacy `/api/signin` only | **[T]** |
| `review_retention_days` | 7 | 7 | `app/config.py`; `app/cleanup.py` | **[T]** |
| `spoof_challenge` | False | False | `app/settings_store.py` | **[T]** |
| `work_start` | 09:00 | 09:00 | `app/config.py`; shift rules | **[T]** |
| `grace_minutes` | 15 | 15 | `app/config.py`; shift rules | **[T]** |
| `max_upload_bytes` | 5,242,880 | env only | `app/config.py`; staff/signin routers | **[T]** |
| Clock token TTL | 20 s | code constant | `app/clock_tokens.py` | **[T]** |
| Undo window | 10 s | front-end + API | `static/signin.js`; `app/routers/clock.py` | **[T]** |
| Review trigger | 3 rejected/ambiguous in 2 min | code constants | `app/signin.py` | **[T]** |

**Threshold interpretation.** The default 0.363 is a cosine-similarity operating point taken from the course guide/OpenCV guidance, not a value chosen from this project's data. The README and course guide both say the threshold should be set from measured results; that has not happened. **[T]** for the code/default; **[N]** for a measured justification.

**Live-setting behaviour.** `get_value` overlays `Setting` rows on environment defaults; `set_values` validates type and range and records changes. The audited DB has seven setting rows, all equal to the defaults above. **[T]** (`app/settings_store.py`; DB aggregate, Appendix A C16).

---

## 5. Enrolment safeguards

Enrolment is `POST /api/admin/staff/{staff_id}/enrol` in `app/routers/staff.py`. It requires an admin session plus CSRF header, a `consent` form field that must be true, and 3–5 images (`MIN_IMAGES = 3`, `MAX_IMAGES = 5`, `CONSENT_TEXT_VERSION = "v1"`). **[T]**

**Per-image checks.** For each upload: size cap, decode, exactly one face, minimum width, blur, then SFace embedding. Rejected images are returned with a status and reason; accepted images are collected (`enrol_staff`; `REJECTION_REASONS`). **[T]**

**Batch checks before any write.** At least 3 acceptable embeddings are required. Then:

- **Mixed-person check.** `check_same_person` compares every embedding to the first with cosine similarity using the live `match_threshold`; any pair below it blocks the whole batch with `enrol_blocked_mixed_people` (`app/enrolment_checks.py`; `enrol_staff`). **[T]**
- **Cross-staff duplicate check.** `find_duplicate_face` queries `FaceEmbedding` joined to `Staff` directly (not the gallery cache), excludes the target staff, includes inactive staff, excludes anonymised staff, and blocks if the best cosine score is `>= duplicate_threshold` (`app/enrolment_checks.py`; `enrol_staff`). **[T]**

**Failure atomicity.** The code comment is explicit: checks run before deleting existing embeddings; on failure nothing is deleted or stored, no `Consent` row is added, no commit occurs, and the gallery is unchanged. Audit entries record ids only, never vectors or scores. **[T]** (`enrol_staff`; tests in `tests/test_staff.py`, `tests/test_enrolment_checks.py`).

**Success path.** Existing embeddings for that staff are deleted, the new embeddings are stored with `model_name`/`model_version`, a `Consent` row is added with `text_version="v1"` and the recording admin id, an audit entry is written, the DB commits, and `gallery.refresh()` reloads active non-anonymised staff. Re-enrolment of the same person is allowed and replaces that person's embeddings. **[T]**

**Consent wording.** The admin page says the staff member has been shown the notice, briefed on use, and consents; it states that only mathematical embeddings are stored, captured images are not saved, and embeddings/consent can be deleted on request (`static/admin/enrol.html`, consent section). The repository contains the checkbox and this wording, but no separate signed consent form, privacy notice, or withdrawal workflow beyond staff deletion. **[I]**

**Deletion.** `delete_staff_data` deletes `FaceEmbedding` and `Consent` rows, replaces the staff code with `deleted-{id}`, blanks name/department/shift, sets `active=False` and `anonymised=True`, and keeps attendance history. The delete endpoint then audits and refreshes the gallery (`app/models.py`; `app/routers/staff.py`, `delete_staff`). **[T]**

**Gap.** The system does not store the original enrolment images, which is good data minimisation. It does store biometric templates (float32 vectors) in plaintext SQLite, with no application-level encryption or key management. **[N]**

## 6. Spoofing and liveness

- **[T]** The only liveness mechanism is an optional head-turn challenge (`spoof_challenge`, default off). The kiosk asks the user to turn left or right; the backend compares the nose x-position relative to the eye midpoint, normalised by inter-eye distance, between the first and challenge frames.
- **[T]** Acceptance requires a change of at least `MIN_TURN_DELTA = 0.08` in the requested direction (`app/spoof.py`, `confirm_head_turn`). The direction is chosen with `secrets.token_bytes(1)`, not `random`.
- **[T]** A failed or missing challenge logs outcome `spoof`, returns `status="spoof"`, and does not issue a token (`app/routers/signin.py`, `_recognise`).
- **[I]** The unit tests cover the pure `nose_offset`/`confirm_head_turn` arithmetic, and scan tests cover challenge branches with fakes. No test uses a real photo, a real replay video, a screen replay, a mask, or a deepfake.
- **[N]** There is no certified presentation-attack detection, no depth sensor, no texture/reflectance analysis, no video-replay detection, no injection-attack detection, and no anti-spoofing evaluation.
- **[T]** `README.md` states: "Not certified anti-spoofing. Optional head-turn challenge helps against still photos but may not stop video replay; a liveness aid, not a security guarantee."

**Assessment.** The challenge raises the effort for a static printed photo, but a short video of the enrolled person turning their head, or a screen replay, could pass. Because the setting is off by default, the deployed kiosk in the audited DB had no liveness check at all. This is a material residual risk for buddy punching and presentation attacks. **[T]** for default/state; **[N]** for effective anti-spoofing.

---

## 7. Failure handling and human oversight

| Situation | System behaviour | Evidence | Label |
|---|---|---|---|
| No face | `no_face`; attempt logged without score; UI asks for a clear frame | `Recognizer.analyse_frame`; `_recognise`; `static/signin.js` | **[T]** |
| More than one face | `multiple_faces`; attempt logged as `rejected`; no match | same | **[T]** |
| Face too small / blurry | `too_small` / `blurry`; attempt logged as `rejected`; retry guidance | same | **[T]** |
| Image undecodable | `invalid_image`; `rejected` attempt | `_recognise` | **[T]** |
| Upload too large | `invalid_image`; no decode | `scan_face`, `enrol_staff` | **[T]** |
| Score below threshold | `rejected`; best candidate and score logged if available | `rules.decide`; `_recognise` | **[T]** |
| Top two within margin | `ambiguous`; logged; review frame may be saved | `rules.decide`; `record_attempt` | **[T]** |
| Spoof challenge failed | `spoof`; logged; no token | `_recognise` | **[T]** |
| Inactive/anonymised match | Forced to `rejected` even if gallery stale | `_eligible_staff` | **[T]** |
| Repeated failures | After 3 rejected/ambiguous in 2 min, save one JPEG for review | `app/signin.py`, `REVIEW_THRESHOLD`, `REVIEW_WINDOW` | **[T]** |
| Admin review | Assign to staff (creates attendance via lifecycle service) or dismiss; frame deleted; audit | `app/routers/review.py`, `resolve_attempt` | **[T]** |
| Missing clock-out | Manager closes/corrects with required reason; audited | `app/routers/approvals.py`; `app/approvals.py` | **[T]** |
| Manual correction | Admin adds/edits attendance with required reason; audited | `app/routers/attendance.py` | **[T]** |

**Human oversight is real but bounded.** The review queue is the main AI-specific oversight: an admin sees the failed frame, the best candidate and the score, then either assigns it or dismisses it. Assignment does not trust the model's candidate; the admin chooses the staff member, and the backend derives in/out from the lifecycle state at the attempt timestamp (`app/routers/review.py`, `resolve_attempt`). **[T]**

**Limits of oversight.** The queue only receives frames after the 3-in-2-minutes trigger, so isolated failures are logged without an image. The queue is admin-only, has no SLA, and depends on someone checking it. The system does not notify anyone. **[T]** for trigger/role; **[N]** for notifications/SLA.

---

## 8. What AI behaviour is logged

`RecognitionAttempt` stores: `id`, `timestamp`, `kiosk_id`, `outcome` (`matched|rejected|ambiguous|no_face|spoof`), `best_staff_id`, `score`, `frame_path`, `resolved` (`app/models.py`). This is enough to reconstruct a decision and to review a failed frame, without storing the embedding. **[T]**

**Aggregate evidence from the audited DB** (read-only copy; Appendix A C15–C17):

| Outcome | Count | Score min | Score mean | Score max | Frames saved |
|---|---:|---:|---:|---:|---:|
| `matched` | 15 | 0.407944 | 0.602199 | 0.795999 | 0 (matched attempts are not frame-captured) |
| `rejected` | 25 | 0.228580 | 0.255259 | 0.293241 | 12 total attempts with frames (all outcomes) |
| `no_face` | 45 | — | — | — | 0 |
| `ambiguous` | 0 | — | — | — | 0 |
| `spoof` | 0 | — | — | — | 0 |

All 85 attempts came from kiosk `entrance-main`, between `2026-11-09 16:11:27` and `2026-11-10 06:19:33`; **0** attempts were resolved. The 12 saved frames are all present on disk. There are **415** JPEGs in `var/review`, of which **403** are not referenced by the audited DB; none is older than 7 days, so the retention cleanup would not yet remove them. This is housekeeping noise from development/testing, not evidence of a leak, but it shows that unreferenced frames can accumulate within the retention window. **[T]**

**Traceability gaps.**

- **[T]** `Attendance.distance` and `Attendance.model_version` columns exist (`app/models.py`) but are **never populated**: in the audited DB, 0 of 5 attendance rows have either value. The recognition score and model version are therefore not attached to the attendance record; they live only on `RecognitionAttempt`.
- **[T]** `RecognitionAttempt` has no `model_name`/`model_version` field, so attempts cannot be segmented by model version after an upgrade.
- **[N]** There is no log of threshold changes tied to individual decisions, no per-decision explanation beyond score/candidate, and no calibration metadata.

---

## 9. Performance

Measured on the audit machine with `var/lena.jpg` (91,814 bytes), using the real `Recognizer` and the same `analyse_frame`/`embed` calls as the app. Raw output is in Appendix A C13.

| Measurement | Result | Label |
|---|---:|---|
| Model load (`Recognizer()`) | 172.9 ms | **[I]** |
| `analyse_frame` runs 0–4 | 27.5, 25.3, 23.7, 25.5, 29.1 ms | **[I]** |
| `embed` runs 0–4 | 49.7, 23.2, 20.3, 20.8, 20.1 ms | **[I]** |
| Detection result | 1 face, box `(207, 182, 145, 206)` | **[T]** |
| Embedding | shape `(128,)`, `float32`, norm `1.000000` | **[T]** |
| Approximate warm analyse+embed | ~44–51 ms per frame | **[I]** |

**Environment:** Intel Core i7-8665U @ 1.90 GHz, 4 cores / 8 logical processors, 8,357,949,440 bytes RAM, Windows 11 Pro 10.0.26300; Python 3.15.2, OpenCV 5.0.0.93, NumPy 2.5.4. **[T]** (Appendix A C12).

**Caveats.** This is one image, one process, CPU only, no concurrent kiosks, no sustained load, no camera capture/upload/JPEG time, and no database or gallery-scale effect. It is a microbenchmark, not a service-level performance test. **[I]** OpenCV printed two warnings that `setPreferableTarget` is not supported by the new graph engine; they did not prevent correct output, but they indicate the installed OpenCV build may not use the requested backend target. **[T]** (raw warnings in C13).

**Scale.** The gallery is an in-memory dict of all active embeddings; matching is a linear scan over people and their vectors (`rules.match`). With the audited data (2 staff, 4 embeddings) this is trivial. No large-gallery benchmark exists, so the claim "fast enough for a single site" is plausible but unmeasured beyond the microbenchmark. **[N]** for scale evidence.

---

## 10. Evaluation: script, arithmetic and results

### 10.1 What the script does

`scripts/evaluate.py` uses the same `Recognizer` path as the app. Input layout:

```text
eval_data/<person>/enrol/*.jpg
eval_data/<person>/<condition>/*.jpg
eval_data/spoof/<person>/*.jpg        (optional)
```

It builds an in-memory gallery from each person's `enrol/` images, scores every probe against its own person (genuine) and every other person (impostor), reduces each person to their best enrol embedding, excludes quality-rejected probes, sweeps 101 thresholds, and writes `results.csv`, `sweep.csv`, `metrics.json`, `score_histogram.png` and `far_frr_curve.png` (`scripts/evaluate.py`, module docstring and `evaluate`/`write_outputs`). **[T]**

### 10.2 FAR/FRR arithmetic

`app/eval_metrics.py` defines, at threshold `t`:

- genuine accept if `score >= t`; genuine reject if `score < t`;
- impostor accept if `score >= t`; impostor reject if `score < t`;
- `FAR = impostor_accept / impostor_count`, `FRR = genuine_reject / genuine_count`;
- `accuracy = (genuine_accept + impostor_reject) / total`;
- if a denominator is zero, the rate is returned as `0.0`.

This arithmetic is unit-tested with synthetic scores, including boundaries (`tests/test_eval_metrics.py`, 11 tests). **[T]** The zero-denominator convention is a reporting hazard: an empty sample produces `FAR=0`, `FRR=0`, not "undefined". **[T]**

### 10.3 Actual runs

`eval_data/` contains only `.gitkeep`. Five runs exist:

| Run directory | Enrolled | Genuine | Impostor | Quality rejected | Reference threshold | FAR=0 point |
|---|---:|---:|---:|---:|---:|---:|
| `20261108T181117Z` | 0 | 0 | 0 | 0 | 0.0 | threshold 0.0 |
| `20261108T181401Z` | 0 | 0 | 0 | 0 | 0.0 | threshold 0.0 |
| `20261109T113446Z` | 0 | 0 | 0 | 0 | 0.0 | threshold 0.0 |
| `20261109T121932Z` | 0 | 0 | 0 | 0 | 0.0 | threshold 0.0 |
| `20261109T135936Z` | 0 | 0 | 0 | 0 | 0.0 | threshold 0.0 |

Every run has an empty `results.csv` header, 101 `sweep.csv` rows all zero, and only `far_frr_curve.png` (no `score_histogram.png`, because there are no scores). The `far_zero_point` at threshold 0.0 is a **zero-sample artifact**: with no impostor scores, FAR is 0 at every threshold, so the lowest grid point qualifies. It is not a real FAR=0 operating point. **[T]** (Appendix A C14).

**Do not read these runs as performance.** They contain no people, no genuine attempts and no impostor attempts. The README itself says: "Real recognition FAR/FRR not measured (no labeled `eval_data/`); eval runs all zeros." **[T]**

**Exit-code nuance.** The script returns `2` if the data directory does not exist. If `eval_data/` exists but is empty (the audited state), it runs, writes zero outputs and returns `0`. So "missing data exits 2" in the README is true only for a missing directory, not an empty one. **[T]** (`scripts/evaluate.py`, `main`; directory listing C14).

### 10.4 What a real evaluation would require

A defensible evaluation would need, at minimum:

- **Consenting volunteers** (the course guide suggests 8–10, minimum 5), with written consent for evaluation-only use and deletion on request. **[N]**
- **Labelled probes**: genuine attempts (each volunteer against their own enrolment) and impostor attempts (each volunteer against every other enrolled person), collected under stated conditions (normal, dim, backlit, glasses, angle, another day). **[N]**
- **Enough impostor pairs** to estimate FAR with a meaningful confidence interval; zero errors in a tiny sample does not prove a low FAR. **[N]**
- **A pre-registered threshold choice** (for example, lowest threshold with zero observed impostor accepts, then report FRR and sample size), not a post-hoc selection. **[N]**
- **Condition and demographic breakdowns**, with sample sizes and honest reporting of gaps. **[N]**
- **A spoof/liveness test** using printed photos, screen replay and video replay, reported separately. **[N]**

None of this is present. Consequently this report makes **no claim** about FAR, FRR, accuracy, equal error rate, threshold justification, or fairness. **[N]**

## 11. AI tests: what is covered and what is not

**AI-focused subset:** 84 tests collected across 11 files; **78 passed, 6 skipped** in 16.03 s (Appendix A C11). The 6 skips are all missing image fixtures in `tests/test_recognition.py`: `one_face.jpg` (4 skips), `no_face.jpg` (1), `two_faces.jpg` (1). No fixture images are committed, so the real-image detection/quality tests did not run. **[T]**

| Test file | Collected | What it covers | Label |
|---|---:|---|---|
| `tests/test_recognition.py` | 9 | Recognizer quality statuses and gallery cache, using fixtures; 6 skipped because images are absent | **[I]** |
| `tests/test_rules.py` | 9 | Cosine match, per-person best reduction, threshold boundary, ambiguity margin, empty gallery | **[T]** |
| `tests/test_enrolment_checks.py` | 13 | Mixed-person batch, duplicate-face detection, manager name redaction, threshold boundary | **[T]** |
| `tests/test_spoof.py` | 6 | Nose offset, left/right turn confirmation, minimum delta | **[T]** |
| `tests/test_eval_metrics.py` | 11 | FAR/FRR/accuracy arithmetic, zero denominators, threshold sweep, FAR=0 point | **[T]** |
| `tests/test_scan.py` | 7 | `/api/scan` outcomes, token issue, challenge, eligibility, attempt logging | **[T]** |
| `tests/test_signin.py` | 9 | Legacy sign-in, cooldown, lifecycle enforcement, review-frame trigger | **[T]** |
| `tests/test_staff.py` | 8 | Staff CRUD, enrolment consent/quality/duplicate paths, deletion | **[T]** |
| `tests/test_review_settings.py` | 7 | Review queue resolve/dismiss, settings validation, retention cleanup | **[T]** |
| `tests/test_models.py` | 4 | Embedding round-trip, anonymisation/deletion | **[T]** |
| `tests/test_frontend_state.py` | 1 | Kiosk lifecycle-to-UI state mapping (Node test via pytest wrapper) | **[T]** |

**Full suite:** 30 test files, **270 collected; 264 passed, 6 skipped, 2 warnings in 82.69 s**, run with `DATABASE_URL` pointed at a copy of `attendance.db` (Appendix A C10). Tests use in-memory SQLite via `tests/conftest.py`; the copy was an extra precaution so the original DB could not be touched. **[T]**

**Not tested.**

- **[N]** Real recognition accuracy/FAR/FRR on any labelled dataset.
- **[N]** Real camera capture, lighting, glasses, angle, distance, motion blur, or kiosk hardware.
- **[N]** Liveness against printed photos, screens, video replay, masks or deepfakes.
- **[N]** Bias/fairness across skin tone, age, gender, disability or facial hair.
- **[N]** Large-gallery performance or concurrent kiosks.
- **[N]** Threshold calibration or score-to-probability calibration.
- **[N]** Recovery from a corrupted model file, missing model, or OpenCV backend failure at runtime (startup fails fast by design).
- **[N]** Manual browser QA of the AI-facing pages (README explicitly lists this as not automated).

**Test quality note.** The pure modules (`rules`, `eval_metrics`, `enrolment_checks`, `spoof`) are well isolated and tested with synthetic vectors/scores, which is appropriate. The HTTP tests use fakes for the recognizer/gallery, so they verify wiring and branch logic, not model behaviour. That distinction matters when presenting: "tested" here means the application logic is tested, not that the face model is accurate. **[T]**

---

## 12. Responsible-AI analysis

### 12.1 Purpose and benefit

The system automates attendance identification for a single site. The plausible benefits are speed, reduced manual entry, and a reviewable record. The AI is assistive: a match only issues a short-lived token, and the person still confirms clock in/out; ambiguous cases go to a human. **[T]** (`app/routers/signin.py`; `app/attendance_service.py`; `README.md`).

### 12.2 Privacy and data minimisation

- **[T]** Enrolment stores only 128-d embeddings, not the captured images; the UI says so.
- **[T]** Embeddings and consent rows are deleted on staff deletion; attendance history is retained with an anonymised staff record.
- **[T]** Review frames are saved only after repeated rejected/ambiguous attempts, and are deleted on resolution or after `review_retention_days` (default 7).
- **[T]** Attempts store a score and best candidate, not the embedding.
- **[N]** Embeddings are stored unencrypted in SQLite; there is no application-level encryption, key rotation, or database-file permission hardening in the repository.
- **[N]** There is no documented retention period for `recognition_attempts` or `audit_logs`; only review frames have a retention setting.
- **[N]** There is no privacy notice document, record of processing, DPIA, or data-subject request workflow beyond deleting a staff member.
- **[I]** Consent is a checkbox recorded by an admin with `text_version="v1"`; there is no separate signed form or withdrawal flow.

**Assessment.** The design follows data minimisation better than many course projects: no enrolment images, no embeddings in logs, deletion of biometrics. The weak points are encryption at rest, retention of attempt/audit metadata, and the absence of formal privacy documentation. These are documentation/compliance gaps, not evidence that the code leaks data. **[T]/[N]**

### 12.3 Fairness and bias

- **[N]** No demographic data is collected for evaluation, and no bias testing exists.
- **[N]** The evaluation set is empty, so no performance difference across skin tone, age, gender, glasses, lighting or angle can be reported.
- **[N]** The model's upstream training data and known failure modes are not documented in the repository.
- **[T]** The system provides a manual alternative: admins can add/correct attendance, and failed matches go to review. This mitigates individual harm but does not measure or fix bias.
- **[T]** The threshold is global; there is no per-user calibration or fallback threshold.

**Assessment.** Fairness is **unmeasured**. A responsible presentation must say that, not imply the model is unbiased because it is pre-trained. The absence of demographic data also means the system cannot audit fairness retrospectively. **[N]**

### 12.4 Transparency and explainability

- **[T]** The decision is explainable at a high level: best cosine score, second-best score, threshold, ambiguity margin.
- **[T]** The admin review screen shows the frame, best candidate and score.
- **[N]** The kiosk user is not shown a score or reason beyond a status message; there is no explanation of why a match failed.
- **[N]** Cosine similarity is not calibrated to a probability, and the UI does not claim it is.
- **[N]** There is no model card, system card, or published threshold rationale.

### 12.5 Human oversight and accountability

- **[T]** A human resolves ambiguous/rejected cases; a human approves re-entry, missing clock-outs and manual corrections; all such actions are audited.
- **[T]** The backend, not the model, decides whether an attendance write is allowed (`app/attendance_service.py`).
- **[T]** Audit log records admin actions (login, enrolment, deletion, review resolution, manual attendance, approvals).
- **[N]** There is no automatic notification or SLA for the review queue.
- **[N]** There is no named accountable owner, model governance process, or incident procedure in the repository.

### 12.6 Security and misuse

- **[T]** Admin passwords are Argon2-hashed (`argon2-cffi`); sessions are opaque tokens in an HttpOnly, SameSite=Lax cookie; mutations require a CSRF header; failed logins lock the account after 5 attempts for 5 minutes; roles and department scope are enforced; uploads are capped at 5 MiB; `SECRET_KEY` must be non-default outside development (`app/security.py`; `app/routers/auth.py`; `app/config.py`).
- **[T]** Clock tokens are HMAC-SHA256 signed, single-use, 20 s, and bound to staff + kiosk (`app/clock_tokens.py`).
- **[N]** The kiosk is identified only by a client-supplied `X-Kiosk-ID` header; there is no kiosk authentication, device certificate, or network binding.
- **[N]** There is no rate limit on `/api/scan`; the 60 s cooldown applies only to legacy `/api/signin`. A token is single-use, but an attacker can request many scans.
- **[N]** No HTTPS enforcement, HSTS, proxy hardening, or at-rest encryption is configured in the repository.
- **[T]** The app does not log embeddings or face images; review frames are files on disk, access-controlled only by the admin frame endpoint.

**Misuse scenarios.** Buddy punching (someone else's face), presentation attacks (photo/screen/video), a stolen admin session, a spoofed kiosk id, and inference from attendance patterns. Mitigations exist for some (liveness optional, review, audit, short token); none is complete. **[T]/[N]**

### 12.7 Risk matrix

| Risk | Likelihood | Impact | Existing mitigation | Residual status | Label |
|---|---|---|---|---|---|
| Wrong person accepted (false accept) | Unknown; unmeasured | High (attendance fraud) | Threshold 0.363, ambiguity margin, review, cooldown (legacy only) | **Unquantified** | **[N]** |
| Genuine user rejected (false reject) | Unknown; unmeasured | Medium (friction, exclusion) | Retry, review queue, manual correction | **Unquantified** | **[N]** |
| Presentation attack / photo | Medium if liveness off | High | Optional head-turn challenge, default off | Weak; no video/replay defence | **[I]/[N]** |
| Bias against a demographic group | Unknown; untested | High (unfair exclusion) | Manual alternative only | **Unmeasured** | **[N]** |
| Biometric data breach | Low–medium | High | No images stored; deletion; admin auth | Templates unencrypted; no at-rest protection | **[I]/[N]** |
| Unreviewed failure | Medium | Medium | 3-in-2-min frame capture, admin queue | No notification/SLA; many failures have no frame | **[I]** |
| Kiosk spoofing / scan flooding | Medium | Medium | Short single-use token | No kiosk auth; no scan rate limit | **[N]** |
| Admin account compromise | Low–medium | High | Argon2, lockout, CSRF, HttpOnly cookie | No MFA; sessions in memory; no password-change UI | **[I]/[N]** |
| Model/version drift | Medium over time | Medium | `model_version` on embeddings | Attendance/attempts do not record model version consistently | **[I]** |

### 12.8 Legal/ethical position

The repository contains privacy-aligned controls (consent checkbox, minimisation, deletion, retention for frames) but **does not establish legal compliance**. There is no DPIA, privacy notice, lawful-basis analysis, processor agreement, or retention schedule for attempts/audit logs. Biometric data is special-category data in many jurisdictions; deploying this outside a course/demo context would require a legal review. **[N]** This report is not legal advice.

---

## 13. Limitations and not-built items

**AI-specific.**

- **[N]** No real FAR/FRR/accuracy evaluation; `eval_data/` is empty and all runs are zero-sample.
- **[N]** No justified threshold; 0.363 is a default/starting point.
- **[N]** No bias/fairness testing or demographic breakdown.
- **[N]** No certified anti-spoofing; liveness is optional and still-photo-only in practice.
- **[N]** No model card, licence citation, upstream training-data documentation, or calibration.
- **[N]** No large-gallery or concurrency benchmark.
- **[N]** No model-version field on `RecognitionAttempt`; `Attendance.distance`/`model_version` are never populated.
- **[I]** Six real-image recognition tests are skipped because fixtures are missing.

**System-level (from `README.md` "Known limitations" and audit).**

- **[T]** Single site, SQLite, in-memory sessions; sessions do not survive restart.
- **[T]** No password-change UI; no Alembic migration framework; no HTTPS/proxy hardening.
- **[T]** One shift per person per day; no split shifts, rosters, leave or swaps.
- **[T]** Token 20 s, undo 10 s; overtime margin 0; missing clock-outs never counted as worked time.
- **[T]** No automated follow-up notifications; insights UI panel hidden though the endpoint exists.
- **[T]** `scripts/cleanup_personal_patterns.py --apply` was not run on the real DB; dry-run 0.
- **[T]** Metrics scoping differs: `/api/admin/metrics/*` is admin/manager + department scope, while `/api/admin/attendance/dashboard|export.csv|report/monthly.csv` remain admin-only.
- **[T]** Working tree is dirty with many uncommitted modifications, untracked backup DBs, `_wtest.py`, `.kilo/`, and docs; HEAD is `7b63694`.
- **[N]** No `LICENSE` file; README says internal/not specified.
- **[T]** 403 unreferenced review JPEGs exist within the 7-day retention window (housekeeping, not a confirmed leak).

## 14. Supporting system

This section is context for the AI component. It is not a full system audit; the authoritative lifecycle detail is in `docs/Session Lifecycle - Current State Report.md`.

### 14.1 Versions and environment

| Component | Version / value | Label |
|---|---|---|
| Python | 3.15.2 | **[T]** |
| FastAPI | 0.162.2 | **[T]** |
| Uvicorn | 0.54.0 | **[T]** |
| OpenCV | 5.0.0.93 (`opencv-python`) | **[T]** |
| NumPy | 2.5.4 | **[T]** |
| SQLAlchemy | 2.1.8 | **[T]** |
| Pydantic | 2.14.3 | **[T]** |
| pytest | 9.2.0 | **[T]** |
| matplotlib | 3.11.2 | **[T]** |
| argon2-cffi | 25.1.0 | **[T]** |
| Database | SQLite (`attendance.db`) | **[T]** |
| OS / CPU / RAM | Windows 11 Pro 10.0.26300; Intel i7-8665U, 4C/8T; 8,357,949,440 bytes | **[T]** |
| `requirements.txt` | Lower bounds only: `fastapi>=0.115`, `uvicorn>=0.30`, `opencv-python>=4.8`, `numpy>=2`, `SQLAlchemy>=2.0`, `argon2-cffi>=23.1`, `python-multipart>=0.0.9`, `tzdata>=2024.1`, `matplotlib>=3.8`, plus test/dev tools | **[T]** |

**Environment variables** (`app/config.py`; defaults shown):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./attendance.db` | DB connection |
| `SECRET_KEY` | `dev-insecure-secret-key-change-me` | HMAC/session signing; must be changed outside dev |
| `APP_ENV` | `development` | Enables the insecure-default guard outside dev |
| `MATCH_THRESHOLD` | `0.363` | Cosine accept threshold |
| `AMBIGUITY_MARGIN` | `0.05` | Top-two separation |
| `COOLDOWN_SECONDS` | `60` | Legacy sign-in cooldown |
| `MAX_FAILED_ATTEMPTS` | `5` | Login lockout threshold |
| `REVIEW_RETENTION_DAYS` | `7` | Review-frame retention |
| `TIMEZONE` | `UTC` | Local-day/shift calculations |
| `MIN_FACE_WIDTH` | `120` | Quality gate (px) |
| `BLUR_THRESHOLD` | `100.0` | Laplacian-variance gate |
| `SESSION_TIMEOUT_MINUTES` | `30` | Admin session TTL |
| `LOCKOUT_MINUTES` | `5` | Login lockout duration |
| `SESSION_COOKIE_SECURE` | `false` | Secure cookie flag |
| `WORK_START` | `09:00` | Legacy lateness default |
| `GRACE_MINUTES` | `15` | Lateness grace |
| `MAX_UPLOAD_BYTES` | `5,242,880` | Upload cap |

### 14.2 Architecture

- **[T]** FastAPI application in `app/main.py`; lifespan runs `init_db()`, review-frame cleanup, best-effort missing-session close, then creates the in-memory `SessionStore`, `Recognizer` and `GalleryCache`.
- **[T]** Routers: `auth`, `staff`, `shifts`, `signin`, `attendance`, `review`, `settings`, `clock`, `approvals`, `metrics`; static mounts at `/admin` and `/static`; `GET /health`.
- **[T]** SQLite via SQLAlchemy 2.x; `check_same_thread=False` (single-site).
- **[T]** Plain HTML/CSS/JS front end served by FastAPI; no build step; Chart.js vendored at `static/vendor/chart.umd.min.js`.
- **[T]** Kiosk state machine in `static/signin_state.js` (pure, Node-tested) plus DOM/network code in `static/signin.js`.
- **[T]** Server-authoritative attendance lifecycle in `app/attendance_service.py`: at most one open session (`open_key` unique), token idempotency (`token_jti` unique), clock-out links to the exact clock-in (`paired_in_id`), and completed-shift rejection.
- **[T]** Clock tokens in `app/clock_tokens.py`; shift rules in `app/shifts.py` and `app/shift_service.py`; metrics in `app/metrics.py`.

### 14.3 Data model

| Table | Rows in audited DB | Purpose | Label |
|---|---:|---|---|
| `admins` | 1 | Admin/manager accounts, role, department scope, lockout | **[T]** |
| `staff` | 2 | Staff identity, active/anonymised flags | **[T]** |
| `consents` | 2 | Consent text version, timestamp, recording admin | **[T]** |
| `face_embeddings` | 4 | 128-d float32 vectors, model name/version, staff FK | **[T]** |
| `attendance` | 5 | In/out events, method, flags, shift link, lifecycle keys | **[T]** |
| `recognition_attempts` | 85 | AI decision log (outcome, score, candidate, frame) | **[T]** |
| `audit_logs` | 12 | Admin action audit | **[T]** |
| `settings` | 7 | Live overrides for thresholds/retention/shift defaults | **[T]** |
| `patterns` | 6 | Shared/personal shift patterns | **[T]** |
| `pattern_days` | 31 | Weekday start/end/overnight per pattern | **[T]** |
| `assignments` | 3 | Staff ↔ pattern effective dates | **[T]** |
| `shift_exceptions` | 0 | Day off / custom hours | **[T]** |
| `clock_tokens` | 15 | Single-use scan→clock tokens | **[T]** |
| `approval_requests` | 0 | Manager approval queue for out-of-window actions | **[T]** |

**Relationships.** `Staff` 1–N `Consent`, `FaceEmbedding`, `Attendance`; `Attendance.paired_in_id` self-FK; `Attendance.shift_id` → `patterns.id`; `Pattern` 1–N `PatternDay`; `Assignment` links `Staff` and `Pattern`; `RecognitionAttempt.best_staff_id` → `staff.id`; `ClockToken.staff_id` → `staff.id`; `ApprovalRequest.staff_id`/`decided_by` FKs. **[T]** (`app/models.py`).

**Biometric storage.** `EmbeddingVector` is a `TypeDecorator` over `LargeBinary`: `np.asarray(value, dtype=np.float32).reshape(-1).tobytes()` on write and `np.frombuffer(...)` on read. No encryption. **[T]** (`app/models.py`).

### 14.4 API inventory

**57 route decorators** in `app/routers/` plus `GET /health` = **58 HTTP endpoints**. Counts by router: approvals 5, attendance 8, auth 3, clock 3, metrics 11, review 3, settings 3, shifts 12, signin 2, staff 7. **[T]** (Appendix A C19).

```text
PUBLIC / KIOSK
POST /api/scan                         POST /api/signin
POST /api/clock                        POST /api/clock/undo
POST /api/clock/request                POST /api/admin/login

ADMIN AUTH / STAFF
POST /api/admin/logout                 GET  /api/admin/me
GET  /api/admin/staff                  POST /api/admin/staff
GET  /api/admin/staff/{id}             PATCH /api/admin/staff/{id}
POST /api/admin/staff/{id}/deactivate  DELETE /api/admin/staff/{id}
POST /api/admin/staff/{id}/enrol

REVIEW / SETTINGS / AUDIT
GET  /api/admin/review                 GET  /api/admin/review/{id}/frame
POST /api/admin/review/{id}/resolve    GET  /api/admin/settings
PUT  /api/admin/settings               GET  /api/admin/audit

ATTENDANCE / APPROVALS
GET  /api/admin/attendance             POST /api/admin/attendance
PATCH /api/admin/attendance/{id}       GET  /api/admin/attendance/dashboard
GET  /api/admin/attendance/export.csv  GET  /api/admin/attendance/report/monthly.csv
GET  /api/admin/attendance/sessions    GET  /api/admin/attendance/sessions.csv
GET  /api/admin/approvals              POST /api/admin/approvals/{id}/approve
POST /api/admin/approvals/{id}/reject  GET  /api/admin/approvals/missing-clockout
POST /api/admin/approvals/missing-clockout/{id}/close

SHIFTS
GET  /api/admin/shifts/people
GET  /api/admin/shifts/patterns        POST /api/admin/shifts/patterns
PUT  /api/admin/shifts/patterns/{id}   DELETE /api/admin/shifts/patterns/{id}
POST /api/admin/shifts/patterns/{id}/customise/{staff_id}
POST /api/admin/shifts/assignments     POST /api/admin/shifts/exceptions
POST /api/admin/shifts/exceptions/bulk
POST /api/admin/shifts/staff/{id}/week
GET  /api/admin/shifts/staff/{id}/history
GET  /api/admin/shifts/staff/{id}/resolved

METRICS
GET /api/admin/metrics/summary         GET /api/admin/metrics/trend
GET /api/admin/metrics/breakdown       GET /api/admin/metrics/arrivals
GET /api/admin/metrics/people          GET /api/admin/metrics/person/{id}
GET /api/admin/metrics/person/{id}/export.csv
GET /api/admin/metrics/insights        GET /api/admin/metrics/reports/late-arrivals
GET /api/admin/metrics/reports/attendance-summary
GET /api/admin/metrics/followup

HEALTH
GET /health
```

**Authorisation summary.** Staff/review/settings/audit and most attendance endpoints use `require_admin` or `require_admin_csrf`; approvals, shift endpoints, sessions and all metrics use `require_role(ROLE_ADMIN, ROLE_MANAGER)`; manager access is department-scoped where the handler applies `can_access_staff`/`scoped_staff_query`; kiosk and clock endpoints are public but token/kiosk-header constrained. **[T]** (`app/security.py`; route inventory C19).

### 14.5 Interfaces

- **Kiosk:** `static/signin.html` + `static/signin.js` + `static/signin_state.js`; camera on demand, live clock, scan every 1.4 s, match → confirm → clock in/out, 10 s undo, challenge overlay, review/approval messaging. **[T]**
- **Admin:** 15 HTML pages under `static/admin/`: login, index/dashboard, staff, enrol, attendance, review, settings, audit, approvals, person, reports, shifts index, and redirect stubs for assign/exceptions/preview. JS modules per page; shared `common.js`. **[T]**
- **Exports:** attendance CSV, monthly CSV, sessions CSV, person metrics CSV, late-arrivals and attendance-summary reports. **[T]**
- **No mobile app, no public API docs page, no webhooks, no email/SMS.** **[N]**

### 14.6 Shifts and metrics

- **[T]** Shift resolution order: personal pattern → assigned shared pattern → none (`app/shifts.py`, `resolve_shift`; README).
- **[T]** Pattern days support weekday, start/end, overnight flag, enabled flag; exceptions are `day_off` or `custom_hours`; assignments have effective-from/to.
- **[T]** Phase 3 uses copy-on-write for personal customisation, archives patterns, and blocks deletion while current/future assignments exist (`app/shift_service.py`; `app/routers/shifts.py`).
- **[T]** Clock evaluation produces `allowed_actions`, `message`, `flag`, `confirm_required`; the kiosk maps these to UI states (`app/shifts.py`; `static/signin_state.js`).
- **[T]** Metrics endpoints cover summary, trend, breakdown, arrivals, people, person, person CSV, insights, late arrivals, attendance summary and follow-up; all accept `from`/`to` (max 366 days) and an optional department filter that can only narrow manager scope (`app/routers/metrics.py`).
- **[T]** Metrics formulas were verified against a hand-built 3-person/2-week dataset (README table); this is not an AI evaluation.
- **[T]** `insights` endpoint exists but the UI panel is hidden (README).
- **[T]** Missing clock-outs are auto-closed after `missing_close_hours` (default 4) and never counted as worked time; managers correct with a required reason (`app/approvals.py`; `app/routers/approvals.py`).

### 14.7 Security posture

**Implemented.** Argon2 password hashing; opaque server-side sessions; HttpOnly/SameSite=Lax cookie with configurable Secure; CSRF header on mutations; login lockout; role and department scoping; audit log; upload cap; non-default `SECRET_KEY` guard outside dev; HMAC single-use clock tokens; 20 s token/10 s undo; review-frame access via admin endpoint only; no embedding/image logging. **[T]**

**Not implemented.** Kiosk authentication; `/api/scan` rate limiting; MFA; password-change UI; persistent sessions; HTTPS/HSTS/proxy hardening; encryption at rest; key rotation; Alembic migrations; centralized log retention; dependency pinning (requirements use lower bounds). **[N]**

### 14.8 Tests and repository state

- **[T]** 30 test files, 270 tests collected; full suite **264 passed, 6 skipped, 2 warnings in 82.69 s** against a DB copy.
- **[T]** Six skips are missing `tests/fixtures/one_face.jpg` (4), `no_face.jpg` (1), `two_faces.jpg` (1).
- **[T]** Node kiosk-state test passes 5 cases (`tests/js/`; `static/signin_state.js`).
- **[T]** Working tree is dirty: 31 modified tracked files, many untracked files including `app/attendance_service.py`, `static/signin_state.js`, lifecycle tests, backup DBs, `.kilo/`, `_wtest.py`, and docs. HEAD is `7b63694`.
- **[T]** 39 commits total; no CI configuration was found. **[N]** for CI.

### 14.9 Git phases

| Phase | Commits | Examples | Label |
|---|---:|---|---|
| Docs/PRD | 1 | `7e0e73b` PRD and guide | **[T]** |
| Phase 1 core (steps 1–15) | 15 | skeleton, YuNet/SFace spike, models, recognition, auth, staff/enrolment, kiosk, attendance, review/settings, liveness, evaluation, hardening | **[T]** |
| Phase 2 (C–K + kiosk UI) | 13 | shift rules/API/UI, `/api/scan`, `/api/clock`, approvals, shift-aware views, kiosk refinements | **[T]** |
| Phase 3 (B, D, I, K + kiosk fix) | 5 | pattern schema, copy-on-write, schedules editor, legacy redirects | **[T]** |
| Steps 1B/2A–2C/2D/3A/3B/3C | 5 | duplicate/mixed-person enrolment, metrics/dashboard, insights, person/reports, README | **[T]** |
| **Total** | **39** | | **[T]** |

## 15. Presentation plan

### 15.1 Screenshot plan (17 screens; 15 AI-related)

Take screenshots at a resolution where text is readable (for example 1440×900 or 1920×1080), use fictional/demo staff only, and redact any real names, staff codes, scores or faces that are not consented. For the AI-flow screens, show the state and the explanation, not just a pretty page.

| # | Screen / file | What to show | AI relevance |
|---:|---|---|---|
| 1 | `static/signin.html` idle | Kiosk idle state, camera off, "Start" prompt | AI entry point |
| 2 | Kiosk camera active | Live preview, face guide, scan indicator | Capture/quality stage |
| 3 | Match success | Identified person, shift context, clock-in/out choice | 1-to-N result + human confirmation |
| 4 | No face / multiple faces | Rejection message and retry guidance | Quality gate |
| 5 | Blurry / too small | Rejection message with reason | Quality gate |
| 6 | Ambiguous result | "Not recognised"/review message | Ambiguity margin |
| 7 | Spoof challenge (if enabled) | Left/right turn prompt and second frame | Liveness |
| 8 | Review queue | Unresolved attempts with candidate/score | Human oversight |
| 9 | Review frame | Captured failure frame and assign/dismiss controls | Human oversight |
| 10 | `static/admin/enrol.html` consent | Consent checkbox and notice | Consent |
| 11 | Enrolment quality results | Accepted/rejected per image with reasons | Enrolment safeguards |
| 12 | Duplicate/mixed-person block | 409/422 message, "nothing stored" | Enrolment safeguards |
| 13 | Staff detail | Embedding count, model name/version, delete action | Biometric storage/deletion |
| 14 | Settings page | `match_threshold`, `ambiguity_margin`, `spoof_challenge` | Threshold governance |
| 15 | Evaluation output | `metrics.json` zeros and `far_frr_curve.png` | Evaluation honesty |
| 16 | Test output | `264 passed, 6 skipped` and AI subset `78 passed, 6 skipped` | Verification |
| 17 | Dashboard/attendance | Attendance sessions and flags | Supporting system |

**Minimum 8 AI-flow screens:** #1–#12 and #14–#16 are AI-related; the plan exceeds the requirement. If time is short, keep #1, #3, #4, #8, #9, #10, #11, #12, #14, #15, #16.

### 15.2 Slide outline (15 slides; 9 AI-focused = 60%)

| # | Slide | Content | AI weight |
|---:|---|---|---|
| 1 | Title and problem | Attendance friction, buddy punching, why face recognition | Context |
| 2 | System overview | Kiosk → FastAPI → SQLite → admin; scan proves identity, clock writes attendance | Context |
| 3 | Models | YuNet + SFace, file sizes/hashes, pre-trained, no fine-tuning | AI |
| 4 | Recognition pipeline | Decode → detect → quality → embed → cosine → decide | AI |
| 5 | Decision rule and thresholds | 0.363, 0.05 margin, per-person best, ambiguity | AI |
| 6 | Enrolment safeguards | Consent, 3–5 images, mixed-person, duplicate, atomic failure | AI |
| 7 | Liveness and spoofing | Head-turn challenge, 0.08 delta, default off, limits | AI |
| 8 | Failure handling and review | Outcomes, 3-in-2-min trigger, assign/dismiss, audit | AI |
| 9 | Evaluation design | Genuine/impostor scores, FAR/FRR, sweep, conditions | AI |
| 10 | Evaluation reality | Empty `eval_data/`, five zero runs, why 0.0 is an artifact | AI |
| 11 | Tests and evidence | 264 passed/6 skipped, AI subset, what is not tested | Context |
| 12 | Responsible AI | Privacy, consent, deletion, bias, security, risk matrix | AI |
| 13 | Supporting system | Shifts, clock tokens, approvals, metrics, exports | Context |
| 14 | Limitations and roadmap | Real evaluation, liveness, bias, encryption, kiosk auth | Context |
| 15 | Conclusion / demo | What is built, what is proven, what is not | Context |

**Weighting check.** Slides 3–10 and 12 are AI-focused: **9 of 15 = 60%**, matching the requested weighting. Slides 2, 11, 13, 14 and 15 are supporting context. The outline can be trimmed to 14 slides by merging 3 and 4, or expanded to 16 by splitting evaluation design/reality.

### 15.3 Demo script (5 minutes)

1. Show the kiosk idle state and start the camera. **[T]**
2. Scan a demo face; show the match, shift context and clock confirmation. **[T]**
3. Scan a non-enrolled/obscured face; show the rejection and review queue. **[T]**
4. Enrol a demo staff member with consent and 3 images; show the quality results. **[T]**
5. Attempt a duplicate face; show the 409 block and "nothing stored". **[T]**
6. Open settings and explain the threshold; open the evaluation output and explain why it is empty. **[T]**
7. Close on the risk matrix and the real-evaluation plan. **[N]**

---

## 16. What could not be determined

- **Real recognition performance.** No labelled data, no genuine/impostor scores, no FAR/FRR/accuracy/EER. The five runs are zero-sample artifacts. **[N]**
- **Whether 0.363 is a good threshold for this population.** The repository does not contain the data or the analysis. **[N]**
- **Fairness across skin tone, age, gender, disability, glasses, lighting or angle.** No demographic data or tests. **[N]**
- **Model licences, citations, upstream training data and intended use.** Not documented in the repository; upstream model cards were not fetched during this audit. **[N]**
- **Liveness effectiveness against photos, screens, video replay, masks or deepfakes.** No test data. **[N]**
- **Throughput under concurrent kiosks or a large gallery.** Only a single-image microbenchmark exists. **[N]**
- **Whether the production DB has the same contents as the audited copy.** The audit used read-only copies; the original was not queried for this report beyond the earlier lifecycle work. **[N]**
- **Whether the dirty working tree is deployable.** It is uncommitted and contains untracked backup DBs and test artifacts; no CI result exists. **[N]**
- **Legal compliance.** No DPIA, privacy notice, lawful-basis analysis, retention schedule for attempts/audit logs, or processor agreement. **[N]**
- **Who is accountable for the system in operation.** No owner/governance/incident procedure is documented. **[N]**

---

## 17. Uncertainties and caveats

- **Timing is indicative.** The microbenchmark used one image and one process; the first embed includes warm-up (49.7 ms vs ~20 ms later). Do not present it as a service SLA. **[I]**
- **The audited DB is a development/demo database.** It has 2 staff, 4 embeddings, 5 attendance rows and 85 attempts. It is not a production sample. **[T]**
- **The 403 unreferenced review JPEGs are within the 7-day retention window.** They may be normal development artifacts; no conclusion about a leak should be drawn from the count alone. **[T]**
- **The 6 skipped tests mean the real-image quality path is not covered by the current suite.** The code path is present and was exercised by the audit microbenchmark, but not by a committed fixture test. **[I]**
- **`Attendance.distance`/`model_version` are unused columns.** This may be legacy schema; it means score/model traceability is weaker than the schema suggests. **[T]**
- **The kiosk header is not authenticated.** `X-Kiosk-ID` is client-supplied; this is a design limitation, not a discovered exploit. **[T]/[N]**
- **The report's claim labels are about evidence in this repository.** A label of **[T]** does not mean the behaviour is correct for every deployment; it means it is implemented and covered by a test or direct read-only verification. **[T]**

## Appendix A. Commands and evidence log

All commands were run read-only in `D:\work\faceattendance` unless stated. Tests and DB queries used copies under `C:\Users\ADMIN\AppData\Local\Temp\opencode\`; the original `attendance.db` was not written. File reads (`app/*.py`, `tests/*.py`, `static/*`, `README.md`, `docs/*`) are cited inline and are not repeated here.

### A.1 Command index

| ID | Command (abridged) | Purpose | Key result |
|---|---|---|---|
| C1 | Recursive file listing excluding `.git`, `.venv`, `__pycache__`, `node_modules` | Inventory | 30 test files, 15 admin HTML pages, 2 ONNX models |
| C2 | `git status --short` | Working-tree state | Dirty; 31 modified, many untracked |
| C3 | `git log --reverse --format="%h %ad %s" --date=short` | History/phases | 39 commits |
| C4 | `git rev-list --count HEAD` | Commit count | 39 |
| C5 | `Get-ChildItem models -File \| Select Name,Length` | Model sizes | 232,589 and 38,696,353 bytes |
| C6 | `Get-FileHash ... -Algorithm SHA256` | Model integrity | Two SHA-256 hashes |
| C7 | `.\.venv\Scripts\python.exe --version` | Python version | 3.15.2 |
| C8 | `.\.venv\Scripts\python.exe -m pip freeze` | Dependency versions | 40 packages |
| C9 | `Get-ChildItem eval_results -Directory`; `Get-ChildItem eval_data` | Evaluation assets | 5 runs; only `.gitkeep` |
| C10 | `$env:DATABASE_URL=...; pytest -q` | Full suite | 264 passed, 6 skipped, 2 warnings, 82.69 s |
| C11 | `pytest -q -rs` on 11 AI files | AI subset | 78 passed, 6 skipped, 16.03 s |
| C12 | CPU/RAM/OS PowerShell query | Environment | i7-8665U, 4C/8T, 8.36 GB, Win 11 26300 |
| C13 | Inline Python timing of `Recognizer()`/`analyse_frame`/`embed` on `var/lena.jpg` | Performance | Load 172.9 ms; analyse 23.7–29.1 ms; embed 20.1–49.7 ms |
| C14 | Inline Python read of `metrics.json` for each run | Evaluation results | All zeros; empty data |
| C15 | SQLite aggregate query on `attendance-ro.db` | Attempt scores | 15 matched, 25 rejected, 45 no_face |
| C16 | SQLite table counts | Data model evidence | 14 tables counted |
| C17 | SQLite query on frames/attendance/settings | Logging/traceability | 12 frame refs, 0/5 distance/model_version, 7 settings |
| C18 | `pytest --collect-only -q` per file | Test inventory | 270 collected across 30 files |
| C19 | Inline Python route parser | API inventory | 57 routes; 10 routers |
| C20 | `Get-ChildItem ... \| Measure-Object -Line` | Code size | app/tests/scripts/static counts |
| C21 | `Get-ChildItem static -Recurse` | Front-end inventory | 15 admin HTML, 21 JS, 2 CSS |
| C22 | `Select-String README.md` for headings/limitations/licence | Documentation | Known limitations and zero-eval statement |
| C23 | Grep for `.fit(`, `train(`, fine-tune, optimizer, backward, epoch | Training check | No matches |
| C24 | Grep for `TODO|FIXME|XXX|HACK` in `.py` | Debt markers | No matches |
| C25 | Read `scripts/download_models.py` | Model provenance | OpenCV Zoo URLs |
| C26 | Grep tests for `attendance.db` | DB safety | No direct references; in-memory fixtures |
| C27 | SQLite `select key,value from settings` | Live AI settings | 7 rows, defaults |
| C28 | `TestClient(app).get("/health")` | Runtime smoke | `200 {"status":"ok"}` |
| C29 | Count `var/review` files and mtimes | Retention evidence | 415 files; 0 older than 7 days |
| C30 | Grep docs for AI/evaluation terms | Supporting docs | Course guide requires real volunteers |
| C31 | Re-list models/eval dirs | Cross-check | Same sizes; 5 runs |
| C32 | Grep staff constants | Enrolment config | `CONSENT_TEXT_VERSION="v1"`, 3–5 images |
| C33 | Grep cookie flags | Security | HttpOnly, SameSite=Lax, Secure configurable |
| C34 | Grep metrics/shift route decorators | API detail | 11 metrics, 12 shifts |
| C35 | Read README enrolment section | Enrolment wording | Consent/duplicate/atomicity |
| C36 | Read lifecycle report header | Supporting context | Server-authoritative lifecycle |
| C37 | Full suite re-run against DB copy | Confirm counts | 264 passed, 6 skipped, 82.69 s |
| C38 | `pip freeze` re-run | Versions | Same 40 packages |
| C39 | Reconcile `var/review` files to DB `frame_path` | Retention/leak check | 415 disk, 12 referenced, 403 unreferenced, 0 missing |

### A.2 Raw output: repository state (C2)

```text
 M app/approvals.py
 M app/db.py
 M app/models.py
 M app/routers/approvals.py
 M app/routers/attendance.py
 M app/routers/clock.py
 M app/routers/review.py
 M app/routers/settings.py
 M app/routers/shifts.py
 M app/routers/signin.py
 M app/routers/staff.py
 M app/security.py
 M app/settings_store.py
 M app/shift_service.py
 M app/shifts.py
 M app/signin.py
 M requirements.txt
 M static/admin/attendance.html
 M static/admin/css/admin.css
 M static/admin/js/attendance.js
 M static/admin/js/settings.js
 M static/admin/js/shifts_patterns.js
 M static/admin/js/shifts_week_editor.js
 M static/admin/settings.html
 M static/signin.html
 M static/signin.js
 M tests/test_attendance.py
 M tests/test_clock.py
 M tests/test_scan.py
 M tests/test_shift_people_history.py
 M tests/test_shifts.py
?? .kilo/
?? _wtest.py
?? app/attendance_service.py
?? attendance.db.bak-clear-20261109-190909
?? attendance.db.bak-clockbugs-20261109-145500
?? attendance.db.bak-phase3-stepB-20261109
?? attendance.db.bak-phase3-stepC-apply-20261109
?? attendance.db.bak-phase3-stepD-20261109
?? attendance.db.bak-phase3-stepE-20261109
?? attendance.db.bak-phase3-stepF-20261109
?? attendance.db.bak-step1B-20261109-145329
?? attendance.db.bak-step1B-20261109-145545-confirm
?? attendance.db.bak-step3C-20261109-151943
?? attendance.db.bak-stepD-20261108175506
?? attendance.db.bak-stepS1-20261109-170853
?? "docs/Face Attendance System Phase 2 Guide (Clock In Out and Shift Management).docx"
?? "docs/Face Attendance System Phase 2 Guide (Clock In Out and Shift Management).md"
?? "docs/Face Attendance System Phase 3 Guide (Simpler, Robust Shift Management).docx"
?? "docs/Face Attendance System Phase 3 Guide (Simpler, Robust Shift Management).md"
?? "docs/Session Lifecycle - Current State Report.md"
?? "docs/Shift Lifecycle Audit - Report.md"
?? scripts/diagnose_attendance.py
?? static/signin_state.js
?? tests/js/
?? tests/test_attendance_lifecycle.py
?? tests/test_attendance_sessions.py
?? tests/test_clock_bugs.py
?? tests/test_frontend_state.py
```

### A.3 Raw output: model files and hashes (C5–C6)

```text
Name                                  Length
----                                  ------
.gitkeep                                   0
face_detection_yunet_2023mar.onnx     232589
face_recognition_sface_2021dec.onnx 38696353

SHA256 8F2383E4DD3CFBB4553EA8718107FC0423210DC964F9F4280604804ED2552FA4
  models/face_detection_yunet_2023mar.onnx
SHA256 0BA9FBFA01B5270C96627C4EF784DA859931E02F04419C829E83484087C34E79
  models/face_recognition_sface_2021dec.onnx
```

### A.4 Raw output: performance (C13)

```text
[ WARN:0@2.010] global net_impl_backend.cpp:345 cv::dnn::dnn5_v20260605::Net::Impl::setPreferableTarget Targets are not supported by the new graph engine for now
[ WARN:0@2.166] global net_impl_backend.cpp:345 cv::dnn::dnn5_v20260605::Net::Impl::setPreferableTarget Targets are not supported by the new graph engine for now
model_load_ms=172.9
image_bytes 91814 min_face_width 120 blur_threshold 100.0
run0 analyse_ms=27.5 status=ok faces=1 box=(207, 182, 145, 206) embed_ms=49.7 shape=(128,) dtype=float32 norm=1.000000
run1 analyse_ms=25.3 status=ok faces=1 box=(207, 182, 145, 206) embed_ms=23.2 shape=(128,) dtype=float32 norm=1.000000
run2 analyse_ms=23.7 status=ok faces=1 box=(207, 182, 145, 206) embed_ms=20.3 shape=(128,) dtype=float32 norm=1.000000
run3 analyse_ms=25.5 status=ok faces=1 box=(207, 182, 145, 206) embed_ms=20.8 shape=(128,) dtype=float32 norm=1.000000
run4 analyse_ms=29.1 status=ok faces=1 box=(207, 182, 145, 206) embed_ms=20.1 shape=(128,) dtype=float32 norm=1.000000
```

### A.5 Raw output: tests (C10–C11, C37)

```text
264 passed, 6 skipped, 2 warnings in 82.69s (0:01:22)

SKIPPED [4] tests\test_recognition.py:24: fixture not added: D:\work\faceattendance\tests\fixtures\one_face.jpg
SKIPPED [1] tests\test_recognition.py:24: fixture not added: D:\work\faceattendance\tests\fixtures\no_face.jpg
SKIPPED [1] tests\test_recognition.py:24: fixture not added: D:\work\faceattendance\tests\fixtures\two_faces.jpg
78 passed, 6 skipped, 1 warning in 16.03s
84 tests collected in 0.24s
```

### A.6 Raw output: evaluation (C14)

```text
20261108T181117Z enrolled=0 genuine=0 impostor=0 rejected=0 ref_t=0.0 far_zero=0.0 spoof_total=0
  files: far_frr_curve.png, metrics.json, results.csv, sweep.csv
20261108T181401Z enrolled=0 genuine=0 impostor=0 rejected=0 ref_t=0.0 far_zero=0.0 spoof_total=0
  files: far_frr_curve.png, metrics.json, results.csv, sweep.csv
20261109T113446Z enrolled=0 genuine=0 impostor=0 rejected=0 ref_t=0.0 far_zero=0.0 spoof_total=0
  files: far_frr_curve.png, metrics.json, results.csv, sweep.csv
20261109T121932Z enrolled=0 genuine=0 impostor=0 rejected=0 ref_t=0.0 far_zero=0.0 spoof_total=0
  files: far_frr_curve.png, metrics.json, results.csv, sweep.csv
20261109T135936Z enrolled=0 genuine=0 impostor=0 rejected=0 ref_t=0.0 far_zero=0.0 spoof_total=0
  files: far_frr_curve.png, metrics.json, results.csv, sweep.csv
eval_data entries: ['.gitkeep']
sweep rows: 101 (all zero)
results.csv: header only
```

### A.7 Raw output: database aggregates (C15–C17)

```text
attempts by outcome/resolved: [('matched', 0, 15), ('no_face', 0, 45), ('rejected', 0, 25)]
matched score min/avg/max: 0.407944 / 0.602199 / 0.795999
rejected score min/avg/max: 0.228580 / 0.255259 / 0.293241
attempt time range: 2026-11-09 16:11:27.737552 .. 2026-11-10 06:19:33.979337
attempts with frames: 12; resolved: 0; kiosks: [('entrance-main', 85)]
attendance total/distance_nonnull/model_version_nonnull: (5, 0, 0)
attendance methods: [('face', 4), ('manual', 1)]
attendance flags: [('early_departure', 1), ('late', 2), ('missing_clock_out', 1), ('overtime', 1)]
embeddings per staff: [(1, 4)]
settings: [('ambiguity_margin','0.05'), ('cooldown_seconds','60'), ('grace_minutes','15'), ('match_threshold','0.363'), ('review_retention_days','7'), ('spoof_challenge','False'), ('work_start','09:00')]
```

### A.8 Raw output: API inventory (C19)

```text
approvals.py      5  prefix=/api/admin/approvals
attendance.py     8  prefix=/api/admin/attendance
auth.py           3  prefix=/api/admin
clock.py          3  prefix=
metrics.py       11  prefix=/api/admin/metrics
review.py         3  prefix=/api/admin/review
settings.py       3  prefix=/api/admin
shifts.py        12  prefix=/api/admin/shifts
signin.py         2  prefix=
staff.py          7  prefix=/api/admin/staff
TOTAL 57
```

### A.9 Raw output: runtime and review frames (C28–C29)

```text
200 {"status":"ok"}
var/review files: 415
db frame rows: 12; distinct: 12; missing on disk: 0
disk jpg not referenced by DB: 403
oldest review file: 2026-11-08 14:38:18
newest review file: 2026-11-10 08:41:41
files older than 7 days: 0
```

### A.10 Raw output: README limitations/evaluation/licence (C22)

```text
251: ## Known limitations
253: - **Not certified anti-spoofing.** Optional head-turn challenge helps against still photos but may
254:   not stop video replay; a liveness aid, not a security guarantee.
267: ## Recognition evaluation
269: Uses the **same Recognizer/threshold path as the app** ...
270: `eval_data/` has **no genuine/impostor pairs**; runs yield `enrolled=0 genuine=0 impostor=0 rejected=0
271: FAR=0 FRR=0` ...
273: missing data exits `2`. **No FAR/FRR fabricated ...**
284: - **Real recognition FAR/FRR not measured** (no labeled `eval_data/`); eval runs all zeros
309: ## License
311: Internal / not specified. Add a `LICENSE` file before external distribution.
```

### A.11 Training and debt checks (C23–C24)

```text
--- training/fine-tuning grep ---
(no matches for .fit( | train( | fine.?tun | optimizer | backward( | epoch)
--- TODO/FIXME/XXX/HACK in .py ---
(no matches)
```

### A.12 Version inventory (C8/C38, abridged to direct dependencies)

```text
fastapi==0.162.2
uvicorn==0.54.0
opencv-python==5.0.0.93
numpy==2.5.4
SQLAlchemy==2.1.8
pydantic==2.14.3
pytest==9.2.0
matplotlib==3.11.2
argon2-cffi==25.1.0
python-multipart==0.0.33
tzdata==2026.6
httpx==0.28.1
starlette==1.10.0
```

### A.13 Git history (C3)

```text
7e0e73b 2026-11-08 docs: add PRD and implementation guide
1638f7c 2026-11-08 step 1: project skeleton with /health
1a11b75 2026-11-08 step 2: recognition spike with YuNet and SFace
091af7d 2026-11-08 step 3: database models and staff anonymisation
705d99b 2026-11-08 step 4: recognition service, rules and gallery cache
4610881 2026-11-08 step 5: admin authentication, sessions and audit logging
9273b93 2026-11-08 step 6: staff CRUD and face enrolment API
3dea2b4 2026-11-08 step 7: admin login, staff and enrolment interface
7c3cd71 2026-11-08 step 8-9: kiosk sign-in API and signin page
bbcc602 2026-11-08 step 10: attendance API, exports and monthly report
d3ab9fe 2026-11-08 step 11: review queue, live settings, audit paging and cleanup
6755907 2026-11-08 step 12: admin attendance, review, settings, audit and dashboard pages
12f315b 2026-11-08 step 13: optional head-turn liveness challenge
d3c7c84 2026-11-08 chore: updated description
46e0c68 2026-11-08 step 14: evaluation script and FAR/FRR metrics
d533d94 2026-11-08 step 15: hardening, demo seed, upload limits and README updates
b20554b 2026-11-08 step C (phase 2): pure shift rules module and tests
ae0cbd4 2026-11-08 step D (phase 2): require_role + shifts/patterns/assignments/exceptions API and schema
938bb84 2026-11-08 step E (phase 2): manager shift UI pages (patterns, assign, exceptions, preview)
deeb64f 2026-11-08 step F (phase 2): /api/scan with clock token + shift evaluate; keep legacy /api/signin
34e62b8 2026-11-08 step G (phase 2): /api/clock consume token+evaluate, undo 10s, approval requests
ff8a7b5 2026-11-08 step H (phase 2): kiosk scan->confirm->clock/undo/request flow
e983d1c 2026-11-08 step I (phase 2): approvals queue, missing-clockout close/correct, audit and UI
35d2845 2026-11-08 step J (phase 2): shift-aware attendance views/exports, dashboard counts, monthly CSV
17f7dc4 2026-11-08 docs: step K README phase 2 and fix approvals nav
76ac824 2026-11-08 refine kiosk UI: guide states, alignment, camera readiness, feedback
82e2729 2026-11-08 kiosk: on-demand camera, live clock, reorganised layout
e3d9c8d 2026-11-08 kiosk: place guide status and cancel inside camera frame
a4c102c 2026-11-08 kiosk: show personal/shift context on early clock-out confirm dialog
40df132 2026-11-09 phase3 B: add Pattern kind/based_on/created_by/archived_at with idempotent SQLite migration
cff5852 2026-11-09 phase3 D: copy-on-write/reset/GC shift service, shared-only pattern list, customise endpoint
06bcf36 2026-11-09 kiosk: keep status/cancel inside camera frame and restore idle panel layout
c6db151 2026-11-09 phase 3 step I: schedules tab and reusable week editor
30024ac 2026-11-09 phase 3 step K: redirect legacy shift pages and update README
6afe87e 2026-11-09 step 1B: block duplicate and mixed-person faces at enrolment
a9e8a62 2026-11-09 Add attendance metrics/dashboard, duplicate-face audit (1C,2A-2C)
0aa29fc 2026-11-09 Add server-side metrics insights (2D), hide dashboard insights panel
5ac58c5 2026-11-09 Implement person page (3A) and reports/follow-up (3B)
7b63694 2026-11-09 Step 3C: README metrics/dashboard/person/reports/follow-up, eval+not-done
```

---

## Appendix B. Claim-to-code index

| Topic | Primary evidence |
|---|---|
| Model loading / quality / embedding | `app/recognition.py` (`Recognizer`, `GalleryCache`) |
| Matching / threshold / ambiguity | `app/rules.py` (`match`, `decide`) |
| Enrolment safeguards | `app/routers/staff.py` (`enrol_staff`); `app/enrolment_checks.py` |
| Liveness | `app/spoof.py` (`nose_offset`, `confirm_head_turn`); `app/routers/signin.py` |
| Scan/outcomes/logging | `app/routers/signin.py` (`_recognise`, `scan_face`); `app/signin.py` (`record_attempt`) |
| Review/oversight | `app/routers/review.py` (`resolve_attempt`); `app/cleanup.py` |
| Threshold settings | `app/settings_store.py`; `app/config.py` |
| Evaluation script/arithmetic | `scripts/evaluate.py`; `app/eval_metrics.py` |
| Biometric storage/deletion | `app/models.py` (`FaceEmbedding`, `delete_staff_data`) |
| Attendance lifecycle | `app/attendance_service.py`; `app/routers/clock.py`; `app/clock_tokens.py` |
| Security | `app/security.py`; `app/routers/auth.py`; `app/config.py` |
| Shifts/metrics | `app/shifts.py`; `app/shift_service.py`; `app/routers/shifts.py`; `app/routers/metrics.py` |
| Tests | `tests/test_recognition.py`, `test_rules.py`, `test_enrolment_checks.py`, `test_spoof.py`, `test_eval_metrics.py`, `test_scan.py`, `test_signin.py`, `test_staff.py`, `test_review_settings.py`, `test_models.py`, `test_frontend_state.py` |

---

**Report status:** complete. The only file created is `docs/IMPLEMENTATION_REPORT.md`. No code, database, settings or existing file was modified. All measured outputs above were produced read-only; tests used a database copy. The central finding is that the AI implementation is real and test-covered at the application level, but its recognition accuracy, threshold justification and fairness remain **unmeasured** because no consented labelled evaluation data exists.





