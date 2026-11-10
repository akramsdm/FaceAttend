"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const SigninState = require("../../static/signin_state.js");

test("scanState distinguishes every lifecycle state", () => {
  assert.equal(SigninState.scanState({ message: "shift_already_completed" }).kind, "completed");
  assert.equal(SigninState.scanState({ message: "reentry_approval_required", allowed_actions: ["in"] }).kind, "reentry");
  assert.equal(SigninState.scanState({ message: "before_window", allowed_actions: [] }).kind, "before_window");
  assert.equal(SigninState.scanState({ message: "after_shift_end", allowed_actions: [] }).kind, "after_shift_end");
  assert.equal(SigninState.scanState({ message: "no_shift_request_approval", allowed_actions: [] }).kind, "no_shift");
  assert.equal(SigninState.scanState({ message: "late_arrival", allowed_actions: ["in"] }).kind, "in");
  assert.equal(SigninState.scanState({ message: "recorded", allowed_actions: ["out"] }).kind, "out");
  assert.equal(SigninState.scanState({ message: "clock_out_early_confirm", allowed_actions: ["out"] }).kind, "out");
  assert.equal(SigninState.scanState({ message: "", allowed_actions: [] }).kind, "blocked");
});

test("scanState keeps the server state and allowed actions", () => {
  const s = SigninState.scanState({ state: "open_session", allowed_actions: ["out"], message: "recorded" });
  assert.equal(s.state, "open_session");
  assert.deepEqual(s.allowed, ["out"]);
});

test("clockResult never reports success without a persisted attendance row", () => {
  assert.equal(SigninState.clockResult(200, { status: "ok" }).kind, "error");
  assert.equal(SigninState.clockResult(200, { status: "ok", attendance: {} }).kind, "error");
  assert.equal(SigninState.clockResult(200, { attendance: { id: 1 } }).kind, "error");
  const ok = SigninState.clockResult(200, {
    status: "ok",
    attendance: { id: 9, type: "out", timestamp: "2026-01-05T09:00:00Z" }
  });
  assert.equal(ok.kind, "success");
  assert.equal(ok.action, "out");
  assert.equal(ok.attendance.id, 9);
});

test("clockResult maps confirmation, blocked, stale and network responses", () => {
  assert.equal(
    SigninState.clockResult(409, { detail: { status: "confirm_required", message: "clock_out_early_confirm" } }).kind,
    "confirm"
  );
  const blocked = SigninState.clockResult(409, {
    detail: { status: "blocked", message: "no_open_session", reason_code: "out_of_window" }
  });
  assert.equal(blocked.kind, "blocked");
  assert.equal(blocked.code, "no_open_session");
  assert.equal(blocked.reason, "out_of_window");
  assert.equal(SigninState.clockResult(400, { detail: { reason: "expired" } }).kind, "stale");
  assert.equal(SigninState.clockResult(400, { detail: { reason: "used" } }).kind, "stale");
  assert.equal(SigninState.clockResult(0, {}).kind, "network");
  assert.equal(SigninState.clockResult(500, { detail: { message: "boom" } }).kind, "error");
});

test("blockedCopy gives distinct, non-success copy per reason", () => {
  const completed = SigninState.blockedCopy("shift_already_completed");
  assert.equal(completed.title, "Shift already completed");
  assert.equal(completed.canRequest, true);
  assert.match(completed.desc, /request manager approval to return/);

  const reentry = SigninState.blockedCopy("reentry_approval_required");
  assert.equal(reentry.title, "Re-entry needs approval");
  assert.equal(reentry.canRequest, true);
  assert.equal(reentry.reason, "reentry");

  const noOpen = SigninState.blockedCopy("no_open_session");
  assert.equal(noOpen.title, "No open clock-in");
  assert.equal(noOpen.canRequest, false);

  const already = SigninState.blockedCopy("session_already_open");
  assert.equal(already.title, "Already clocked in");
  assert.equal(already.canRequest, false);

  const noShift = SigninState.blockedCopy("no_shift_request_approval");
  assert.equal(noShift.reason, "no_shift");
  assert.equal(noShift.canRequest, true);

  const early = SigninState.blockedCopy("before_window");
  assert.equal(early.canRequest, true);
  assert.equal(early.reason, "out_of_window");

  const after = SigninState.blockedCopy("after_shift_end");
  assert.equal(after.canRequest, true);
  assert.equal(after.reason, "out_of_window");

  const afterCtx = SigninState.blockedCopy("after_shift_end", {
    shiftEnd: "5:00 PM",
    nextShift: "Wed 11 Nov, 8:00 AM"
  });
  assert.equal(afterCtx.title, "Shift ended");
  assert.match(afterCtx.desc, /This shift ended at 5:00 PM/);
  assert.match(afterCtx.desc, /next shift starts Wed 11 Nov, 8:00 AM/);
  assert.equal(afterCtx.canRequest, true);

  const completedCtx = SigninState.blockedCopy("shift_already_completed", {
    shiftEnd: "5:00 PM",
    nextShift: "Wed 11 Nov, 8:00 AM"
  });
  assert.match(completedCtx.desc, /This shift ended at 5:00 PM/);
  assert.match(completedCtx.desc, /next shift starts Wed 11 Nov, 8:00 AM/);
  assert.equal(completedCtx.canRequest, true);

  const unknown = SigninState.blockedCopy("something_else");
  assert.equal(unknown.title, "Attendance not available");
  assert.equal(unknown.canRequest, false);
});
