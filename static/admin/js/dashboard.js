(function () {
  "use strict";

  var msg = document.getElementById("message");
  var rangeSel = document.getElementById("range");
  var fromInp = document.getElementById("from");
  var toInp = document.getElementById("to");
  var deptSel = document.getElementById("department");
  var customWrap = document.getElementById("custom-wrap");
  var customWrapTo = document.getElementById("custom-wrap-to");
  var charts = {};
  var tableMode = {};

  // Patterned fills so charts do not rely on colour alone (accessible).
  var PAT = {};
  function makePattern(color, style) {
    var c = document.createElement("canvas"); c.width = 8; c.height = 8;
    var g = c.getContext("2d");
    g.fillStyle = "#ffffff"; g.fillRect(0, 0, 8, 8);
    g.strokeStyle = color; g.fillStyle = color; g.lineWidth = 2;
    if (style === "diag") { g.beginPath(); g.moveTo(-2, 8); g.lineTo(8, -2); g.moveTo(2, 10); g.lineTo(10, 2); g.stroke(); }
    else if (style === "cross") { g.beginPath(); g.moveTo(-2, 8); g.lineTo(8, -2); g.moveTo(0, -2); g.lineTo(10, 8); g.stroke(); }
    else if (style === "dot") { g.fillRect(1, 1, 2, 2); g.fillRect(5, 5, 2, 2); }
    else { g.globalAlpha = 0.35; g.fillRect(0, 0, 8, 8); g.globalAlpha = 1; }
    return Chart && Chart.ctx ? Chart.ctx.createPattern(c, "repeat") : color;
  }
  function palette(ctx) {
    // rebuild patterns per chart ctx
    function p(color, style) { var c = document.createElement("canvas"); c.width = 8; c.height = 8; var g = c.getContext("2d");
      g.fillStyle = "#fff"; g.fillRect(0,0,8,8); g.strokeStyle = color; g.fillStyle = color; g.lineWidth = 2;
      if (style === "diag") { g.beginPath(); g.moveTo(-2,8); g.lineTo(8,-2); g.moveTo(2,10); g.lineTo(10,2); g.stroke(); }
      else if (style === "cross") { g.beginPath(); g.moveTo(-2,8); g.lineTo(8,-2); g.moveTo(0,-2); g.lineTo(10,8); g.stroke(); }
      else if (style === "dot") { g.fillRect(1,1,2,2); g.fillRect(5,5,2,2); }
      else { g.globalAlpha = 0.35; g.fillRect(0,0,8,8); g.globalAlpha = 1; }
      return ctx.createPattern(c, "repeat"); }
    return { present: p("#1d6b3d", "dot"), late: p("#7a5200", "diag"), absent: p("#9f242b", "cross"), bar: p("#1f5fa8", "solid"), bar2: p("#17487d", "diag") };
  }
  var COLORS = { present: "#1d6b3d", late: "#7a5200", absent: "#9f242b", bar: "#1f5fa8", bar2: "#17487d" };

  function setMsg(t, k) { msg.textContent = t || ""; msg.className = "message" + (k ? " " + k : ""); }
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function fmtDate(d) { return d.toISOString().slice(0, 10); }
  function pct(v) { return (Math.round((Number(v) || 0) * 1000) / 10) + "%"; }

  function rangeParams() {
    var mode = rangeSel.value;
    var today = new Date();
    var to = fmtDate(today);
    var from = to;
    if (mode === "7") { var d = new Date(today); d.setDate(d.getDate() - 6); from = fmtDate(d); }
    else if (mode === "30") { var d2 = new Date(today); d2.setDate(d2.getDate() - 29); from = fmtDate(d2); }
    else if (mode === "custom") { from = fromInp.value || to; to = toInp.value || to; }
    return { from: from, to: to, department: deptSel.value || "" };
  }
  function syncCustom() {
    var on = rangeSel.value === "custom";
    customWrap.hidden = !on; customWrapTo.hidden = !on;
    if (on && !fromInp.value) {
      var d = new Date(); var t = fmtDate(d); d.setDate(d.getDate() - 29); fromInp.value = fmtDate(d); toInp.value = t;
    }
  }

  async function fetchJson(path, params) {
    var qs = [];
    Object.keys(params || {}).forEach(function (k) { if (params[k] !== "" && params[k] != null) qs.push(encodeURIComponent(k) + "=" + encodeURIComponent(params[k])); });
    var url = path + (qs.length ? "?" + qs.join("&") : "");
    var r = await FA.request(url);
    if (!r.ok) throw new Error(await FA.readError(r));
    return r.json();
  }

  function setKpi(id, val) { var el = document.getElementById(id); if (el) el.textContent = (val == null ? "-" : String(val)); }
  function state(bodyId, text, isError) {
    var b = document.getElementById("body-" + bodyId); if (!b) return;
    b.innerHTML = '<p class="chart-state"' + (isError ? ' role="alert"' : '') + ">" + esc(text) + "</p>";
  }
  function destroyChart(key) { if (charts[key]) { try { charts[key].destroy(); } catch (e) {} charts[key] = null; } }

  function renderTable(bodyId, headers, rows) {
    var b = document.getElementById("body-" + bodyId); if (!b) return;
    var h = "<table class=\"chart-table\"><thead><tr>" + headers.map(function (x) { return "<th scope=\"col\">" + esc(x) + "</th>"; }).join("") + "</tr></thead><tbody>";
    if (!rows.length) { h += "<tr><td colspan=\"" + headers.length + "\">No data for this range.</td></tr>"; }
    else { rows.forEach(function (r) { h += "<tr>" + r.map(function (c) { return "<td>" + esc(c) + "</td>"; }).join("") + "</tr>"; }); }
    h += "</tbody></table>";
    b.innerHTML = h;
  }

  function renderChart(bodyId, config, tableHeaders, tableRows) {
    if (tableMode[bodyId]) { renderTable(bodyId, tableHeaders, tableRows); return; }
    var b = document.getElementById("body-" + bodyId); if (!b) return;
    b.innerHTML = '<canvas id="chart-' + bodyId + '" role="img" aria-label="' + esc(config.aria || bodyId + " chart") + '"></canvas>';
    var ctx = b.querySelector("canvas").getContext("2d");
    destroyChart(bodyId);
    try { charts[bodyId] = new Chart(ctx, config); } catch (e) { state(bodyId, "Chart could not render: " + e.message, true); }
  }

  function baseScales(xTitle, yTitle, stacked) {
    return {
      x: { stacked: !!stacked, title: { display: true, text: xTitle } },
      y: { stacked: !!stacked, beginAtZero: true, title: { display: true, text: yTitle } }
    };
  }

  function renderTrend(items) {
    if (!items || !items.length) { state("trend", "No data for this range."); return; }
    var labels = items.map(function (d) { return d.date; });
    var ds = function (label, key, color, pat) { return { label: label, data: items.map(function (d) { return d[key]; }), backgroundColor: pat, borderColor: color, borderWidth: 2 }; };
    // patterns need ctx - render via callback
    if (tableMode["trend"]) { renderTable("trend", ["Date", "Present", "Late", "Absent"], items.map(function (d) { return [d.date, d.present, d.late, d.absent]; })); return; }
    var b = document.getElementById("body-trend"); b.innerHTML = '<canvas id="chart-trend" role="img" aria-label="Stacked bar: daily present, late and absent"></canvas>';
    var ctx = b.querySelector("canvas").getContext("2d"); var pal = palette(ctx);
    destroyChart("trend");
    charts["trend"] = new Chart(ctx, {
      type: "bar",
      data: { labels: labels, datasets: [ds("Present", "present", COLORS.present, pal.present), ds("Late", "late", COLORS.late, pal.late), ds("Absent", "absent", COLORS.absent, pal.absent)] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true } }, scales: baseScales("Date", "Number of people", true), aria: "Daily trend" }
    });
  }

  function renderArrivals(items) {
    if (!items || !items.length) { state("arrivals", "No arrival data for this range."); return; }
    var labels = items.map(function (b) { return b.bucket_start_min + " to " + b.bucket_end_min + " min"; });
    var rows = items.map(function (b) { return [b.bucket_start_min + "–" + b.bucket_end_min, b.count]; });
    if (tableMode["arrivals"]) { renderTable("arrivals", ["Offset bucket (min vs shift start)", "Count"], rows); return; }
    var b = document.getElementById("body-arrivals"); b.innerHTML = '<canvas id="chart-arrivals" role="img" aria-label="Histogram of arrival offsets"></canvas>';
    var ctx = b.querySelector("canvas").getContext("2d"); var pal = palette(ctx); destroyChart("arrivals");
    charts["arrivals"] = new Chart(ctx, { type: "bar", data: { labels: labels, datasets: [{ label: "Arrivals (count)", data: items.map(function (x) { return x.count; }), backgroundColor: pal.bar, borderColor: COLORS.bar, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, scales: baseScales("Arrival offset (minutes vs shift start; negative = early)", "Number of arrivals", false) } });
  }

  function renderWeekday(items) {
    if (!items || !items.length) { state("weekday", "No data for this range."); return; }
    var labels = items.map(function (d) { return d.weekday_name; });
    var rows = items.map(function (d) { return [d.weekday_name, d.clock_ins, d.late, pct(d.lateness_rate)]; });
    if (tableMode["weekday"]) { renderTable("weekday", ["Weekday", "Clock-ins", "Late", "Lateness rate"], rows); return; }
    var b = document.getElementById("body-weekday"); b.innerHTML = '<canvas id="chart-weekday" role="img" aria-label="Late arrivals by weekday"></canvas>';
    var ctx = b.querySelector("canvas").getContext("2d"); var pal = palette(ctx); destroyChart("weekday");
    charts["weekday"] = new Chart(ctx, { type: "bar", data: { labels: labels, datasets: [{ label: "Late arrivals (count)", data: items.map(function (d) { return d.late; }), backgroundColor: pal.late, borderColor: COLORS.late, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, scales: baseScales("Weekday", "Late arrivals (count)", false) } });
  }

  function renderHours(items) {
    if (!items || !items.length) { state("hours", "No data for this range."); return; }
    var labels = items.map(function (d) { return d.group || "(none)"; });
    var rows = items.map(function (d) { return [d.group || "(none)", d.hours_worked]; });
    if (tableMode["hours"]) { renderTable("hours", ["Department", "Hours worked"], rows); return; }
    var b = document.getElementById("body-hours"); b.innerHTML = '<canvas id="chart-hours" role="img" aria-label="Hours worked by department"></canvas>';
    var ctx = b.querySelector("canvas").getContext("2d"); var pal = palette(ctx); destroyChart("hours");
    charts["hours"] = new Chart(ctx, { type: "bar", data: { labels: labels, datasets: [{ label: "Hours worked", data: items.map(function (d) { return d.hours_worked; }), backgroundColor: pal.bar, borderColor: COLORS.bar, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, scales: baseScales("Department", "Hours worked", false) } });
  }

  function renderRate(items) {
    if (!items || !items.length) { state("rate", "No data for this range."); return; }
    var labels = items.map(function (d) { return d.group || "(none)"; });
    var rows = items.map(function (d) { return [d.group || "(none)", pct(d.attendance_rate), d.present, d.scheduled_ended]; });
    if (tableMode["rate"]) { renderTable("rate", ["Department", "Attendance rate", "Present (ended days)", "Scheduled ended days"], rows); return; }
    var b = document.getElementById("body-rate"); b.innerHTML = '<canvas id="chart-rate" role="img" aria-label="Attendance rate by department"></canvas>';
    var ctx = b.querySelector("canvas").getContext("2d"); var pal = palette(ctx); destroyChart("rate");
    charts["rate"] = new Chart(ctx, { type: "bar", data: { labels: labels, datasets: [{ label: "Attendance rate", data: items.map(function (d) { return Math.round((d.attendance_rate || 0) * 1000) / 10; }), backgroundColor: pal.present, borderColor: COLORS.present, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, scales: { x: { title: { display: true, text: "Department" } }, y: { beginAtZero: true, max: 100, title: { display: true, text: "Attendance rate (%)" } } } } });
  }

  function renderList(bodyId, items, metricLabel, metricFn) {
    var b = document.getElementById("list-" + bodyId); if (!b) return;
    if (!items || !items.length) { b.innerHTML = '<p class="chart-state">No data for this range.</p>'; return; }
    var h = '<ol class="rank-list">';
    items.slice(0, 8).forEach(function (p) {
      h += '<li><a href="attendance.html?staff_id=' + encodeURIComponent(p.staff_id) + '">' + esc(p.name || ("Staff " + p.staff_id)) + '</a>'
        + ' <span class="muted small">(' + esc(p.department || "-") + ') — ' + esc(metricLabel) + ': ' + esc(metricFn(p)) + '</span></li>';
    });
    h += '</ol>';
    b.innerHTML = h;
  }

  async function loadDepartments() {
    try {
      var r = await FA.request("/api/admin/staff?active=true");
      if (!r.ok) return;
      var list = await r.json();
      var seen = {};
      list.forEach(function (s) { if (s.department) seen[s.department] = true; });
      Object.keys(seen).sort().forEach(function (d) {
        var opt = document.createElement("option"); opt.value = d; opt.textContent = d; deptSel.appendChild(opt);
      });
    } catch (e) { /* non-fatal */ }
  }


  async function load() {
    setMsg("Loading dashboard…", "info");
    ["trend", "arrivals", "weekday", "hours", "rate"].forEach(function (k) { state(k, "Loading…"); });
    document.getElementById("list-late-body").innerHTML = '<p class="chart-state">Loading…</p>';
    document.getElementById("list-low-body").innerHTML = '<p class="chart-state">Loading…</p>';
    var p = rangeParams();
    document.getElementById("range-label").textContent = p.from + " to " + p.to + (p.department ? " · " + p.department : "");
    try {
      var results = await Promise.all([
        fetchJson("/api/admin/metrics/summary", p),
        fetchJson("/api/admin/metrics/trend", p),
        fetchJson("/api/admin/metrics/arrivals", p),
        fetchJson("/api/admin/metrics/breakdown", Object.assign({ by: "weekday" }, p)),
        fetchJson("/api/admin/metrics/breakdown", Object.assign({ by: "department" }, p)),
        fetchJson("/api/admin/metrics/people", Object.assign({ sort: "late" }, p)),
        fetchJson("/api/admin/metrics/people", Object.assign({ sort: "absent" }, p))
      ]);
      var summary = results[0], trend = results[1], arrivals = results[2], byWd = results[3], byDept = results[4], latePeople = results[5], absentPeople = results[6];
      var t = summary.today || {};
      setKpi("kpi-present", t.present); setKpi("kpi-absent", t.absent_so_far); setKpi("kpi-late", t.late);
      setKpi("kpi-clocked", t.currently_clocked_in); setKpi("kpi-missing", t.missing_clock_outs); setKpi("kpi-pending", t.pending_approvals);
      renderTrend((trend.items || []));
      renderArrivals((arrivals.items || []));
      renderWeekday((byWd.items || []));
      renderHours((byDept.items || []));
      renderRate((byDept.items || []));
      var lItems = (latePeople.items || []).filter(function (x) { return (x.late || 0) > 0; });
      renderList("late-body", lItems.length ? lItems : (latePeople.items || []), "late arrivals", function (x) { return x.late; });
      var low = (absentPeople.items || []).slice().sort(function (a, b) { return (a.attendance_rate || 0) - (b.attendance_rate || 0); });
      renderList("low-body", low, "attendance rate", function (x) { return pct(x.attendance_rate) + " (" + x.absent + " absent)"; });
      setMsg("");
    } catch (e) {
      if (e && e.message && e.message.indexOf("session") !== -1) return;
      setMsg("Could not load dashboard: " + e.message, "error");
      ["trend", "arrivals", "weekday", "hours", "rate"].forEach(function (k) { state(k, "Error loading data.", true); });
      document.getElementById("list-late-body").innerHTML = '<p class="chart-state" role="alert">Error loading data.</p>';
      document.getElementById("list-low-body").innerHTML = '<p class="chart-state" role="alert">Error loading data.</p>';
    }
  }

  function wireToggles() {
    document.querySelectorAll("[data-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var k = btn.getAttribute("data-toggle");
        tableMode[k] = !tableMode[k];
        btn.textContent = tableMode[k] ? "View as chart" : "View as table";
        // re-render from last state by reloading (simple, keeps logic single-source)
        load();
      });
    });
  }

  (async function () {
    var s = await FA.requireSession();
    FA.showAdminName(s.admin.username);
    FA.wireLogout(); FA.startSessionWatch();
    syncCustom();
    rangeSel.addEventListener("change", syncCustom);
    document.getElementById("filters").addEventListener("submit", function (ev) { ev.preventDefault(); load(); });
    document.getElementById("refresh").addEventListener("click", load);
    wireToggles();
    await loadDepartments();
    load();
  })();
})();
