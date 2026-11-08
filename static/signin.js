(function () {
  "use strict";

  var INTERVAL_MS = 1500;
  var RESULT_MS = 4000;
  var KIOSK_ID = "entrance-main";

  var video = document.getElementById("video");
  var canvas = document.getElementById("capture-canvas");
  var placeholder = document.getElementById("camera-placeholder");
  var statusEl = document.getElementById("status");
  var mainEl = document.getElementById("status-main");
  var subEl = document.getElementById("status-sub");
  var manualLink = document.getElementById("manual-link");
  var manualHelp = document.getElementById("manual-help");

  var stream = null;
  var timerId = null;
  var resetId = null;
  var inFlight = false;
  var showingResult = false;

  function setStatus(kind, main, sub) {
    statusEl.className = "status status-" + kind;
    mainEl.textContent = main || "";
    subEl.textContent = sub || "";
  }

  function formatTime(value) {
    if (!value) return "";
    try {
      return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (e) {
      return String(value);
    }
  }

  function actionLabel(action) {
    return action === "out" ? "Signed out" : "Signed in";
  }

  function showWaiting() {
    showingResult = false;
    setStatus("waiting", "Waiting for a face", "Position your face inside the guide.");
  }

  function scheduleReset() {
    window.clearTimeout(resetId);
    resetId = window.setTimeout(showWaiting, RESULT_MS);
  }

  function handlePayload(payload) {
    var status = payload && payload.status;
    if (status === "ok") {
      showingResult = true;
      setStatus("ok", payload.name || "Recognised", actionLabel(payload.action) + " at " + formatTime(payload.time));
      scheduleReset();
      return;
    }
    if (status === "already_recorded") {
      showingResult = true;
      setStatus("warn", payload.name || "Already recorded", actionLabel(payload.action) + " already recorded at " + formatTime(payload.time));
      scheduleReset();
      return;
    }

    if (showingResult) return; // keep a successful/warn result visible until reset

    var messages = {
      no_face: ["No face detected", "Position your face inside the guide."],
      multiple_faces: ["More than one face detected", "Only one person should face the camera."],
      too_small: ["Face is too small", "Move closer to the camera."],
      blurry: ["Image is blurry", "Hold still and face the camera."],
      not_recognised: ["Not recognised", "Try again, or sign in manually at reception."],
      ambiguous: ["Not recognised", "Try again, or sign in manually at reception."],
      invalid_image: ["Could not read image", "Please try again."]
    };
    var msg = messages[status] || ["Not recognised", "Try again, or sign in manually at reception."];
    setStatus("error", msg[0], msg[1]);
  }

  function handleServiceError() {
    if (showingResult) return;
    setStatus("error", "Service unavailable", "The sign-in service is not available. Please sign in manually at reception.");
  }

  function captureBlob() {
    if (!stream || !video.videoWidth || !video.videoHeight) return Promise.resolve(null);
    var width = Math.min(video.videoWidth, 640);
    var height = Math.round(video.videoHeight * (width / video.videoWidth));
    canvas.width = width;
    canvas.height = height;
    var ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, width, height);
    return new Promise(function (resolve) {
      canvas.toBlob(function (blob) { resolve(blob); }, "image/jpeg", 0.85);
    });
  }

  function scan() {
    if (inFlight || showingResult || !stream) return;
    inFlight = true;
    captureBlob().then(function (blob) {
      if (!blob) { inFlight = false; return null; }
      var form = new FormData();
      form.append("image", blob, "frame.jpg");
      return fetch("/api/signin", {
        method: "POST",
        headers: { "X-Kiosk-ID": KIOSK_ID },
        body: form
      });
    }).then(function (response) {
      if (!response) return null;
      if (response.status === 503) { handleServiceError(); return null; }
      return response.json().catch(function () { return null; });
    }).then(function (payload) {
      if (payload) handlePayload(payload);
    }).catch(function () {
      handleServiceError();
    }).finally(function () {
      inFlight = false;
    });
  }

  function startScanning() {
    if (timerId) return;
    timerId = window.setInterval(scan, INTERVAL_MS);
  }

  function cameraErrorMessage(err) {
    if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) {
      return "Camera access is blocked. Allow camera permission in the browser.";
    }
    if (err && (err.name === "NotFoundError" || err.name === "DevicesNotFoundError")) {
      return "No camera was found. Connect a camera and reload this page.";
    }
    if (err && (err.name === "NotReadableError" || err.name === "TrackStartError")) {
      return "The camera is in use by another application.";
    }
    return "The camera could not be started. Use localhost or HTTPS.";
  }

  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      placeholder.textContent = "Camera access is not available in this browser.";
      setStatus("error", "Camera unavailable", "Use a current browser on localhost or HTTPS, or sign in manually at reception.");
      return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      .then(function (mediaStream) {
        stream = mediaStream;
        video.srcObject = mediaStream;
        return video.play().catch(function () {});
      })
      .then(function () {
        placeholder.hidden = true;
        showWaiting();
        startScanning();
      })
      .catch(function (err) {
        placeholder.textContent = "Camera not available";
        setStatus("error", "Camera blocked", cameraErrorMessage(err) + " Sign in manually at reception if needed.");
      });
  }

  manualLink.addEventListener("click", function (event) {
    event.preventDefault();
    manualHelp.hidden = !manualHelp.hidden;
    if (!manualHelp.hidden) manualHelp.focus && manualHelp.focus();
  });

  window.addEventListener("beforeunload", function () {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
  });

  startCamera();
})();
