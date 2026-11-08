(function () {
  "use strict";

  var SCAN_INTERVAL_MS = 1400;
  var SUCCESS_RESET_MS = 4500;
  var UNDO_MS = 10000;
  var TOKEN_FALLBACK_MS = 20000;
  var CHALLENGE_HOLD_MS = 900;
  var FAIL_REDIRECT_AFTER = 3;
  var FAIL_REDIRECT_MS = 5000;
  var KIOSK_ID = "entrance-main";

  var $ = function (id) { return document.getElementById(id); };
  var systemStatus = $("system-status"), systemStatusText = $("system-status-text");
  var scanView = $("scan-view"), dialogView = $("dialog-view"), successView = $("success-view");
  var video = $("video"), canvas = $("capture-canvas"), faceGuide = $("face-guide"), scanLine = $("scan-line");
  var cameraOverlay = $("camera-overlay"), overlaySpinner = $("overlay-spinner");
  var overlayMain = $("overlay-main"), overlaySub = $("overlay-sub");
  var feedback = $("feedback"), feedbackText = $("feedback-text");
  var dialogIcon = $("dialog-icon"), dialogTitle = $("dialog-title"), dialogDesc = $("dialog-desc");
  var dialogExtra = $("dialog-extra"), dialogActions = $("dialog-actions"), dialogError = $("dialog-error");
  var successIndicator = $("success-indicator"), successHeading = $("success-heading");
  var successName = $("success-name"), successVerify = $("success-verify");
  var detailAction = $("detail-action"), detailTime = $("detail-time"), detailShift = $("detail-shift");
  var finalMessage = $("final-message"), btnUndo = $("btn-undo"), undoSecs = $("undo-secs"), resultNote = $("result-note");
  var manualLink = $("manual-link"), manualHelp = $("manual-help");

  var stream = null, scanTimer = null, inFlight = false, uiState = "initializing";
  var challenge = null; // {dir, firstBlob, startedAt}
  var session = null; // {token, name, shift_summary, message, allowed_actions, expiresAt}
  var lastToken = null, lastEventId = null, lastAction = null, lastShift = null, lastName = null;
  var failCount = 0, resetTimer = null, undoTimer = null;

  function clearTimers() {
    if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
    if (undoTimer) { clearInterval(undoTimer); undoTimer = null; }
  }
  function nowMs() { return Date.now(); }
  function fmtTime(d) {
    var dt = d ? new Date(d) : new Date();
    try { return dt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
    catch (e) { return dt.toLocaleTimeString(); }
  }

  function setSystemStatus(kind, text) {
    systemStatus.className = "system-status is-" + (kind || "loading");
    systemStatusText.textContent = text || "";
  }
  function setFeedback(tone, text) {
    feedback.className = "feedback tone-" + (tone || "neutral");
    feedbackText.textContent = text || "";
  }
  function showView(which) {
    scanView.hidden = which !== "scan";
    dialogView.hidden = which !== "dialog";
    successView.hidden = which !== "success";
  }
  function showOverlay(show, spinner, main, sub) {
    cameraOverlay.hidden = !show;
    overlaySpinner.hidden = !spinner;
    overlayMain.textContent = main || "";
    overlaySub.textContent = sub || "";
  }
  function svg(icon) {
    if (icon === "warn") return '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.5 20h19L12 3z"/><path d="M12 9v4"/><circle cx="12" cy="16.5" r=".7" fill="currentColor"/></svg>';
    if (icon === "error") return '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M15 9 9 15M9 9l6 6"/></svg>';
    if (icon === "info") return '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 11h1v5h1"/></svg>';
    if (icon === "ok") return '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
    return "";
  }
  function setDialog(kind, icon, title, desc, extra, actions) {
    uiState = "dialog";
    dialogIcon.className = "dialog-icon " + (kind || "info");
    dialogIcon.innerHTML = svg(icon || "info");
    dialogTitle.textContent = title || "";
    dialogDesc.textContent = desc || "";
    dialogExtra.textContent = extra || "";
    dialogError.textContent = "";
    dialogActions.innerHTML = "";
    (actions || []).forEach(function (a) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn " + (a.cls || "btn-neutral");
      b.textContent = a.label;
      b.addEventListener("click", a.onClick);
      dialogActions.appendChild(b);
    });
    showView("dialog");
  }

  function scanningEnabled() {
    return !!stream && uiState === "scanning" && !inFlight;
  }

  function resetToScanning(msg) {
    clearTimers();
    session = null; challenge = null; inFlight = false; uiState = "scanning";
    btnUndo.hidden = true; resultNote.textContent = "";
    faceGuide.className = "face-guide waiting"; scanLine.className = "scan-line";
    showOverlay(false);
    showView("scan");
    setSystemStatus("ok", "Camera ready");
    if (msg && msg.text) setFeedback(msg.tone || "neutral", msg.text);
    else setFeedback("neutral", "Position your face inside the guide.");
  }

  function scheduleReset(ms) {
    if (resetTimer) clearTimeout(resetTimer);
    resetTimer = setTimeout(function () { resetToScanning(); }, ms || SUCCESS_RESET_MS);
  }

  function startCamera() {
    uiState = "initializing";
    setSystemStatus("loading", "Starting camera");
    setFeedback("neutral", "Starting camera...");
    faceGuide.className = "face-guide waiting"; scanLine.className = "scan-line";
    showView("scan");
    showOverlay(true, true, "Starting camera...", "Please wait while we prepare the attendance system.");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showCameraUnavailable("Camera unavailable.", "Camera access is not available in this browser. Use a current browser on localhost or HTTPS, or use manual attendance.");
      return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      .then(function (media) {
        stream = media; video.srcObject = media;
        return video.play().catch(function () {});
      })
      .then(function () {
        function waitForVideo() {
          if (video.readyState >= 2 && !video.paused) { resetToScanning(); startScanning(); return; }
          var tries = 0;
          var iv = setInterval(function () {
            tries += 1;
            if (video.readyState >= 2 && !video.paused) { clearInterval(iv); resetToScanning(); startScanning(); }
            else if (tries > 40) { clearInterval(iv); showCameraUnavailable("Camera unavailable.", "The camera started but video could not be displayed. Please reload or check the camera."); }
          }, 100);
        }
        if (video.readyState >= 2) waitForVideo(); else video.addEventListener("loadeddata", waitForVideo, { once: true });
      })
      .catch(function (err) {
        var msg = cameraErrorMessage(err);
        if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) showCameraBlocked(msg);
        else showCameraUnavailable("Camera unavailable.", msg);
      });
  }
  function cameraErrorMessage(err) {
    if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) return "Please allow camera permission in the browser, then reload this page.";
    if (err && (err.name === "NotFoundError" || err.name === "DevicesNotFoundError")) return "No camera was found. Connect a camera and reload this page.";
    if (err && (err.name === "NotReadableError" || err.name === "TrackStartError")) return "The camera is in use by another application. Close it and reload.";
    return "Please check the camera connection or contact the administrator.";
  }
  function showCameraBlocked(detail) {
    uiState = "error"; setSystemStatus("error", "Camera unavailable");
    showOverlay(false); showView("scan");
    setFeedback("tone-error", "Camera access is blocked.");
    setDialog("error", "error", "Camera unavailable.", detail + " Manual attendance is still available below.", "", [
      { label: "Try again", cls: "btn-primary", onClick: function () { startCamera(); } }
    ]);
  }
  function showCameraUnavailable(title, detail) {
    uiState = "error"; setSystemStatus("error", "Camera unavailable");
    showOverlay(false); showView("scan"); setFeedback("warn", "Camera unavailable.");
    setDialog("error", "error", title || "Camera unavailable.", detail || "Please check the camera connection or contact the administrator.", "", [
      { label: "Try again", cls: "btn-primary", onClick: function () { startCamera(); } }
    ]);
  }

  function startScanning() {
    if (scanTimer) return;
    scanTimer = setInterval(tickScan, SCAN_INTERVAL_MS);
  }
  function captureBlob() {
    if (!stream || !video.videoWidth || !video.videoHeight) return Promise.resolve(null);
    var width = Math.min(video.videoWidth, 640);
    var height = Math.round(video.videoHeight * (width / video.videoWidth));
    canvas.width = width; canvas.height = height;
    canvas.getContext("2d").drawImage(video, 0, 0, width, height);
    return new Promise(function (resolve) { canvas.toBlob(function (b) { resolve(b); }, "image/jpeg", 0.85); });
  }
  function postForm(url, form) {
    return fetch(url, { method: "POST", headers: { "X-Kiosk-ID": KIOSK_ID }, body: form })
      .then(function (res) {
        if (res.status === 503) return { __svc: true };
        return res.json().catch(function () { return null; });
      });
  }
  function postJson(url, body) {
    return fetch(url, { method: "POST", headers: { "X-Kiosk-ID": KIOSK_ID, "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (res) {
        if (res.status === 503) return { __svc: true, __status: 503 };
        return res.json().then(function (j) { return { __status: res.status, data: j }; }).catch(function () { return { __status: res.status, data: null }; });
      });
  }

  function tickScan() {
    if (!scanningEnabled()) return;
    if (challenge && challenge.firstBlob && (nowMs() - challenge.startedAt) < CHALLENGE_HOLD_MS) return;
    inFlight = true;
    captureBlob().then(function (blob) {
      if (!blob) return null;
      if (uiState !== "scanning") return null;
      if (challenge && challenge.firstBlob) {
        var f2 = new FormData();
        f2.append("image", challenge.firstBlob, "frame1.jpg");
        f2.append("challenge", challenge.dir);
        f2.append("challenge_image", blob, "frame2.jpg");
        var dir = challenge.dir;
        challenge = null;
        faceGuide.className = "face-guide detected"; scanLine.className = "scan-line active";
        setFeedback("accent", "Verifying your identity...");
        return postForm("/api/scan", f2).then(function (p) { return { payload: p, challenged: dir }; });
      }
      var f = new FormData(); f.append("image", blob, "frame.jpg");
      return postForm("/api/scan", f).then(function (p) {
        if (p && p.__svc) return { payload: p, challenged: null };
        if (p && (p.status === "too_small" || p.status === "blurry")) {
          faceGuide.className = "face-guide detected"; scanLine.className = "scan-line active";
          setFeedback("accent", "Face detected. Hold still.");
        }
        return { payload: p, challenged: null };
      });
    }).then(function (res) {
      if (!res || uiState !== "scanning") return;
      var p = res.payload;
      if (!p || p.__svc) { handleBackendUnavailable(); return; }
      handleScanPayload(p, res.challenged);
    }).catch(function () { if (uiState === "scanning") handleBackendUnavailable(); })
      .then(function () { inFlight = false; }, function () { inFlight = false; });
  }

  function handleBackendUnavailable() {
    if (uiState !== "scanning") return;
    uiState = "error"; setSystemStatus("error", "Service unavailable");
    scanLine.className = "scan-line"; faceGuide.className = "face-guide";
    setDialog("error", "error", "Attendance service temporarily unavailable.", "Please contact reception for assistance.", "", [
      { label: "Try again", cls: "btn-primary", onClick: function () { resetToScanning(); } }
    ]);
  }

  function handleScanPayload(p, challenged) {
    if (!p) return;
    var st = p.status;
    if (st === "challenge_required" && p.challenge) {
      challenge = { dir: p.challenge, firstBlob: null, startedAt: 0 };
      faceGuide.className = "face-guide detected"; scanLine.className = "scan-line";
      captureBlob().then(function (b) { if (challenge && challenge.dir === p.challenge) { challenge.firstBlob = b; challenge.startedAt = nowMs(); } });
      var dirText = p.challenge === "left" ? "Turn your head LEFT" : "Turn your head RIGHT";
      setFeedback("accent", dirText + ", then face forward.");
      return;
    }
    if (st === "ok" && p.token) {
      failCount = 0; challenge = null;
      faceGuide.className = "face-guide verified"; scanLine.className = "scan-line";
      handleRecognised(p);
      return;
    }
    if (st === "spoof") {
      failCount += 1;
      setFeedback("warn", "We couldn't recognise you. Please try again.");
      faceGuide.className = "face-guide failed"; scanLine.className = "scan-line";
      if (failCount >= FAIL_REDIRECT_AFTER) showUnrecognised("We couldn't recognise you.", "Please try again or contact reception. Manual attendance is available below.");
      return;
    }
    scanLine.className = "scan-line";
    if (st === "no_face") {
      faceGuide.className = "face-guide waiting";
      setFeedback("neutral", "Position your face inside the guide.");
      return;
    }
    if (st === "multiple_faces") {
      faceGuide.className = "face-guide failed";
      setFeedback("warn", "Please ensure only one person is in front of the camera.");
      return;
    }
    if (st === "too_small" || st === "blurry" || st === "invalid_image") {
      faceGuide.className = "face-guide detected";
      setFeedback("accent", st === "blurry" ? "Hold still and face the camera." : "Position your face inside the guide.");
      return;
    }
    if (st === "not_recognised" || st === "ambiguous") {
      failCount += 1;
      faceGuide.className = "face-guide failed";
      setFeedback("warn", "We couldn't recognise you. Please try again.");
      if (failCount >= FAIL_REDIRECT_AFTER) {
        showUnrecognised(st === "ambiguous" ? "Unable to verify your identity." : "We couldn't recognise you.",
          "Please try again or contact reception. Manual attendance is available below.");
      }
      return;
    }
    faceGuide.className = "face-guide waiting";
    setFeedback("neutral", "Position your face inside the guide.");
  }

  function showUnrecognised(title, desc) {
    uiState = "dialog"; setSystemStatus("warn", "Camera ready");
    var actions = [
      { label: "Try again", cls: "btn-primary", onClick: function () { failCount = 0; resetToScanning(); } }
    ];
    setDialog("warn", "warn", title, desc, "", actions);
    setTimeout(function () { if (uiState === "dialog" && dialogTitle.textContent === title) { failCount = 0; resetToScanning(); } }, FAIL_REDIRECT_MS);
  }

  function handleRecognised(p) {
    uiState = "processing"; inFlight = false;
    faceGuide.className = "face-guide detected"; scanLine.className = "scan-line";
    var allowed = p.allowed_actions || [];
    var msg = p.message || "";
    lastShift = p.shift_summary || null; lastName = p.name || null;
    session = { token: p.token, name: p.name || "", shift_summary: p.shift_summary || "Not assigned",
      message: msg, allowed_actions: allowed, expiresAt: nowMs() + ((p.token_expires_in || 20) * 1000) };
    lastToken = p.token;

    if (msg === "before_window" || msg === "no_shift_request_approval") {
      showBlocked(msg, p);
      return;
    }
    if (allowed.indexOf("out") !== -1 && allowed.indexOf("in") === -1 && msg === "clock_out_early_confirm") {
      showEarlyConfirm();
      return;
    }
    if (allowed.indexOf("in") !== -1 && allowed.indexOf("out") === -1) {
      doClock("in", false);
      return;
    }
    if (allowed.indexOf("out") !== -1 && allowed.indexOf("in") === -1) {
      if (msg === "clock_out_early_confirm") { showEarlyConfirm(); return; }
      doClock("out", false);
      return;
    }
    if (allowed.indexOf("out") !== -1 && allowed.indexOf("in") !== -1) {
      // both allowed (unexpected) - prefer out to close open session
      doClock("out", false);
      return;
    }
    showBlocked(msg, p);
  }

  function tokenValid() { return session && session.token && nowMs() < session.expiresAt; }
  function expireSessionNote() {
    dialogError.textContent = "The confirmation time ran out. Please scan again.";
    setTimeout(function () { resetToScanning(); }, 1200);
  }

  function showEarlyConfirm() {
    var remain = session ? Math.max(1, Math.ceil((session.expiresAt - nowMs()) / 1000)) : 0;
    setDialog("warn", "warn", "Clock out early?", "Your assigned shift has not ended yet. Are you sure you want to clock out now?",
      remain > 0 ? ("Confirm within " + remain + " seconds.") : "", [
        { label: "Cancel", cls: "btn-neutral", onClick: function () { resetToScanning(); } },
        { label: "Confirm clock out", cls: "btn-primary", onClick: function () {
            if (!tokenValid()) { expireSessionNote(); return; }
            doClock("out", true);
          } }
      ]);
  }

  function showBlocked(msg, p) {
    var title = "Attendance not available";
    var desc = "Please try again later or contact reception.";
    var reason = "out_of_window";
    if (msg === "no_shift_request_approval") { title = "No shift assigned"; desc = "There is no shift assigned for you today. You can request approval or contact reception."; reason = "no_shift"; }
    else if (msg === "before_window") { title = "Too early to clock in"; desc = "Your clock-in window has not opened yet. Please try again later or request approval."; reason = "out_of_window"; }
    setDialog("info", "info", title, desc, "", [
      { label: "Back to scan", cls: "btn-neutral", onClick: function () { resetToScanning(); } },
      { label: "Request approval", cls: "btn-primary", onClick: function () { requestApproval(reason); } }
    ]);
  }

  function requestApproval(reason) {
    if (!session || !session.token) { resetToScanning(); return; }
    var action = (session.allowed_actions || []).indexOf("out") !== -1 ? "out" : "in";
    dialogError.textContent = "Sending request...";
    postJson("/api/clock/request", { token: session.token, action: action, reason_code: reason || "out_of_window" })
      .then(function (res) {
        if (res.__svc) { handleBackendUnavailable(); return; }
        if (res.__status >= 200 && res.__status < 300) {
          showResultNeutral("Approval requested", session.name || "", "A manager will review your request.", "If you need help, please contact reception.");
        } else {
          var d = (res.data && res.data.detail) || {};
          if (d.reason === "expired") { expireSessionNote(); return; }
          dialogError.textContent = "Could not send the request. Please contact reception.";
        }
      }).catch(function () { dialogError.textContent = "Could not send the request. Please contact reception."; });
  }

  function doClock(action, confirm) {
    if (!session || !session.token) { resetToScanning(); return; }
    if (!tokenValid()) { setDialog("warn","warn","Session expired","The confirmation time ran out. Please scan again.","",[{label:"OK",cls:"btn-primary",onClick:function(){resetToScanning();}}]); return; }
    uiState = "processing";
    dialogError.textContent = "";
    setFeedback("accent", "Recording attendance...");
    postJson("/api/clock", { token: session.token, action: action, confirm: !!confirm })
      .then(function (res) {
        if (res.__svc) { handleBackendUnavailable(); return; }
        var st = res.__status, data = res.data || {};
        if (st === 200 && data.attendance) {
          lastAction = action; lastEventId = data.attendance.id; showSuccess(action, data.attendance); return;
        }
        var detail = data.detail || {};
        if (st === 409 && detail.status === "confirm_required" && action === "out") { showEarlyConfirm(); return; }
        if (st === 400 && (detail.reason === "expired" || detail.reason === "used")) {
          setDialog("warn","warn","Session expired","The confirmation time ran out. Please scan again.","",[{label:"Try again",cls:"btn-primary",onClick:function(){resetToScanning();}}]); return;
        }
        setDialog("error","error","Attendance could not be recorded", (detail.message || "Please try again or contact reception."), "", [
          { label: "Try again", cls: "btn-primary", onClick: function () { resetToScanning(); } }
        ]);
      }).catch(function () { handleBackendUnavailable(); });
  }

  function showSuccess(action, att) {
    clearTimers(); uiState = "success"; session = null; challenge = null;
    setSystemStatus("ok", "Camera ready");
    var isOut = action === "out";
    successIndicator.className = "success-indicator ok";
    successIndicator.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
    successHeading.textContent = "Attendance Verified";
    successName.textContent = lastName || "";
    successVerify.textContent = "Your identity has been successfully verified.";
    detailAction.innerHTML = '<span class="pill ' + (isOut ? "out" : "in") + '">' + (isOut ? "Clock Out" : "Clock In") + '</span>';
    detailTime.textContent = fmtTime(att && att.timestamp ? att.timestamp : new Date());
    detailShift.textContent = lastShift || "Not assigned";
    finalMessage.className = "final-message ok";
    finalMessage.textContent = isOut ? "Your departure has been recorded. Have a good day!" : "You're all set. Have a productive day!";
    resultNote.textContent = "";
    startUndo();
    showView("success");
    scheduleReset(SUCCESS_RESET_MS);
  }

  function showResultNeutral(title, name, desc, note) {
    clearTimers(); uiState = "success"; session = null; challenge = null;
    setSystemStatus("ok", "Camera ready");
    successIndicator.className = "success-indicator neutral";
    successIndicator.innerHTML = svg("info");
    successHeading.textContent = title;
    successName.textContent = name || "";
    successVerify.textContent = desc || "";
    detailAction.innerHTML = '<span class="detail-value" style="color:var(--text-secondary)">—</span>';
    detailTime.textContent = fmtTime(new Date());
    detailShift.textContent = lastShift || "Not assigned";
    finalMessage.className = "final-message neutral";
    finalMessage.textContent = note || "Please contact reception if you need assistance.";
    btnUndo.hidden = true; resultNote.textContent = "";
    showView("success");
    scheduleReset(SUCCESS_RESET_MS);
  }

  function startUndo() {
    if (!lastToken && !lastEventId) { btnUndo.hidden = true; return; }
    var deadline = nowMs() + UNDO_MS;
    btnUndo.hidden = false;
    function tick() {
      var left = Math.max(0, Math.ceil((deadline - nowMs()) / 1000));
      undoSecs.textContent = String(left);
      if (left <= 0) { if (undoTimer) clearInterval(undoTimer); undoTimer = null; btnUndo.hidden = true; }
    }
    tick();
    undoTimer = setInterval(tick, 250);
  }

  btnUndo.addEventListener("click", function () {
    if (!lastToken && !lastEventId) return;
    postJson("/api/clock/undo", lastEventId ? { event_id: lastEventId } : { token: lastToken })
      .then(function (res) {
        if (res.__status === 200) {
          if (undoTimer) { clearInterval(undoTimer); undoTimer = null; }
          btnUndo.hidden = true; if (resetTimer) clearTimeout(resetTimer);
          successHeading.textContent = "Attendance undone";
          successVerify.textContent = "The clock event was undone.";
          finalMessage.className = "final-message neutral";
          finalMessage.textContent = "You can scan again when ready.";
          resultNote.textContent = "";
          scheduleReset(2500);
        } else {
          var d = (res.data && res.data.detail) || {};
          btnUndo.hidden = true;
          if (d.status === "undo_window_expired" || res.__status === 409) resultNote.textContent = "Undo time has passed. Please contact reception if this needs correcting.";
          else resultNote.textContent = "Could not undo. Please contact reception.";
        }
      }).catch(function () { resultNote.textContent = "Could not undo. Please contact reception."; });
  });

  manualLink.addEventListener("click", function (e) { e.preventDefault(); manualHelp.hidden = !manualHelp.hidden; });
  window.addEventListener("beforeunload", function () { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); });

  startCamera();
})();
