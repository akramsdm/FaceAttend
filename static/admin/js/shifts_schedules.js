(function () {
  "use strict";
  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]; }); }
  function msg(t, k) { var m = $("message"); m.textContent = t || ""; m.className = "message" + (k ? " " + k : ""); }
  function setErr(t) { var el = $("sch-error"); if (el) el.textContent = t || ""; }
  function hm(v) { return v ? String(v).slice(0, 5) : "—"; }

  var state = { patterns: [] };

  function summary(p) {
    var on = (p.days || []).filter(function (d) { return d.enabled; }).sort(function (a,b){return a.weekday-b.weekday;});
    if (!on.length) return "No working days";
    var first = on[0];
    var same = on.every(function (d) { return hm(d.start_time)===hm(first.start_time) && hm(d.end_time)===hm(first.end_time) && !!d.overnight===!!first.overnight; });
    var names = on.map(function (d) { return DAYS[d.weekday]; });
    var range = names.length === 5 && names[0]==="Mon" && names[4]==="Fri" ? "Mon–Fri" : names.join(", ");
    var times = hm(first.start_time) + "–" + hm(first.end_time) + (first.overnight ? " (ends next day)" : "");
    return range + (same ? " " + times : " (varies)");
  }

  function render() {
    var host = $("sched-cards");
    host.innerHTML = "";
    if (!state.patterns.length) { host.innerHTML = '<p class="muted">No schedules yet. Create one to get started.</p>'; return; }
    state.patterns.forEach(function (p) {
      var used = Number(p.used_by || 0);
      var canDel = used === 0;
      var card = document.createElement("article");
      card.className = "schedule-card";
      card.setAttribute("aria-label", p.name);
      card.innerHTML =
        '<h3>' + esc(p.name) + '</h3>' +
        '<p>' + esc(summary(p)) + '</p>' +
        '<div class="schedule-meta">Used by ' + used + ' ' + (used === 1 ? "person" : "people") + (p.is_default ? ' · <span class="muted">Default</span>' : '') + '</div>' +
        '<div class="button-row" style="margin-top:.6rem"></div>';
      var acts = card.querySelector(".button-row");
      function btn(text, cls, fn, disabled, title) {
        var b = document.createElement("button"); b.type = "button"; b.className = "button " + (cls||"secondary") + " small"; b.textContent = text;
        if (disabled) { b.disabled = true; if (title) b.title = title; }
        b.addEventListener("click", function () { fn(p); }); acts.appendChild(b);
      }
      btn("Edit", "secondary", openEdit);
      btn("Duplicate", "secondary", duplicate);
      btn("Delete", "danger", del, !canDel, canDel ? "" : "In use by current/future assignments or history; reassign before deleting");
      host.appendChild(card);
    });
  }

  async function load() {
    var r = await FA.request("/api/admin/shifts/patterns");
    if (!r.ok) { setErr(await FA.readError(r)); return; }
    state.patterns = await r.json();
    render();
    // refresh People tab selectors if present
    if (window.FAShiftsPeople && window.FAShiftsPeople.reloadPatterns) window.FAShiftsPeople.reloadPatterns(state.patterns);
  }

  function openEditor(pattern) {
    var isEdit = !!(pattern && pattern.id);
    $("sch-editor-title").textContent = isEdit ? "Edit schedule" : "New schedule";
    $("sch-id").value = isEdit ? pattern.id : "";
    $("sch-name").value = isEdit ? pattern.name : "";
    var data = isEdit ? { days: pattern.days, windows: pattern } : null;
    window.FAWeekEditor.mount($("sch-editor-host"), { prefix: "sch", days: data ? data.days : null, windows: data ? data.windows : {} });
    setErr("");
    $("sch-editor").hidden = false;
    $("sch-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("sch-name").focus();
  }

  function openEdit(p) { openEditor(p); }

  async function duplicate(p) {
    setErr("");
    var payload = {
      name: p.name + " (copy)",
      early_window_min: p.early_window_min, grace_min: p.grace_min, break_min: p.break_min,
      days: (p.days || []).map(function (d) { return { weekday:d.weekday, start_time:hm(d.start_time), end_time:hm(d.end_time), overnight:!!d.overnight, enabled:!!d.enabled }; })
    };
    var r = await FA.request("/api/admin/shifts/patterns", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload) });
    if (!r.ok) { setErr(await FA.readError(r)); msg(await FA.readError(r), "error"); return; }
    msg("Schedule duplicated.", "success"); load();
  }

  async function del(p) {
    if (Number(p.used_by || 0) > 0) { msg("Schedule is in use; reassign before deleting.", "error"); return; }
    if (!confirm("Delete schedule '" + p.name + "'?")) return;
    var r = await FA.request("/api/admin/shifts/patterns/" + p.id, { method: "DELETE" });
    if (!r.ok) { msg(await FA.readError(r), "error"); return; }
    msg("Schedule deleted.", "success"); load();
  }

  async function saveEditor(ev) {
    ev.preventDefault(); setErr("");
    var name = $("sch-name").value.trim();
    if (!name) { setErr("Name is required."); $("sch-name").focus(); return; }
    var w = window.FAWeekEditor.read($("sch-editor-host"));
    var payload = { name: name, early_window_min: w.early_window_min, grace_min: w.grace_min, break_min: w.break_min, days: w.days };
    var id = $("sch-id").value;
    var path = id ? "/api/admin/shifts/patterns/" + id : "/api/admin/shifts/patterns";
    var method = id ? "PUT" : "POST";
    var r = await FA.request(path, { method: method, headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload) });
    if (!r.ok) { setErr(await FA.readError(r)); return; }
    msg(id ? "Schedule updated." : "Schedule created.", "success");
    $("sch-editor").hidden = true;
    load();
  }

  function init() {
    var btnNew = $("sch-new-btn"); if (btnNew) btnNew.addEventListener("click", function () { openEditor(null); });
    var form = $("sch-form"); if (form) form.addEventListener("submit", saveEditor);
    var cancel = $("sch-cancel"); if (cancel) cancel.addEventListener("click", function () { $("sch-editor").hidden = true; setErr(""); });
    load();
  }

  window.FASchedulesTab = { init: init, load: load };
})();
