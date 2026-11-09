(function () {
  "use strict";
  var msg = document.getElementById("message");
  var note = document.getElementById("followup-note");
  var results = document.getElementById("results");
  function setMsg(t, k) { msg.textContent = t || ""; msg.className = "message" + (k ? " " + k : ""); }
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function pct(v) { return (Math.round((Number(v) || 0) * 1000) / 10) + "%"; }
  function params() {
    return { report: document.getElementById("report").value,
      from: document.getElementById("from").value, to: document.getElementById("to").value,
      department: document.getElementById("department").value || "" };
  }
  async function fetchJson(path, p) {
    var qs = Object.keys(p).filter(function (k) { return p[k] !== "" && p[k] != null; }).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(p[k]); }).join("&");
    var r = await FA.request(path + "?" + qs);
    if (!r.ok) throw new Error(await FA.readError(r));
    return r.json();
  }
  function render(data, kind) {
    if (data.followup && data.followup.note) { note.textContent = data.followup.note; note.hidden = false; } else { note.hidden = true; }
    var items = data.items || [], totals = data.totals || {};
    if (kind === "late-arrivals") {
      var h = '<table class="report-table"><thead><tr><th scope="col">Staff</th><th scope="col">Department</th><th scope="col">Late arrivals</th><th scope="col">Late minutes</th><th scope="col">On-time rate</th><th scope="col">Attendance rate</th><th scope="col">Follow-up</th></tr></thead><tbody>';
      if (!items.length) h += '<tr><td colspan="7">No late arrivals in this range.</td></tr>';
      items.forEach(function (x) {
        var badge = x.needs_followup ? '<span class="badge">Needs follow-up</span>' : "-";
        h += '<tr><td><a href="person.html?id=' + encodeURIComponent(x.staff_id) + '">' + esc(x.name) + ' (' + esc(x.staff_code || x.staff_id) + ')</a></td><td>' + esc(x.department || "-") + '</td><td>' + esc(x.late) + '</td><td>' + esc(x.late_minutes) + '</td><td>' + esc(pct(x.on_time_rate)) + '</td><td>' + esc(pct(x.attendance_rate)) + '</td><td>' + badge + '</td></tr>';
      });
      h += '<tr class="totals"><td colspan="2">TOTAL (' + esc(totals.staff || 0) + ' staff)</td><td>' + esc(totals.late_arrivals || 0) + '</td><td>' + esc(totals.late_minutes || 0) + '</td><td colspan="3"></td></tr>';
      h += '</tbody></table>';
      results.innerHTML = h;
    } else {
      var h2 = '<table class="report-table"><thead><tr><th scope="col">Staff</th><th scope="col">Department</th><th scope="col">Scheduled (ended)</th><th scope="col">Present</th><th scope="col">Absent</th><th scope="col">Late</th><th scope="col">On-time rate</th><th scope="col">Attendance rate</th><th scope="col">Hours</th><th scope="col">Missing out</th><th scope="col">Follow-up</th></tr></thead><tbody>';
      if (!items.length) h2 += '<tr><td colspan="11">No staff in this scope.</td></tr>';
      items.forEach(function (x) {
        var badge = x.needs_followup ? '<span class="badge">Needs follow-up</span>' : "-";
        h2 += '<tr><td><a href="person.html?id=' + encodeURIComponent(x.staff_id) + '">' + esc(x.name) + ' (' + esc(x.staff_code || x.staff_id) + ')</a></td><td>' + esc(x.department || "-") + '</td><td>' + esc(x.scheduled_ended) + '</td><td>' + esc(x.present) + '</td><td>' + esc(x.absent) + '</td><td>' + esc(x.late) + '</td><td>' + esc(pct(x.on_time_rate)) + '</td><td>' + esc(pct(x.attendance_rate)) + '</td><td>' + esc(x.hours_worked) + '</td><td>' + esc(x.missing_clock_outs) + '</td><td>' + badge + '</td></tr>';
      });
      h2 += '<tr class="totals"><td colspan="2">TOTAL (' + esc(totals.staff || 0) + ' staff)</td><td>' + esc(totals.scheduled_ended || 0) + '</td><td>' + esc(totals.present || 0) + '</td><td>' + esc(totals.absent || 0) + '</td><td>' + esc(totals.late || 0) + '</td><td></td><td></td><td>' + esc(totals.hours_worked || 0) + '</td><td>' + esc(totals.missing_clock_outs || 0) + '</td><td></td></tr>';
      h2 += '</tbody></table>';
      results.innerHTML = h2;
    }
  }
  async function run() {
    var p = params();
    if (!p.from || !p.to) { setMsg("Choose a From and To date.", "error"); return; }
    setMsg("Loading…", "info"); results.innerHTML = '<p class="muted">Loading…</p>';
    try {
      var data = await fetchJson("/api/admin/metrics/reports/" + p.report, { from: p.from, to: p.to, department: p.department });
      render(data, p.report);
      setMsg(data.items.length + " row(s). " + (data.from || "") + " to " + (data.to || "") + ".", "ok");
    } catch (e) { if (e && e.message && e.message.indexOf("session") !== -1) return; setMsg("Could not run report: " + e.message, "error"); results.innerHTML = ""; }
  }
  async function exportCsv() {
    var p = params();
    if (!p.from || !p.to) { setMsg("Choose a From and To date.", "error"); return; }
    try {
      var qs = "from=" + encodeURIComponent(p.from) + "&to=" + encodeURIComponent(p.to) + (p.department ? "&department=" + encodeURIComponent(p.department) : "") + "&format=csv";
      var r = await FA.request("/api/admin/metrics/reports/" + p.report + "?" + qs);
      if (!r.ok) throw new Error(await FA.readError(r));
      var blob = await r.blob();
      var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = p.report + ".csv"; a.click(); URL.revokeObjectURL(a.href);
    } catch (e) { setMsg("Export failed: " + e.message, "error"); }
  }
  async function loadDepartments() {
    try {
      var r = await FA.request("/api/admin/staff?active=true"); if (!r.ok) return;
      var seen = {}; (await r.json()).forEach(function (s) { if (s.department) seen[s.department] = true; });
      var sel = document.getElementById("department");
      Object.keys(seen).sort().forEach(function (d) { var o = document.createElement("option"); o.value = d; o.textContent = d; sel.appendChild(o); });
    } catch (e) {}
  }
  (async function () {
    var s = await FA.requireSession(); FA.showAdminName(s.admin.username); FA.wireLogout(); FA.startSessionWatch();
    var today = new Date(); var t = today.toISOString().slice(0, 10); var d30 = new Date(today); d30.setDate(d30.getDate() - 29);
    document.getElementById("from").value = d30.toISOString().slice(0, 10); document.getElementById("to").value = t;
    document.getElementById("filters").addEventListener("submit", function (ev) { ev.preventDefault(); run(); });
    document.getElementById("export").addEventListener("click", exportCsv);
    await loadDepartments();
  })();
})();
