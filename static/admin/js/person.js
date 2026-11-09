(function () {
  "use strict";
  var msg = document.getElementById("message");
  var chart = null;
  function setMsg(t, k) { msg.textContent = t || ""; msg.className = "message" + (k ? " " + k : ""); }
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function pct(v) { return (Math.round((Number(v) || 0) * 1000) / 10) + "%"; }
  function qs(name) { return new URLSearchParams(location.search).get(name); }
  function staffId() { var v = qs("id") || qs("staff_id"); return v ? parseInt(v, 10) : null; }
  function params() { return { from: document.getElementById("from").value, to: document.getElementById("to").value }; }
  async function fetchJson(path, p) {
    var q = Object.keys(p || {}).filter(function (k) { return p[k]; }).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(p[k]); }).join("&");
    var r = await FA.request(path + (q ? "?" + q : ""));
    if (!r.ok) throw new Error(await FA.readError(r));
    return r.json();
  }
  function kpi(label, value) { return '<div class="kpi-card"><p class="kpi-label">' + esc(label) + '</p><p class="kpi-value">' + esc(value) + '</p></div>'; }
  function renderKpis(m, fu) {
    var late = Number(m.late || 0), lateMin = Number(m.late_minutes || 0);
    var avgLate = (m.average_late_minutes != null) ? m.average_late_minutes : (late ? (lateMin / late).toFixed(2) : "0");
    var h = kpi("Attendance rate", pct(m.attendance_rate)) + kpi("On-time rate", pct(m.on_time_rate))
      + kpi("Late arrivals", late) + kpi("Avg late (min)", avgLate)
      + kpi("Hours worked", m.hours_worked) + kpi("Overtime (count)", m.overtime)
      + kpi("Early departures", m.early_departures) + kpi("Missing clock-outs", m.missing_clock_outs);
    document.getElementById("kpis").innerHTML = h;
    var fn = document.getElementById("followup-note");
    if (fu && fu.flagged) {
      fn.innerHTML = '<span class="badge">Needs follow-up</span> ' + esc(fu.late) + ' late arrival(s) in this range (rule: ' + esc(fu.late_count) + '+ in ' + esc(fu.period_days) + ' days). ' + esc(fu.note || "Prompt for a conversation, not an automatic penalty.");
      fn.hidden = false;
    } else if (fu && fu.note) { fn.textContent = fu.note; fn.hidden = false; } else { fn.hidden = true; }
  }
  function cls(status) { return "st-" + String(status || "").replace(/ /g, "-"); }
  function renderCalendar(days, from, to) {
    if (!days || !days.length) { document.getElementById("calendar").innerHTML = '<p class="muted">No data.</p>'; return; }
    var names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    var h = '<div class="cal" role="grid" aria-label="Calendar by day">';
    names.forEach(function (n) { h += '<div class="cal-head" role="columnheader">' + n + '</div>'; });
    var start = new Date(from + "T00:00:00"); var end = new Date(to + "T00:00:00");
    // pad to Monday
    var pad = (start.getDay() + 6) % 7; // Mon=0
    for (var i = 0; i < pad; i++) h += '<div class="day" aria-hidden="true"></div>';
    days.forEach(function (d) {
      h += '<div class="day ' + cls(d.status) + '" role="gridcell"><span class="d">' + esc(d.date.slice(8)) + '</span><span class="s">' + esc(d.status) + '</span></div>';
    });
    h += '</div>';
    document.getElementById("calendar").innerHTML = h;
  }
  function renderArrival(days) {
    var pts = (days || []).filter(function (d) { return d.arrival_offset_minutes != null; });
    var wrap = document.getElementById("arr-body");
    if (!pts.length) { wrap.innerHTML = '<p class="muted">No clock-ins in this range.</p>'; return; }
    wrap.innerHTML = '<canvas id="arr-chart" role="img" aria-label="Line chart of arrival offset by day"></canvas>';
    var ctx = document.getElementById("arr-chart").getContext("2d");
    if (chart) { try { chart.destroy(); } catch (e) {} }
    chart = new Chart(ctx, { type: "line", data: { labels: pts.map(function (d) { return d.date; }),
      datasets: [{ label: "Arrival offset (minutes vs shift start)", data: pts.map(function (d) { return d.arrival_offset_minutes; }),
        borderColor: "#1f5fa8", backgroundColor: "rgba(31,95,168,0.1)", tension: 0.15, pointRadius: 3 }] },
      options: { responsive: true, maintainAspectRatio: false,
        scales: { x: { title: { display: true, text: "Date" } }, y: { title: { display: true, text: "Minutes vs shift start (negative = early)" } } } } });
  }
  function renderEvents(events) {
    var el = document.getElementById("events");
    if (!events || !events.length) { el.innerHTML = '<p class="muted">No events in this range.</p>'; return; }
    var h = '<table class="ev-table"><thead><tr><th scope="col">Time (local)</th><th scope="col">Type</th><th scope="col">Method</th><th scope="col">Shift date</th><th scope="col">Flag</th><th scope="col">Undone</th><th scope="col">Edited by</th><th scope="col">Edit reason</th></tr></thead><tbody>';
    events.forEach(function (e) {
      h += '<tr><td>' + esc(e.timestamp || "") + '</td><td>' + esc(e.type) + '</td><td>' + esc(e.method || "-") + '</td><td>' + esc(e.shift_date || "-") + '</td><td>' + esc(e.flag || "-") + '</td><td>' + (e.undone ? "yes" : "no") + '</td><td>' + esc(e.edited_by_name || (e.edited_by ? ("#" + e.edited_by) : "-")) + '</td><td>' + esc(e.edit_reason || "-") + '</td></tr>';
    });
    h += '</tbody></table>';
    el.innerHTML = h;
  }
  function renderSchedule(s) {
    var el = document.getElementById("schedule");
    if (!s) { el.innerHTML = '<p class="muted">No schedule data.</p>'; return; }
    var h = '<dl>';
    function row(k, v) { if (v != null && v !== "") h += '<dt><strong>' + esc(k) + '</strong></dt><dd>' + esc(v) + '</dd>'; }
    row("As of", s.date); row("Source", s.source); row("Pattern", s.pattern); row("Start", s.start); row("End", s.end);
    row("Grace (min)", s.grace_min); row("Break (min)", s.break_min); row("Overnight", s.overnight != null ? String(s.overnight) : null); row("Kind", s.kind);
    h += '</dl>'; el.innerHTML = h;
  }
  async function load() {
    var id = staffId();
    if (!id) { setMsg("Missing staff id (use ?id=...).", "error"); return; }
    var p = params();
    if (!p.from || !p.to) { setMsg("Choose a date range.", "error"); return; }
    setMsg("Loading…", "info");
    try {
      var d = await fetchJson("/api/admin/metrics/person/" + id, p);
      var st = d.staff || {};
      document.getElementById("title").textContent = st.name || ("Staff " + st.id);
      document.getElementById("subtitle").textContent = (st.staff_code || "") + (st.department ? " · " + st.department : "") + " · " + d.from + " to " + d.to;
      renderKpis(d.metrics || {}, d.followup);
      renderCalendar(d.days || [], d.from, d.to);
      renderArrival(d.days || []);
      renderEvents(d.events || d.recent_events || []);
      renderSchedule(d.schedule);
      setMsg("");
    } catch (e) {
      if (e && e.message && e.message.indexOf("session") !== -1) return;
      setMsg("Could not load person: " + e.message, "error");
    }
  }
  async function exportCsv() {
    var id = staffId(); var p = params();
    if (!id || !p.from || !p.to) return;
    try {
      var r = await FA.request("/api/admin/metrics/person/" + id + "/export.csv?from=" + encodeURIComponent(p.from) + "&to=" + encodeURIComponent(p.to));
      if (!r.ok) throw new Error(await FA.readError(r));
      var blob = await r.blob(); var a = document.createElement("a"); a.href = URL.createObjectURL(blob);
      a.download = "person-" + id + ".csv"; a.click(); URL.revokeObjectURL(a.href);
    } catch (e) { setMsg("Export failed: " + e.message, "error"); }
  }
  (async function () {
    var s = await FA.requireSession(); FA.showAdminName(s.admin.username); FA.wireLogout(); FA.startSessionWatch();
    var today = new Date(); var t = today.toISOString().slice(0, 10); var d30 = new Date(today); d30.setDate(d30.getDate() - 29);
    var f = qs("from") || d30.toISOString().slice(0, 10); var to = qs("to") || t;
    document.getElementById("from").value = f; document.getElementById("to").value = to;
    document.getElementById("filters").addEventListener("submit", function (ev) { ev.preventDefault(); load(); });
    document.getElementById("export-btn").addEventListener("click", exportCsv);
    document.getElementById("print-btn").addEventListener("click", function () { window.print(); });
    load();
  })();
})();
