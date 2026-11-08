(function () {
  "use strict";
  var msg = document.getElementById("message");
  var tz = document.getElementById("tz-label");
  var present = document.getElementById("present");
  var absent = document.getElementById("absent");
  var late = document.getElementById("late");
  var success = document.getElementById("success");
  var detail = document.getElementById("detail");

  function setMsg(t, k) { msg.textContent = t || ""; msg.className = "message" + (k ? " " + k : ""); }

  async function load() {
    setMsg("Loading…", "info");
    try {
      var r = await FA.request("/api/admin/attendance/dashboard");
      if (!r.ok) throw new Error(await FA.readError(r));
      var d = await r.json();
      tz.textContent = d.timezone || "UTC";
      present.textContent = String(d.present_today);
      absent.textContent = String(d.absent_today);
      late.textContent = String(d.late_arrivals);
      success.textContent = d.recognition_success_rate == null ? "No attempts today" : (Math.round(d.recognition_success_rate * 100) + "%");
      detail.textContent = "Date: " + d.date_local + " · Active staff: " + d.active_staff + " · Attempts today: " + d.recognition_attempts_today + " · Matched: " + d.matched_today + ".";
      setMsg("");
    } catch (e) { if (e && e.message && e.message.indexOf("session") !== -1) return; setMsg("Could not load dashboard: " + e.message, "error"); }
  }

  (async function () {
    var s = await FA.requireSession();
    FA.showAdminName(s.admin.username);
    FA.wireLogout(); FA.startSessionWatch();
    document.getElementById("refresh").addEventListener("click", load);
    load();
  })();
})();
