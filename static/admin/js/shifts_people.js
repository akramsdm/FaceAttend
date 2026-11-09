(function () {
  "use strict";

  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]; }); }
  function msg(t, k) { var m = $("message"); m.textContent = t || ""; m.className = "message" + (k ? " " + k : ""); }
  function setErr(id, t) { var el = $(id); if (el) el.textContent = t || ""; }
  function todayIso() { return new Date().toISOString().slice(0, 10); }
  function mondayOf(d) { var dt = new Date(d + "T00:00:00"); var day = (dt.getDay() + 6) % 7; dt.setDate(dt.getDate() - day); return dt.toISOString().slice(0, 10); }

  var state = { people: [], patterns: [], weekStart: "" };

  function wireTabs() {
    var tabs = [ $("tab-people"), $("tab-schedules") ];
    var panels = [ $("panel-people"), $("panel-schedules") ];
    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t, j) {
          var on = j === i;
          t.setAttribute("aria-selected", on ? "true" : "false");
          t.tabIndex = on ? 0 : -1;
          panels[j].hidden = !on;
        });
        tab.focus();
      });
      tab.addEventListener("keydown", function (ev) {
        if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
        ev.preventDefault();
        var idx = tabs.indexOf(document.activeElement);
        var n = ev.key === "ArrowRight" ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
        tabs[n].click();
      });
    });
  }

  function labelDay(d, i) {
    if (!d || !d.shift) return '<span class="off" title="' + DAYS[i] + ' off">' + DAYS[i] + ' Off</span>';
    var sh = d.shift;
    var st = (sh.start || "").slice(11, 16);
    var en = (sh.end || "").slice(11, 16);
    var cls = sh.overnight ? "night" : "";
    var tip = DAYS[i] + " " + st + "–" + en + (sh.overnight ? " (ends next day)" : "");
    return '<span class="' + cls + '" title="' + esc(tip) + '">' + esc(st) + "–" + esc(en) + "</span>";
  }

  function badges(p) {
    var b = "";
    if (p.source === "custom") b += '<span class="badge badge-custom">Custom</span>';
    if (p.source === "changed") b += '<span class="badge badge-changed">Changed</span>';
    return b;
  }

  function renderPeople(items) {
    var tb = $("people-rows");
    tb.innerHTML = "";
    if (!items.length) { tb.innerHTML = '<tr><td colspan="6" class="muted">No people match the filters.</td></tr>'; return; }
    items.forEach(function (p) {
      var tr = document.createElement("tr");
      var week = '<div class="week-strip">' + (p.days || []).map(labelDay).join("") + "</div>";
      var label = esc(p.schedule_label || "None") + badges(p);
      if (p.based_on) label += '<div class="small muted">Based on ' + esc(p.based_on) + '</div>';
      tr.innerHTML =
        '<td><input type="checkbox" class="row-sel" data-id="' + p.id + '" aria-label="Select ' + esc(p.name) + '"></td>' +
        '<td>' + esc(p.name) + '</td>' +
        '<td>' + esc(p.department || "—") + '</td>' +
        '<td>' + label + '</td>' +
        '<td>' + week + '</td>' +
        '<td class="row-actions"></td>';
      var acts = tr.querySelector(".row-actions");
      function btn(text, fn) {
        var b = document.createElement("button"); b.type = "button"; b.className = "button secondary small"; b.textContent = text;
        b.addEventListener("click", function () { fn(p); }); acts.appendChild(b);
      }
      btn("Edit week", openEditWeek);
      btn("Change a day", openChangeDay);
      btn("History", openHistory);
      tb.appendChild(tr);
    });
  }

  function selectedIds() {
    return Array.from(document.querySelectorAll(".row-sel:checked")).map(function (c) { return Number(c.getAttribute("data-id")); });
  }

  function populateScheduleFilters() {
    var opts = '<option value="">Any</option>' + state.patterns.map(function (p) { return '<option value="' + esc(p.name) + '">' + esc(p.name) + '</option>'; }).join("");
    $("f-schedule").innerHTML = opts;
    var bopts = state.patterns.map(function (p) { return '<option value="' + p.id + '">' + esc(p.name) + '</option>'; }).join("");
    $("b-schedule").innerHTML = bopts || '<option value="">No schedules</option>';
  }

  async function loadPatterns() {
    var r = await FA.request("/api/admin/shifts/patterns");
    state.patterns = r.ok ? await r.json() : [];
    populateScheduleFilters();
  }

  async function loadPeople() {
    var params = new URLSearchParams();
    var ws = $("f-week").value || state.weekStart;
    if (!ws) ws = mondayOf(todayIso());
    state.weekStart = ws; $("f-week").value = ws;
    params.set("week_start", ws);
    if ($("f-dept").value.trim()) params.set("department", $("f-dept").value.trim());
    if ($("f-search").value.trim()) params.set("search", $("f-search").value.trim());
    if ($("f-working").checked) params.set("working_today", "true");
    var r = await FA.request("/api/admin/shifts/people?" + params.toString());
    if (!r.ok) { msg(await FA.readError(r), "error"); return; }
    var data = await r.json();
    state.people = data.items || [];
    var schedFilter = $("f-schedule").value;
    var items = state.people;
    if (schedFilter) items = items.filter(function (p) { return (p.schedule_label || "") === schedFilter; });
    renderPeople(items);
  }

  function showPanel(html, focusId) {
    var panel = $("row-action-panel"); var body = $("row-action-body");
    body.innerHTML = html; panel.hidden = false;
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    if (focusId && $(focusId)) $(focusId).focus();
  }
  function hidePanel() { $("row-action-panel").hidden = true; $("row-action-body").innerHTML = ""; }

  function dayCell(wd, d) {
    var en = d && d.enabled ? "checked" : (d ? "" : (wd < 5 ? "checked" : ""));
    var st = d ? (d.start_time || "08:00") : "08:00";
    var et = d ? (d.end_time || "17:00") : "17:00";
    var ov = d && d.overnight ? "checked" : "";
    return '<div class="day-cell"><h4>' + DAYS[wd] + '</h4>' +
      '<label><input type="checkbox" data-wd="' + wd + '" data-f="enabled" ' + en + '> Working day</label>' +
      '<label>Start <input type="time" data-wd="' + wd + '" data-f="start_time" value="' + esc(String(st).slice(0,5)) + '"></label>' +
      '<label>End <input type="time" data-wd="' + wd + '" data-f="end_time" value="' + esc(String(et).slice(0,5)) + '"></label>' +
      '<label><input type="checkbox" data-wd="' + wd + '" data-f="overnight" ' + ov + '> Ends next day</label></div>';
  }
  function readDayGrid(root) {
    var days = [];
    for (var wd = 0; wd < 7; wd++) {
      function q(f) { return root.querySelector('[data-wd="' + wd + '"][data-f="' + f + '"]'); }
      var en = q("enabled").checked;
      days.push({ weekday: wd, enabled: en, start_time: q("start_time").value || "08:00", end_time: q("end_time").value || "17:00", overnight: q("overnight").checked });
    }
    return days;
  }

  async function openEditWeek(person) {
    // prefill from resolved week if available, else fetch resolved; keep it simple: fetch patterns list not enough - use resolved endpoint for week
    var ws = $("f-week").value || state.weekStart;
    var end = new Date(ws + "T00:00:00"); end.setDate(end.getDate() + 6); var we = end.toISOString().slice(0, 10);
    var rr = await FA.request("/api/admin/shifts/staff/" + person.id + "/resolved?start=" + ws + "&end=" + we);
    if (!rr.ok) { msg(await FA.readError(rr), "error"); return; }
    var resolved = (await rr.json()).items || [];
    // build day defaults: if shift present use its start/end (overnight if end date != start date)
    var dayDefaults = [];
    for (var i = 0; i < 7; i++) {
      var it = resolved[i]; var sh = it && it.shift;
      if (!sh) { dayDefaults.push({ weekday: i, enabled: false, start_time: "08:00", end_time: "17:00", overnight: false }); continue; }
      var st = (sh.start || "").slice(11, 16) || "08:00";
      var en = (sh.end || "").slice(11, 16) || "17:00";
      dayDefaults.push({ weekday: i, enabled: true, start_time: st, end_time: en, overnight: !!sh.overnight });
    }
    // find a base/shared pattern id to compare against (use current label match)
    var baseId = ""; state.patterns.forEach(function (p) { if (p.name === person.schedule_label || (person.based_on && p.name === person.based_on)) baseId = String(p.id); });
    var baseOpts = state.patterns.map(function (p) { return '<option value="' + p.id + '"' + (String(p.id)===baseId?" selected":"") + '>' + esc(p.name) + '</option>'; }).join("");
    var html = '<h3>Edit week — ' + esc(person.name) + '</h3>' +
      '<p class="help">Saving creates a personal version of this schedule and assigns it. "Reset to base" returns the person to the shared schedule.</p>' +
      '<form id="ew-form" novalidate>' +
      '<div class="toolbar">' +
      '<div class="field"><label for="ew-base">Compare to schedule</label><select id="ew-base">' + baseOpts + '</select></div>' +
      '<div class="field"><label for="ew-from">Start date</label><input id="ew-from" type="date" value="' + esc(todayIso()) + '"></div>' +
      '</div>' +
      '<details class="advanced"><summary>Advanced</summary><div class="toolbar">' +
      '<div class="field"><label for="ew-early">Clock-in opens (minutes before start)</label><input id="ew-early" type="number" min="0" max="240" value="30"></div>' +
      '<div class="field"><label for="ew-grace">Counts as late after (minutes)</label><input id="ew-grace" type="number" min="0" max="240" value="10"></div>' +
      '<div class="field"><label for="ew-break">Break (minutes)</label><input id="ew-break" type="number" min="0" max="480" value="0"></div>' +
      '</div></details>' +
      '<div class="day-grid" id="ew-grid">' + dayDefaults.map(function (d, i) { return dayCell(i, d); }).join("") + '</div>' +
      '<p id="ew-error" class="message error" role="alert"></p>' +
      '<div id="ew-preview" class="message info" aria-live="polite"></div>' +
      '<div class="button-row">' +
      '<button class="button secondary" type="button" id="ew-preview-btn">Preview impact</button>' +
      '<button class="button" type="submit" id="ew-save" disabled>Save week</button>' +
      '<button class="button secondary" type="button" id="ew-reset">Reset to base</button>' +
      '<button class="button secondary" type="button" id="ew-cancel">Close</button>' +
      '</div></form>';
    showPanel(html, "ew-base");
    $("ew-cancel").onclick = hidePanel;
    async function doPreview() {
      setErr("ew-error", "");
      var payload = weekPayload();
      var r = await FA.request("/api/admin/shifts/staff/" + person.id + "/week", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(Object.assign({}, payload, { dry_run: true })) });
      if (!r.ok) { setErr("ew-error", await FA.readError(r)); return; }
      var d = await r.json();
      var parts = ["Impact preview: " + (d.status === "reset" ? "this matches the base schedule (will reset)" : "a personal schedule version will be saved and assigned") + "."];
      if (d.warning) parts.push("Warning: " + d.warning);
      $("ew-preview").textContent = parts.join(" ");
      $("ew-save").disabled = false;
    }
    function weekPayload() {
      return {
        pattern_id: Number($("ew-base").value) || null,
        days: readDayGrid($("ew-grid")),
        early_window_min: Number($("ew-early").value),
        grace_min: Number($("ew-grace").value),
        break_min: Number($("ew-break").value),
        effective_from: $("ew-from").value || todayIso()
      };
    }
    $("ew-preview-btn").onclick = doPreview;
    $("ew-form").onsubmit = async function (ev) {
      ev.preventDefault(); setErr("ew-error", "");
      var r = await FA.request("/api/admin/shifts/staff/" + person.id + "/week", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(weekPayload()) });
      if (!r.ok) { setErr("ew-error", await FA.readError(r)); return; }
      var d = await r.json();
      msg(d.status === "reset" ? "Schedule reset to base." : "Personal week saved.", "success");
      hidePanel(); loadPeople();
    };
    $("ew-reset").onclick = async function () {
      setErr("ew-error", "");
      // reset = assign base effective today via save of identical? easiest: save with dry-run of base - but base days unknown here. Use assign endpoint to base.
      var base = Number($("ew-base").value);
      if (!base) { setErr("ew-error", "Choose a base schedule."); return; }
      var r = await FA.request("/api/admin/shifts/assignments", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ pattern_id: base, staff_ids:[person.id], effective_from: $("ew-from").value || todayIso() }) });
      if (!r.ok) { setErr("ew-error", await FA.readError(r)); return; }
      msg("Reset to base schedule.", "success"); hidePanel(); loadPeople();
    };
  }

  function openChangeDay(person) {
    var html = '<h3>Change a day — ' + esc(person.name) + '</h3>' +
      '<p class="help">A day change overrides the schedule for that date (one change per person per day). A reason is required.</p>' +
      '<form id="cd-form" novalidate>' +
      '<div class="toolbar">' +
      '<div class="field"><label for="cd-date">Date</label><input id="cd-date" type="date" value="' + esc(todayIso()) + '"></div>' +
      '<div class="field"><label for="cd-end">End date (optional range)</label><input id="cd-end" type="date"></div>' +
      '<div class="field"><label for="cd-kind">Change</label><select id="cd-kind"><option value="day_off">Day off</option><option value="custom_hours">Different hours</option></select></div>' +
      '</div>' +
      '<div class="toolbar" id="cd-times" hidden>' +
      '<div class="field"><label for="cd-st">Start</label><input id="cd-st" type="time" value="09:00"></div>' +
      '<div class="field"><label for="cd-en">End</label><input id="cd-en" type="time" value="17:00"></div>' +
      '</div>' +
      '<div class="field"><label for="cd-reason">Reason (required)</label><input id="cd-reason" type="text" maxlength="500" required></div>' +
      '<p id="cd-error" class="message error" role="alert"></p>' +
      '<div id="cd-preview" class="message info" aria-live="polite"></div>' +
      '<div class="button-row">' +
      '<button class="button secondary" type="button" id="cd-preview-btn">Preview impact</button>' +
      '<button class="button" type="submit" id="cd-save" disabled>Save day change</button>' +
      '<button class="button secondary" type="button" id="cd-cancel">Close</button></div></form>';
    showPanel(html, "cd-date");
    $("cd-kind").onchange = function () { $("cd-times").hidden = $("cd-kind").value !== "custom_hours"; };
    $("cd-cancel").onclick = hidePanel;
    function payload(dry) {
      var p = { staff_id: person.id, kind: $("cd-kind").value, reason: $("cd-reason").value.trim(), dry_run: !!dry };
      var end = $("cd-end").value;
      if (end && end !== $("cd-date").value) { p.start_date = $("cd-date").value; p.end_date = end; }
      else { p.date = $("cd-date").value; }
      if (p.kind === "custom_hours") { p.start_time = $("cd-st").value; p.end_time = $("cd-en").value; }
      return p;
    }
    $("cd-preview-btn").onclick = async function () {
      setErr("cd-error", ""); if (!$("cd-reason").value.trim()) { setErr("cd-error", "Reason is required."); return; }
      var r = await FA.request("/api/admin/shifts/exceptions", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload(true)) });
      if (!r.ok) { setErr("cd-error", await FA.readError(r)); return; }
      var d = await r.json();
      var parts = ["Affects " + (d.count || 1) + " day(s)."];
      if (d.exceptions_replaced && d.exceptions_replaced.length) parts.push("Replaces " + d.exceptions_replaced.length + " existing day change(s) (clash). ");
      if (d.warning) parts.push("Warning: " + d.warning);
      $("cd-preview").textContent = parts.join(" "); $("cd-save").disabled = false;
    };
    $("cd-form").onsubmit = async function (ev) {
      ev.preventDefault(); setErr("cd-error", "");
      if (!$("cd-reason").value.trim()) { setErr("cd-error", "Reason is required."); return; }
      var r = await FA.request("/api/admin/shifts/exceptions", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload(false)) });
      if (!r.ok) { setErr("cd-error", await FA.readError(r)); return; }
      msg("Day change saved.", "success"); hidePanel(); loadPeople();
    };
  }

  async function openHistory(person) {
    var r = await FA.request("/api/admin/shifts/staff/" + person.id + "/history");
    if (!r.ok) { msg(await FA.readError(r), "error"); return; }
    var h = await r.json();
    function sec(title, rows, render) {
      if (!rows || !rows.length) return '<h4>' + title + '</h4><p class="muted small">None.</p>';
      return '<h4>' + title + '</h4>' + rows.map(function (x) { return '<div class="history-item">' + render(x) + '</div>'; }).join("");
    }
    var html = '<h3>History — ' + esc(person.name) + '</h3>' +
      '<div class="drawer">' +
      sec("Assignments", h.assignments, function (a) {
        return esc(a.effective_from) + " → " + esc(a.effective_to || "present") + ": " + esc(a.pattern_name || ("#" + a.pattern_id)) + " <span class='small muted'>(" + esc(a.pattern_kind || "") + ")</span>";
      }) +
      sec("Personal versions", h.personal_versions, function (p) {
        return esc(p.name) + " (based on " + esc(p.based_on_name || "—") + ") · created " + esc((p.created_at||"").slice(0,19).replace("T"," ")) + (p.created_by ? " · by admin #" + p.created_by : "") + (p.archived_at ? " · <span class='muted'>archived</span>" : "");
      }) +
      sec("Day changes", h.exceptions, function (e) {
        return esc(e.date) + " · " + esc(e.kind === "day_off" ? "Day off" : "Different hours") + (e.start_time ? " " + esc(e.start_time) + "–" + esc(e.end_time) : "") + " · reason: " + esc(e.reason) + (e.created_by ? " · by admin #" + e.created_by : "");
      }) +
      '</div><div class="button-row"><button class="button secondary" type="button" id="hist-close">Close</button></div>';
    showPanel(html, "hist-close");
    $("hist-close").onclick = hidePanel;
  }

  function wireBulk() {
    $("b-start").value = todayIso();
    $("bulk-assign-open").onclick = function () { $("bulk-panel").hidden = !$("bulk-panel").hidden; $("holiday-panel").hidden = true; $("b-apply").disabled = true; $("b-preview").textContent = ""; };
    $("b-cancel").onclick = function () { $("bulk-panel").hidden = true; };
    $("b-preview-btn").onclick = async function () {
      setErr("b-error", ""); $("b-apply").disabled = true;
      var ids = selectedIds(); if (!ids.length) { setErr("b-error", "Select one or more people in the table."); return; }
      var pid = Number($("b-schedule").value); if (!pid) { setErr("b-error", "Choose a schedule."); return; }
      var eff = $("b-start").value || todayIso();
      var r = await FA.request("/api/admin/shifts/assignments", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ pattern_id: pid, staff_ids: ids, effective_from: eff, dry_run: true }) });
      if (!r.ok) { setErr("b-error", await FA.readError(r)); return; }
      var d = await r.json();
      var parts = ["Affected: " + (d.affected_people||[]).length + " person(s)."];
      if (d.assignments_ending && d.assignments_ending.length) parts.push((d.assignments_ending.length) + " current schedule(s) will end/change. ");
      if (d.warning) parts.push("Warning: " + d.warning);
      $("b-preview").textContent = parts.join(" "); $("b-apply").disabled = false;
    };
    $("bulk-form").onsubmit = async function (ev) {
      ev.preventDefault(); setErr("b-error", "");
      var ids = selectedIds(); var pid = Number($("b-schedule").value); var eff = $("b-start").value || todayIso();
      var r = await FA.request("/api/admin/shifts/assignments", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ pattern_id: pid, staff_ids: ids, effective_from: eff }) });
      if (!r.ok) { setErr("b-error", await FA.readError(r)); return; }
      var d = await r.json();
      msg("Schedule assigned to " + d.assigned + " person(s).", "success");
      $("bulk-panel").hidden = true; loadPeople();
    };
  }

  function wireHoliday() {
    $("h-start").value = todayIso(); $("h-end").value = todayIso();
    $("holiday-open").onclick = function () { $("holiday-panel").hidden = !$("holiday-panel").hidden; $("bulk-panel").hidden = true; $("h-apply").disabled = true; $("h-preview").textContent = ""; };
    $("h-cancel").onclick = function () { $("holiday-panel").hidden = true; };
    $("h-scope").onchange = function () { $("h-dept-wrap").hidden = $("h-scope").value !== "department"; };
    function hp(dry) {
      var p = { start_date: $("h-start").value, end_date: $("h-end").value, kind: "day_off", reason: $("h-reason").value.trim(), dry_run: !!dry };
      if ($("h-scope").value === "everyone") p.everyone = true; else p.department = $("h-dept").value.trim();
      return p;
    }
    $("h-preview-btn").onclick = async function () {
      setErr("h-error", ""); $("h-apply").disabled = true;
      if (!$("h-reason").value.trim()) { setErr("h-error", "Reason is required."); return; }
      var r = await FA.request("/api/admin/shifts/exceptions/bulk", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(hp(true)) });
      if (!r.ok) { setErr("h-error", await FA.readError(r)); return; }
      var d = await r.json();
      var parts = ["Will create " + d.count + " day change(s) for " + (d.affected_people||[]).length + " person(s). "];
      if (d.exceptions_replaced && d.exceptions_replaced.length) parts.push("Replaces " + d.exceptions_replaced.length + " existing day change(s). ");
      if (d.warning) parts.push("Warning: " + d.warning);
      $("h-preview").textContent = parts.join(" "); $("h-apply").disabled = false;
    };
    $("holiday-form").onsubmit = async function (ev) {
      ev.preventDefault(); setErr("h-error", "");
      if (!$("h-reason").value.trim()) { setErr("h-error", "Reason is required."); return; }
      var r = await FA.request("/api/admin/shifts/exceptions/bulk", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(hp(false)) });
      if (!r.ok) { setErr("h-error", await FA.readError(r)); return; }
      var d = await r.json();
      msg("Holiday applied: " + d.count + " day change(s).", "success");
      $("holiday-panel").hidden = true; loadPeople();
    };
  }

  async function boot() {
    FA.wireLogout();
    var me = await FA.requireSession();
    FA.showAdminName(me.admin.username);
    FA.startSessionWatch();
    wireTabs();
    state.weekStart = mondayOf(todayIso());
    $("f-week").value = state.weekStart;
    await loadPatterns();
    await loadPeople();
    wireBulk(); wireHoliday();
    $("filters-form").addEventListener("submit", function (ev) { ev.preventDefault(); loadPeople(); });
  }

  document.addEventListener("DOMContentLoaded", function () { boot().catch(function (e) { msg(e.message, "error"); }); });
})();
