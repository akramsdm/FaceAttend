(function () {
  "use strict";

  var FRAME_COUNT = 4;
  var COUNTDOWN_SECONDS = 3;

  var staff = null;
  var stream = null;
  var cameraReady = false;
  var captured = [];
  var busy = false;
  var capturing = false;
  var submitted = false;
  var needsRetake = false;
  var cancelRequested = false;

  var els = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function setPageMessage(text, kind) {
    els.pageMessage.textContent = text || "";
    els.pageMessage.className = "message" + (kind ? " " + kind : "");
  }

  function setCameraMessage(text, kind) {
    els.cameraMessage.textContent = text || "";
    els.cameraMessage.className = "message" + (kind ? " " + kind : "");
  }

  function setSlotsMessage(text) {
    els.slotsMessage.textContent = text || "";
  }

  function slot(index) {
    return els.slots.querySelector('[data-slot="' + index + '"]');
  }

  function renderSlot(index, text, kind) {
    var status = slot(index).querySelector("[data-slot-status]");
    status.textContent = text;
    status.className = "slot-status" + (kind ? " " + kind : "");
  }

  function refreshControls() {
    var active = staff !== null && staff.active && !staff.anonymised;
    var consentGiven = els.consent.checked;

    els.startCamera.disabled = busy || cameraReady || !active;
    els.startCamera.textContent = cameraReady ? "Camera ready" : "Start camera";
    els.capture.disabled = !(
      active &&
      consentGiven &&
      cameraReady &&
      !busy &&
      !submitted &&
      !needsRetake &&
      captured.length === 0
    );
    els.cancelCapture.hidden = !capturing;
    els.cancelCapture.disabled = !capturing;
    els.retake.disabled = busy || captured.length === 0;
    els.submit.disabled = !(
      active &&
      consentGiven &&
      !busy &&
      !submitted &&
      !needsRetake &&
      captured.length === FRAME_COUNT
    );
    els.submit.textContent = submitted ? "Enrolment saved" : "Submit for enrolment";
    els.consent.disabled = !active || busy;
  }

  function renderSummary() {
    els.summaryName.textContent = staff.name;
    els.summaryCode.textContent = staff.staff_code;
    els.summaryDepartment.textContent = staff.department || "Not set";
    els.summaryShift.textContent = staff.shift || "Not set";
    els.summaryActive.textContent = staff.active ? "Active" : "Inactive";
    els.summaryEnrolment.textContent = staff.enrolled
      ? "Enrolled (" + staff.embedding_count + " embeddings)"
      : "Not enrolled";

    var canEnrol = staff.active && !staff.anonymised;
    els.staffWarning.hidden = canEnrol;
    els.consentSection.hidden = !canEnrol;
    els.cameraSection.hidden = !canEnrol;

    if (!canEnrol) {
      els.staffWarning.textContent = staff.anonymised
        ? "This staff record has been permanently deleted. Enrolment is not available."
        : "This staff member is inactive. Reactivate them on the Staff page before enrolling faces.";
    }

    refreshControls();
  }

  function cameraErrorMessage(error) {
    if (!error) {
      return "Could not start the camera.";
    }
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "Camera access was blocked. Allow camera permission in the browser and try again.";
    }
    if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") {
      return "No camera was found. Connect a camera and try again.";
    }
    if (error.name === "NotReadableError" || error.name === "TrackStartError") {
      return "The camera is in use by another application. Close it there and try again.";
    }
    return "Could not start the camera. Use localhost or HTTPS and check the camera connection.";
  }

  async function startCamera() {
    if (cameraReady || busy) {
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setCameraMessage(
        "This browser cannot access the camera here. Use a current browser on localhost or HTTPS.",
        "error"
      );
      return;
    }

    els.startCamera.disabled = true;
    setCameraMessage("Requesting camera access...", "info");

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: "user",
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
        audio: false,
      });
      els.video.srcObject = stream;
      await els.video.play();
      cameraReady = true;
      els.placeholder.hidden = true;
      setCameraMessage(
        "Camera ready. Tick the consent box, then select Capture 4 frames.",
        "info"
      );
    } catch (error) {
      stopCamera();
      setCameraMessage(cameraErrorMessage(error), "error");
    } finally {
      refreshControls();
    }
  }

  function stopCamera() {
    if (stream) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
      stream = null;
    }
    if (els.video) {
      els.video.srcObject = null;
    }
    cameraReady = false;
    if (els.placeholder) {
      els.placeholder.hidden = false;
    }
    if (els.countdown) {
      els.countdown.hidden = true;
      els.countdown.textContent = "";
    }
  }

  function sleep(milliseconds) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, milliseconds);
    });
  }

  async function runCountdown() {
    for (var seconds = COUNTDOWN_SECONDS; seconds >= 1; seconds -= 1) {
      if (cancelRequested) {
        throw new Error("cancelled");
      }
      els.countdown.textContent = String(seconds);
      els.countdown.hidden = false;
      await sleep(1000);
    }
    if (cancelRequested) {
      throw new Error("cancelled");
    }
    els.countdown.hidden = true;
    els.countdown.textContent = "";
  }

  function grabFrame() {
    return new Promise(function (resolve, reject) {
      if (!els.video.videoWidth || !els.video.videoHeight) {
        reject(new Error("The camera preview is not ready yet."));
        return;
      }
      var canvas = document.createElement("canvas");
      canvas.width = els.video.videoWidth;
      canvas.height = els.video.videoHeight;
      canvas
        .getContext("2d")
        .drawImage(els.video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(
        function (blob) {
          if (blob) {
            resolve(blob);
          } else {
            reject(new Error("Could not capture a frame."));
          }
        },
        "image/jpeg",
        0.92
      );
    });
  }

  function addFrame(index, blob) {
    var url = URL.createObjectURL(blob);
    captured.push({ blob: blob, url: url });

    var image = slot(index).querySelector(".slot-thumb");
    image.src = url;
    image.alt = "Captured frame " + (index + 1);
    image.hidden = false;
    renderSlot(index, "Captured; not checked yet", "");
  }

  function clearCaptures() {
    captured.forEach(function (frame) {
      URL.revokeObjectURL(frame.url);
    });
    captured = [];
    submitted = false;
    needsRetake = false;

    for (var index = 0; index < FRAME_COUNT; index += 1) {
      var image = slot(index).querySelector(".slot-thumb");
      image.removeAttribute("src");
      image.alt = "";
      image.hidden = true;
      renderSlot(index, "Not captured", "");
    }
    setSlotsMessage("No frames captured yet.");
    refreshControls();
  }

  async function captureSequence() {
    if (
      busy ||
      !staff ||
      !staff.active ||
      !els.consent.checked ||
      !cameraReady
    ) {
      return;
    }

    clearCaptures();
    busy = true;
    capturing = true;
    cancelRequested = false;
    setCameraMessage(
      "Capturing 4 frames. Ask the staff member to hold still and look at the camera.",
      "info"
    );
    refreshControls();

    try {
      for (var index = 0; index < FRAME_COUNT; index += 1) {
        await runCountdown();
        var blob = await grabFrame();
        addFrame(index, blob);
      }
      setCameraMessage(
        "4 frames captured. Check the previews, then submit for enrolment.",
        "success"
      );
      setSlotsMessage("All 4 frames are ready to submit.");
    } catch (error) {
      if (error && error.message === "cancelled") {
        setCameraMessage("Capture cancelled. No frames were kept.", "info");
      } else {
        setCameraMessage(
          (error && error.message) || "Could not capture the frames.",
          "error"
        );
      }
      clearCaptures();
    } finally {
      busy = false;
      capturing = false;
      cancelRequested = false;
      els.countdown.hidden = true;
      els.countdown.textContent = "";
      refreshControls();
    }
  }

  function resetCapturedStatuses() {
    for (var index = 0; index < captured.length; index += 1) {
      renderSlot(index, "Captured; not checked yet", "");
    }
  }

  function applyResults(images) {
    for (var index = 0; index < FRAME_COUNT; index += 1) {
      var result = images[index];
      if (!result) {
        renderSlot(index, "No result returned", "");
      } else if (result.accepted) {
        renderSlot(index, "Accepted", "accepted");
      } else {
        renderSlot(
          index,
          "Rejected: " +
            (result.reason || result.status || "quality check failed"),
          "rejected"
        );
      }
    }
  }

  async function submitEnrolment() {
    if (els.submit.disabled || !staff || captured.length !== FRAME_COUNT) {
      return;
    }

    busy = true;
    setCameraMessage("Checking the 4 frames...", "info");
    setSlotsMessage("Quality checks are running. Keep this page open.");
    for (var index = 0; index < FRAME_COUNT; index += 1) {
      renderSlot(index, "Checking...", "");
    }
    refreshControls();

    var body = new FormData();
    body.append("consent", "true");
    captured.forEach(function (frame, frameIndex) {
      body.append("images", frame.blob, "frame-" + (frameIndex + 1) + ".jpg");
    });

    try {
      var response = await FA.request(
        "/api/admin/staff/" + encodeURIComponent(staff.id) + "/enrol",
        { method: "POST", body: body }
      );
      var payload = await response.json().catch(function () {
        return {};
      });

      if (!response.ok) {
        var detail = payload.detail;
        if (detail && Array.isArray(detail.images)) {
          applyResults(detail.images);
          needsRetake = true;
          setSlotsMessage(
            "Some frames were rejected. Select Retake to capture 4 new frames."
          );
          setCameraMessage(
            detail.message || "At least 3 accepted frames are required.",
            "error"
          );
        } else {
          resetCapturedStatuses();
          setSlotsMessage("No embeddings were stored. Check the message and try again.");
          setCameraMessage(
            typeof detail === "string"
              ? detail
              : "Enrolment failed. No embeddings were stored.",
            "error"
          );
        }
        return;
      }

      applyResults(payload.images || []);
      submitted = true;
      staff.enrolled = true;
      staff.embedding_count = payload.stored_embeddings;
      renderSummary();
      setSlotsMessage(
        "Enrolment saved. " +
          payload.stored_embeddings +
          " embeddings stored; the frames were not saved."
      );
      setCameraMessage(
        "Enrolment saved for " +
          staff.name +
          ". " +
          payload.stored_embeddings +
          " face embeddings stored. No images were saved.",
        "success"
      );
    } catch (error) {
      resetCapturedStatuses();
      setSlotsMessage("No embeddings were stored.");
      setCameraMessage(
        (error && error.message) || "Could not submit the enrolment.",
        "error"
      );
    } finally {
      busy = false;
      refreshControls();
    }
  }

  async function showPicker() {
    els.pickerSection.hidden = false;
    try {
      var response = await FA.request("/api/admin/staff?active=true");
      if (!response.ok) {
        throw new Error(await FA.readError(response));
      }
      var list = await response.json();
      els.staffSelect.replaceChildren();

      if (!list.length) {
        var empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "No active staff found";
        els.staffSelect.appendChild(empty);
        els.staffSelect.disabled = true;
        els.openEnrolment.disabled = true;
        setPageMessage(
          "No active staff members. Add or reactivate a staff member first.",
          "info"
        );
        return;
      }

      list.forEach(function (person) {
        var option = document.createElement("option");
        option.value = String(person.id);
        option.textContent =
          person.staff_code + " - " + person.name +
          (person.enrolled ? " (enrolled)" : " (not enrolled)");
        els.staffSelect.appendChild(option);
      });
      els.staffSelect.disabled = false;
      els.openEnrolment.disabled = false;
    } catch (error) {
      setPageMessage(
        (error && error.message) || "Could not load the staff list.",
        "error"
      );
    }
  }

  async function loadStaff(staffId) {
    try {
      var response = await FA.request("/api/admin/staff/" + staffId);
      if (!response.ok) {
        throw new Error(await FA.readError(response));
      }
      staff = await response.json();
      els.staffSection.hidden = false;
      renderSummary();
    } catch (error) {
      setPageMessage(
        (error && error.message) || "Could not load this staff member.",
        "error"
      );
      await showPicker();
    }
  }

  document.addEventListener("DOMContentLoaded", async function () {
    els.pageMessage = byId("page-message");
    els.pickerSection = byId("picker-section");
    els.pickerForm = byId("picker-form");
    els.staffSelect = byId("staff-select");
    els.openEnrolment = byId("open-enrolment");
    els.staffSection = byId("staff-section");
    els.summaryName = byId("summary-name");
    els.summaryCode = byId("summary-code");
    els.summaryDepartment = byId("summary-department");
    els.summaryShift = byId("summary-shift");
    els.summaryActive = byId("summary-active");
    els.summaryEnrolment = byId("summary-enrolment");
    els.staffWarning = byId("staff-warning");
    els.consentSection = byId("consent-section");
    els.consent = byId("consent");
    els.cameraSection = byId("camera-section");
    els.cameraMessage = byId("camera-message");
    els.video = byId("camera");
    els.placeholder = byId("camera-placeholder");
    els.countdown = byId("countdown");
    els.startCamera = byId("start-camera");
    els.capture = byId("capture");
    els.cancelCapture = byId("cancel-capture");
    els.slots = byId("slots");
    els.slotsMessage = byId("slots-message");
    els.submit = byId("submit-enrolment");
    els.retake = byId("retake");

    FA.wireLogout();
    try {
      var me = await FA.requireSession();
      FA.showAdminName(me.admin.username);
      FA.startSessionWatch();
    } catch (error) {
      return;
    }

    els.pickerForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!els.staffSelect.value) {
        setPageMessage("Choose a staff member to continue.", "error");
        return;
      }
      window.location.href =
        "enrol.html?staff_id=" + encodeURIComponent(els.staffSelect.value);
    });

    els.startCamera.addEventListener("click", startCamera);
    els.capture.addEventListener("click", captureSequence);
    els.cancelCapture.addEventListener("click", function () {
      if (capturing) {
        cancelRequested = true;
        els.cancelCapture.disabled = true;
        setCameraMessage("Cancelling the capture...", "info");
      }
    });
    els.submit.addEventListener("click", submitEnrolment);
    els.retake.addEventListener("click", function () {
      if (busy) {
        return;
      }
      clearCaptures();
      setCameraMessage(
        cameraReady
          ? "Ready to capture 4 new frames."
          : "Start the camera, then capture 4 new frames.",
        "info"
      );
    });
    els.consent.addEventListener("change", function () {
      if (els.consent.checked) {
        setCameraMessage(
          "Consent confirmed in this form. Select Capture 4 frames.",
          "info"
        );
      } else {
        setCameraMessage(
          "Consent is required before capturing or submitting frames.",
          "info"
        );
      }
      refreshControls();
    });

    window.addEventListener("pagehide", stopCamera);

    var rawId = new URLSearchParams(window.location.search).get("staff_id");
    if (rawId && /^[0-9]+$/.test(rawId)) {
      await loadStaff(rawId);
    } else {
      await showPicker();
    }
  });
})();
