/*
 * Pure kiosk state classification (no DOM, no network).
 *
 * Kept separate from signin.js so the lifecycle -> UI mapping can be unit
 * tested with the built-in Node test runner (no new dependencies).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SigninState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function scanState(payload) {
    payload = payload || {};
    var allowed = payload.allowed_actions || [];
    var message = payload.message || "";
    var kind = "blocked";
    if (message === "shift_already_completed") kind = "completed";
    else if (message === "reentry_approval_required") kind = "reentry";
    else if (message === "before_window") kind = "before_window";
    else if (message === "after_shift_end") kind = "after_shift_end";
    else if (message === "no_shift_request_approval") kind = "no_shift";
    else if (allowed.indexOf("out") !== -1 && allowed.indexOf("in") === -1) kind = "out";
    else if (allowed.indexOf("in") !== -1 && allowed.indexOf("out") === -1) kind = "in";
    return {
      kind: kind,
      allowed: allowed,
      message: message,
      state: payload.state || null
    };
  }

  function clockResult(status, data) {
    data = data || {};
    var detail = data.detail || {};
    // A success state requires BOTH HTTP 200 and a persisted attendance row.
    if (status === 200 && data.status === "ok" && data.attendance && data.attendance.id) {
      return {
        kind: "success",
        attendance: data.attendance,
        action: data.attendance.type || null
      };
    }
    if (status === 409 && detail.status === "confirm_required") {
      return { kind: "confirm", message: detail.message || "" };
    }
    if (status === 409 && detail.status === "blocked") {
      return {
        kind: "blocked",
        code: detail.message || "no_shift_request_approval",
        reason: detail.reason_code || null
      };
    }
    if (status === 400 && (detail.reason === "expired" || detail.reason === "used")) {
      return { kind: "stale", reason: detail.reason };
    }
    if (status === 0 || status == null) return { kind: "network" };
    return { kind: "error", message: detail.message || null };
  }

  function blockedCopy(code, context) {
    context = context || {};
    var ended = context.shiftEnd ? ("This shift ended at " + context.shiftEnd + ". ") : "This shift has ended. ";
    var next = context.nextShift ? ("Your next shift starts " + context.nextShift + ". ") : "";
    if (code === "no_shift_request_approval") {
      return {
        title: "No shift assigned",
        desc: "There is no shift assigned for you today. You can request approval or contact reception.",
        reason: "no_shift",
        canRequest: true
      };
    }
    if (code === "before_window") {
      return {
        title: "Too early to clock in",
        desc: "Your clock-in window has not opened yet. Please try again later or request approval.",
        reason: "out_of_window",
        canRequest: true
      };
    }
    if (code === "after_shift_end") {
      return {
        title: "Shift ended",
        desc: ended + next + "You can request manager approval to clock in now, or contact reception.",
        reason: "out_of_window",
        canRequest: true
      };
    }
    if (code === "shift_already_completed") {
      return {
        title: "Shift already completed",
        desc: "Your attendance for this shift is already complete. " + ended + next + "Nothing has been recorded now. You can request manager approval to return, or contact reception.",
        reason: "reentry",
        canRequest: true
      };
    }
    if (code === "reentry_approval_required") {
      return {
        title: "Re-entry needs approval",
        desc: "Your shift is already complete. " + next + "A manager must approve another clock-in for the same shift.",
        reason: "reentry",
        canRequest: true
      };
    }
    if (code === "no_open_session") {
      return {
        title: "No open clock-in",
        desc: "You are not currently clocked in, so there is nothing to clock out. Please scan again or contact reception.",
        reason: "out_of_window",
        canRequest: false
      };
    }
    if (code === "session_already_open") {
      return {
        title: "Already clocked in",
        desc: "You already have an open session. Please clock out first, then scan again.",
        reason: "reentry",
        canRequest: false
      };
    }
    return {
      title: "Attendance not available",
      desc: "Please try again later or contact reception.",
      reason: "out_of_window",
      canRequest: false
    };
  }

  return {
    scanState: scanState,
    clockResult: clockResult,
    blockedCopy: blockedCopy
  };
});
