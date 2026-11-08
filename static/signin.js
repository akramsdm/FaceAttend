(function () {
  "use strict";

  var INTERVAL_MS = 1500;
  var RESULT_MS = 4000;
  var UNDO_MS = 10000;
  var TOKEN_MS = 20000;
  var KIOSK_ID = "entrance-main";
  var CHALLENGE_DELAY_MS = 900;
  var RECEPTION = "please contact reception";

  var video = document.getElementById("video");
  var canvas = document.getElementById("capture-canvas");
  var placeholder = document.getElementById("camera-placeholder");
  var statusEl = document.getElementById("status");
  var mainEl = document.getElementById("status-main");
  var subEl = document.getElementById("status-sub");
  var scanScreen = document.getElementById("scan-screen");
  var confirmScreen = document.getElementById("confirm-screen");
  var resultScreen = document.getElementById("result-screen");
  var confirmName = document.getElementById("confirm-name");
  var confirmShift = document.getElementById("confirm-shift");
  var confirmTime = document.getElementById("confirm-time");
  var confirmMessage = document.getElementById("confirm-message");
  var countdownEl = document.getElementById("countdown");
  var btnIn = document.getElementById("btn-in");
  var btnOut = document.getElementById("btn-out");
  var btnReason = document.getElementById("btn-reason");
  var earlyBox = document.getElementById("early-confirm");
  var earlyYes = document.getElementById("early-yes");
  var earlyNo = document.getElementById("early-no");
  var blockedBox = document.getElementById("blocked-box");
  var btnRequest = document.getElementById("btn-request");
  var confirmError = document.getElementById("confirm-error");
  var resultMain = document.getElementById("result-main");
  var resultSub = document.getElementById("result-sub");
  var btnUndo = document.getElementById("btn-undo");
  var undoSecs = document.getElementById("undo-secs");
  var resultNote = document.getElementById("result-note");
  var manualLink = document.getElementById("manual-link");
  var manualHelp = document.getElementById("manual-help");

  var stream = null, timerId = null, inFlight = false, showingResult = false;
  var challengeDir = null, challengeFirst = null;
  var current = null; // {token, name, shift_summary, allowed_actions, message, token_expires_in}
  var countdownId = null, tokenDeadline = 0, lastEventId = null, lastToken = null;
  var undoTimerId = null, undoDeadline = 0, resultResetId = null;
  var lastAction = null, lastRequestReason = null;

  function setStatus(kind, main, sub) {
    statusEl.className = "status status-" + kind;
    mainEl.textContent = main || "";
    subEl.textContent = sub || "";
  }
  function fmtTime(d) {
    try { return new Date(d || Date.now()).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}); }
    catch(e){ return ""; }
  }
  function showScreen(which) {
    scanScreen.hidden = which !== "scan";
    confirmScreen.hidden = which !== "confirm";
    resultScreen.hidden = which !== "result";
  }
  function clearTimers() {
    window.clearInterval(countdownId); countdownId = null;
    window.clearInterval(undoTimerId); undoTimerId = null;
    window.clearTimeout(resultResetId); resultResetId = null;
  }
  function stopScanning() { /* keep interval but gate on state */ }
  function scanningAllowed() { return !!stream && !inFlight && !showingResult && !current; }

  function resetAll() {
    clearTimers();
    current = null; lastEventId = null; lastToken = null; lastAction = null; lastRequestReason = null;
    challengeDir = null; challengeFirst = null; showingResult = false;
    earlyBox.hidden = true; blockedBox.hidden = true; confirmError.textContent = "";
    btnUndo.hidden = true; resultNote.textContent = "";
    showScreen("scan");
    setStatus("waiting", "Waiting for a face", "Position your face inside the guide.");
  }

  function scheduleResultReset() {
    window.clearTimeout(resultResetId);
    resultResetId = window.setTimeout(resetAll, RESULT_MS);
  }

  function humanMessage(code) {
    var m = {
      no_shift_request_approval: "No shift assigned for you today.",
      before_window: "It is too early to clock in.",
      window_open_not_clocked_in: "You can clock in now.",
      late_arrival: "You are late; clocking in will be recorded as late.",
      clock_out_early_confirm: "Your shift has not ended yet. Confirm if you want to clock out early.",
      recorded: "You can clock out now."
    };
    return m[code] || "";
  }

  function reasonForRequest(code, action) {
    if (code === "no_shift_request_approval") return "no_shift";
    if (code === "before_window") return "out_of_window";
    if (code === "clock_out_early_confirm") return "early_out";
    return "out_of_window";
  }

  function updateCountdown() {
    if (!current) return;
    var left = Math.max(0, Math.ceil((tokenDeadline - Date.now()) / 1000));
    countdownEl.textContent = "Time remaining: " + left + "s";
    if (left <= 0) {
      window.clearInterval(countdownId); countdownId = null;
      setStatus("error", "Session expired", "The confirmation time ran out. Please scan again.");
      resetAll();
    }
  }

  function enterConfirm(payload) {
    current = payload;
    lastToken = payload.token;
    tokenDeadline = Date.now() + ((payload.token_expires_in || 20) * 1000);
    confirmName.textContent = payload.name || "Recognised";
    confirmShift.textContent = payload.shift_summary ? ("Shift: " + payload.shift_summary) : "";
    confirmTime.textContent = "Time: " + fmtTime(new Date());
    var msg = humanMessage(payload.message);
    confirmMessage.textContent = msg || "";
    confirmError.textContent = "";
    earlyBox.hidden = true; blockedBox.hidden = true;
    var allowed = payload.allowed_actions || [];
    btnIn.disabled = allowed.indexOf("in") === -1;
    btnOut.disabled = allowed.indexOf("out") === -1;
    var reasons = [];
    if (btnIn.disabled && btnOut.disabled) reasons.push(msg || "No actions are available right now.");
    else {
      if (btnIn.disabled) reasons.push("Clock in is not available: " + (msg || "not allowed now."));
      if (btnOut.disabled && allowed.indexOf("in") !== -1) reasons.push("Clock out is not available until you are clocked in.");
    }
    btnReason.textContent = reasons.join(" ");
    if (btnIn.disabled && btnOut.disabled) {
      blockedBox.hidden = false;
      lastRequestReason = reasonForRequest(payload.message, "in");
    }
    showScreen("confirm");
    updateCountdown();
    countdownId = window.setInterval(updateCountdown, 250);
  }

  function handleScanPayload(payload) {
    if (!payload) return;
    var status = payload.status;
    if (status === "challenge_required") {
      challengeDir = payload.challenge === "left" ? "left" : "right";
      setStatus("waiting", challengeDir === "left" ? "Turn your head LEFT" : "Turn your head RIGHT", "Hold briefly, then face forward.");
      return;
    }
    if (status === "ok" && payload.token) {
      enterConfirm(payload);
      return;
    }
    if (showingResult || current) return;
    var messages = {
      no_face: ["No face detected", "Position your face inside the guide."],
      multiple_faces: ["More than one face detected", "Only one person should face the camera."],
      too_small: ["Face is too small", "Move closer to the camera."],
      blurry: ["Image is blurry", "Hold still and face the camera."],
      not_recognised: ["Not recognised", "Try again, or " + RECEPTION + "."],
      ambiguous: ["Not recognised", "Try again, or " + RECEPTION + "."],
      invalid_image: ["Could not read image", "Please try again."],
      spoof: ["Liveness check failed", "Turn your head as asked, or " + RECEPTION + "."]
    };
    var msg = messages[status] || ["Not recognised", "Try again, or " + RECEPTION + "."];
    setStatus("error", msg[0], msg[1]);
  }

  function handleServiceError() {
    if (showingResult || current) return;
    setStatus("error", "Service unavailable", "The sign-in service is not available. Please " + RECEPTION + ".");
  }

  function captureBlob() {
    if (!stream || !video.videoWidth || !video.videoHeight) return Promise.resolve(null);
    var width = Math.min(video.videoWidth, 640);
    var height = Math.round(video.videoHeight * (width / video.videoWidth));
    canvas.width = width; canvas.height = height;
    canvas.getContext("2d").drawImage(video, 0, 0, width, height);
    return new Promise(function (resolve) { canvas.toBlob(function (blob) { resolve(blob); }, "image/jpeg", 0.85); });
  }

  function postForm(url, form) {
    return fetch(url, { method: "POST", headers: { "X-Kiosk-ID": KIOSK_ID }, body: form })
      .then(function (res) {
        if (res.status === 503) { handleServiceError(); return { __svc: true }; }
        return res.json().catch(function(){ return null; });
      });
  }

  function postJson(url, body) {
    return fetch(url, { method: "POST", headers: { "X-Kiosk-ID": KIOSK_ID, "Content-Type": "application/json" },
      body: JSON.stringify(body) })
      .then(function (res) {
        if (res.status === 503) { handleServiceError(); return { __svc: true, __status: 503 }; }
        return res.json().then(function (j) { return { __status: res.status, data: j }; }).catch(function(){ return { __status: res.status, data: null }; });
      });
  }

  function scan() {
    if (!scanningAllowed()) return;
    inFlight = true;
    captureBlob().then(function (blob) {
      if (!blob) return null;
      if (challengeDir && challengeFirst) {
        var f2 = new FormData();
        f2.append("image", challengeFirst, "frame1.jpg");
        f2.append("challenge", challengeDir);
        f2.append("challenge_image", blob, "frame2.jpg");
        challengeDir = null; challengeFirst = null;
        return postForm("/api/scan", f2);
      }
      var f = new FormData(); f.append("image", blob, "frame.jpg");
      return postForm("/api/scan", f).then(function (payload) {
        if (payload && payload.__svc) return payload;
        if (payload && payload.status === "challenge_required" && payload.challenge) {
          challengeDir = payload.challenge; challengeFirst = blob;
          setStatus("waiting", challengeDir === "left" ? "Turn your head LEFT" : "Turn your head RIGHT", "Hold briefly, then face forward.");
          return null;
        }
        return payload;
      });
    }).then(function (payload) {
      if (!payload || payload.__svc) return;
      handleScanPayload(payload);
    }).catch(function(){ handleServiceError(); })
      .finally(function(){ inFlight = false; });
  }

  function showResultOk(action, att) {
    clearTimers();
    showingResult = true; current = null;
    lastEventId = att && att.id;
    var label = action === "out" ? "Clocked out" : "Clocked in";
    resultMain.textContent = label;
    resultSub.textContent = "Recorded at " + fmtTime(att && att.timestamp ? att.timestamp : new Date());
    resultNote.textContent = "";
    btnUndo.hidden = false;
    undoDeadline = Date.now() + UNDO_MS;
    function tickUndo() {
      var left = Math.max(0, Math.ceil((undoDeadline - Date.now()) / 1000));
      undoSecs.textContent = String(left);
      if (left <= 0) { window.clearInterval(undoTimerId); undoTimerId = null; btnUndo.hidden = true; }
    }
    tickUndo();
    undoTimerId = window.setInterval(tickUndo, 250);
    showScreen("result");
    scheduleResultReset();
  }

  function doClock(action, confirm) {
    if (!current || !current.token) return;
    confirmError.textContent = "";
    btnIn.disabled = true; btnOut.disabled = true;
    postJson("/api/clock", { token: current.token, action: action, confirm: !!confirm })
      .then(function (res) {
        if (res.__svc) { resetAll(); return; }
        var status = res.__status, data = res.data || {};
        if (status === 200 && data.attendance) { lastAction = action; showResultOk(action, data.attendance); return; }
        var detail = data.detail || {};
        if (status === 409 && detail.status === "confirm_required" && action === "out") {
          earlyBox.hidden = false; btnIn.disabled = (current.allowed_actions||[]).indexOf("in")===-1; btnOut.disabled = false; return;
        }
        if (status === 400 && detail.reason === "expired") {
          setStatus("error", "Session expired", "The confirmation time ran out. Please scan again.");
          resetAll(); return;
        }
        confirmError.textContent = (detail.message || detail.reason || "The action could not be completed.") + " Please try again or " + RECEPTION + ".";
        if (current) { var allowed=current.allowed_actions||[]; btnIn.disabled=allowed.indexOf("in")===-1; btnOut.disabled=allowed.indexOf("out")===-1; }
      }).catch(function(){
        confirmError.textContent = "Service unavailable. Please try again or " + RECEPTION + ".";
        if (current) { btnIn.disabled=false; btnOut.disabled=false; }
      });
  }

  btnIn.addEventListener("click", function(){ doClock("in", false); });
  btnOut.addEventListener("click", function(){
    if (!current) return;
    if (current.message === "clock_out_early_confirm") { earlyBox.hidden = false; return; }
    doClock("out", false);
  });
  earlyYes.addEventListener("click", function(){ earlyBox.hidden = true; doClock("out", true); });
  earlyNo.addEventListener("click", function(){ earlyBox.hidden = true; });

  btnRequest.addEventListener("click", function(){
    if (!current) return;
    var reason = lastRequestReason || reasonForRequest(current.message, "in");
    postJson("/api/clock/request", { token: current.token, action: (current.allowed_actions||[]).indexOf("out")!==-1?"out":"in", reason_code: reason })
      .then(function(res){
        if (res.__svc) { resetAll(); return; }
        clearTimers(); showingResult = true; current = null;
        resultMain.textContent = "Approval requested";
        resultSub.textContent = "A manager will review your request.";
        resultNote.textContent = "If you need help, " + RECEPTION + ".";
        btnUndo.hidden = true;
        showScreen("result"); scheduleResultReset();
      }).catch(function(){ confirmError.textContent = "Could not send request. Please " + RECEPTION + "."; });
  });

  btnUndo.addEventListener("click", function(){
    if (!lastToken && !lastEventId) return;
    postJson("/api/clock/undo", lastEventId ? { event_id: lastEventId } : { token: lastToken })
      .then(function(res){
        if (res.__status === 200) {
          window.clearInterval(undoTimerId); btnUndo.hidden = true; window.clearTimeout(resultResetId);
          resultMain.textContent = "Undone";
          resultSub.textContent = "The clock event was undone.";
          resultNote.textContent = "";
          scheduleResultReset();
        } else {
          var d = (res.data && res.data.detail) || {};
          if (d.status === "undo_window_expired" || res.__status === 409) {
            btnUndo.hidden = true; resultNote.textContent = "Undo time has passed. Please " + RECEPTION + " if this needs correcting.";
          } else { resultNote.textContent = "Could not undo. Please " + RECEPTION + "."; }
        }
      }).catch(function(){ resultNote.textContent = "Could not undo. Please " + RECEPTION + "."; });
  });

  function startScanning() { if (timerId) return; timerId = window.setInterval(scan, INTERVAL_MS); }
  function cameraErrorMessage(err) {
    if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) return "Camera access is blocked. Allow camera permission in the browser.";
    if (err && (err.name === "NotFoundError" || err.name === "DevicesNotFoundError")) return "No camera was found. Connect a camera and reload this page.";
    if (err && (err.name === "NotReadableError" || err.name === "TrackStartError")) return "The camera is in use by another application.";
    return "The camera could not be started. Use localhost or HTTPS.";
  }
  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      placeholder.textContent = "Camera access is not available in this browser.";
      setStatus("error", "Camera unavailable", "Use a current browser on localhost or HTTPS, or " + RECEPTION + "."); return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      .then(function (mediaStream) { stream = mediaStream; video.srcObject = mediaStream; return video.play().catch(function () {}); })
      .then(function () { placeholder.hidden = true; resetAll(); startScanning(); })
      .catch(function (err) { placeholder.textContent = "Camera not available"; setStatus("error", "Camera blocked", cameraErrorMessage(err) + " " + RECEPTION + " if needed."); });
  }
  manualLink.addEventListener("click", function (event) { event.preventDefault(); manualHelp.hidden = !manualHelp.hidden; });
  window.addEventListener("beforeunload", function () { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); });
  startCamera();
})();
