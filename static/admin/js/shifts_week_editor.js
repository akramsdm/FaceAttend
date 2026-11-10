/* Reusable week editor component for Schedules tab and People "Edit week" (Phase 3 Step I/5.2).
   Exposes window.FAWeekEditor = { mount, read, fill, presets } */
(function () {
  "use strict";
  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]; }); }
  function hm(v, fb) { if (!v) return fb || "08:00"; var s = String(v); return s.length >= 5 ? s.slice(0, 5) : (fb || "08:00"); }

  var PRESETS = {
    "mon_fri_9_5": { label: "Mon–Fri 9–5", days: [1,1,1,1,1,0,0].map(function (on, i) { return { weekday:i, enabled:!!on, start_time:"09:00", end_time:"17:00", overnight:false }; }), early_window_min:30, grace_min:10, break_min:60 },
    "office_8_5": { label: "Office 8–5", days: [1,1,1,1,1,0,0].map(function (on, i) { return { weekday:i, enabled:!!on, start_time:"08:00", end_time:"17:00", overnight:false }; }), early_window_min:30, grace_min:10, break_min:60 },
    "night": { label: "Night (10–6, ends next day)", days: [1,1,1,1,1,1,1].map(function (_, i) { return { weekday:i, enabled:true, start_time:"22:00", end_time:"06:00", overnight:true }; }), early_window_min:30, grace_min:10, break_min:30 }
  };

  function autoOvernight(start, end) {
    // If end <= start, treat as ends next day (label-driven; checkbox reflects).
    if (!start || !end) return false;
    return end <= start;
  }

  function cellHtml(prefix, wd, d) {
    var en = d && d.enabled ? "checked" : (d ? "" : (wd < 5 ? "checked" : ""));
    var st = hm(d && d.start_time, "08:00");
    var et = hm(d && d.end_time, "17:00");
    // The times decide whether a shift ends next day; ignore a stale flag.
    var ov = !!(d && d.enabled) && autoOvernight(st, et);
    var idEn = prefix + "-en-" + wd, idSt = prefix + "-st-" + wd, idEt = prefix + "-et-" + wd, idOv = prefix + "-ov-" + wd;
    return '<div class="day-cell" data-daycell="' + wd + '">' +
      '<h4>' + DAYS[wd] + '</h4>' +
      '<label for="' + idEn + '"><input id="' + idEn + '" type="checkbox" data-wd="' + wd + '" data-f="enabled" ' + en + '> Working day</label>' +
      '<label for="' + idSt + '">Start <input id="' + idSt + '" type="time" data-wd="' + wd + '" data-f="start_time" value="' + esc(st) + '"></label>' +
      '<label for="' + idEt + '">End <input id="' + idEt + '" type="time" data-wd="' + wd + '" data-f="end_time" value="' + esc(et) + '"></label>' +
      '<label for="' + idOv + '" class="small"><input id="' + idOv + '" type="checkbox" data-wd="' + wd + '" data-f="overnight" ' + (ov ? "checked" : "") + '> Ends next day</label>' +
      '<p class="help" data-ov-note="' + wd + '" ' + (ov ? '' : 'hidden') + '>Ends next day</p>' +
      '</div>';
  }

  function mount(container, opts) {
    opts = opts || {};
    var prefix = opts.prefix || ("we-" + Math.random().toString(36).slice(2, 7));
    var days = opts.days || null;
    var w = opts.windows || {};
    var presetOpts = Object.keys(PRESETS).map(function (k) { return '<option value="' + k + '">' + esc(PRESETS[k].label) + '</option>'; }).join("");
    container.innerHTML =
      '<div class="toolbar" style="margin-bottom:.5rem">' +
        '<div class="field"><label for="' + prefix + '-preset">Preset</label><select id="' + prefix + '-preset"><option value="">Choose…</option>' + presetOpts + '</select></div>' +
        '<div class="field" style="align-self:flex-end"><button type="button" class="button secondary small" id="' + prefix + '-copy-mon">Copy Monday to all weekdays</button></div>' +
      '</div>' +
      '<div class="day-grid" id="' + prefix + '-grid">' + [0,1,2,3,4,5,6].map(function (i) { var d = days && days.find(function (x) { return Number(x.weekday) === i; }); return cellHtml(prefix, i, d); }).join("") + '</div>' +
      '<details class="advanced"><summary>Advanced</summary><div class="toolbar">' +
        '<div class="field"><label for="' + prefix + '-early">Clock-in opens (minutes before start)</label><input id="' + prefix + '-early" type="number" min="0" max="240" value="' + esc(String(w.early_window_min != null ? w.early_window_min : 30)) + '"></div>' +
        '<div class="field"><label for="' + prefix + '-grace">Counts as late after (minutes)</label><input id="' + prefix + '-grace" type="number" min="0" max="240" value="' + esc(String(w.grace_min != null ? w.grace_min : 10)) + '"></div>' +
        '<div class="field"><label for="' + prefix + '-break">Break (minutes)</label><input id="' + prefix + '-break" type="number" min="0" max="480" value="' + esc(String(w.break_min != null ? w.break_min : 0)) + '"></div>' +
      '</div></details>';

    function q(wd, f) { return container.querySelector('[data-wd="' + wd + '"][data-f="' + f + '"]'); }
    function syncOv(wd) {
      var st = q(wd, "start_time").value, et = q(wd, "end_time").value, en = q(wd, "enabled").checked;
      var ov = q(wd, "overnight");
      ov.checked = en && autoOvernight(st, et);
      var note = container.querySelector('[data-ov-note="' + wd + '"]');
      if (note) note.hidden = !ov.checked;
    }
    [0,1,2,3,4,5,6].forEach(function (wd) {
      ["start_time", "end_time", "enabled"].forEach(function (f) {
        var el = q(wd, f); if (el) el.addEventListener("change", function () { syncOv(wd); });
      });
      var ov = q(wd, "overnight"); if (ov) ov.addEventListener("change", function () { syncOv(wd); });
      syncOv(wd);
    });

    var presetSel = document.getElementById(prefix + "-preset");
    if (presetSel) presetSel.addEventListener("change", function () {
      var p = PRESETS[presetSel.value]; if (!p) return;
      fill(container, { days: p.days, windows: p });
      presetSel.value = "";
    });
    var copyBtn = document.getElementById(prefix + "-copy-mon");
    if (copyBtn) copyBtn.addEventListener("click", function () {
      var mon = read(container).days.find(function (d) { return d.weekday === 0; }) || { enabled:true, start_time:"09:00", end_time:"17:00", overnight:false };
      [1,2,3,4].forEach(function (wd) {
        q(wd, "enabled").checked = !!mon.enabled;
        q(wd, "start_time").value = hm(mon.start_time, "09:00");
        q(wd, "end_time").value = hm(mon.end_time, "17:00");
        q(wd, "overnight").checked = !!mon.overnight;
        syncOv(wd);
      });
    });

    container._wePrefix = prefix;
    return { prefix: prefix, container: container };
  }

  function fill(container, data) {
    if (!data) return;
    var days = data.days || [];
    [0,1,2,3,4,5,6].forEach(function (wd) {
      var d = days.find(function (x) { return Number(x.weekday) === wd; });
      var en = d ? !!d.enabled : (wd < 5);
      var st = hm(d && d.start_time, "08:00");
      var et = hm(d && d.end_time, "17:00");
      var ov = en && autoOvernight(st, et);
      var qe = container.querySelector('[data-wd="' + wd + '"][data-f="enabled"]');
      var qs = container.querySelector('[data-wd="' + wd + '"][data-f="start_time"]');
      var qt = container.querySelector('[data-wd="' + wd + '"][data-f="end_time"]');
      var qo = container.querySelector('[data-wd="' + wd + '"][data-f="overnight"]');
      if (qe) qe.checked = en; if (qs) qs.value = st; if (qt) qt.value = et; if (qo) qo.checked = ov;
      var note = container.querySelector('[data-ov-note="' + wd + '"]'); if (note) note.hidden = !ov;
    });
    var w = data.windows || data;
    var pfx = container._wePrefix;
    function setv(suf, val) { var el = pfx ? document.getElementById(pfx + suf) : null; if (el && val != null) el.value = String(val); }
    setv("-early", w.early_window_min); setv("-grace", w.grace_min); setv("-break", w.break_min);
  }

  function read(container) {
    var days = [];
    for (var wd = 0; wd < 7; wd++) {
      function q(f) { return container.querySelector('[data-wd="' + wd + '"][data-f="' + f + '"]'); }
      var st = (q("start_time").value || "08:00").slice(0, 5);
      var et = (q("end_time").value || "17:00").slice(0, 5);
      var ov = q("enabled").checked && autoOvernight(st, et);
      days.push({ weekday: wd, enabled: q("enabled").checked, start_time: st, end_time: et, overnight: ov });
    }
    var pfx = container._wePrefix;
    function gv(suf, fb) { var el = pfx ? document.getElementById(pfx + suf) : null; return el ? Number(el.value) : fb; }
    return { days: days, early_window_min: gv("-early", 30), grace_min: gv("-grace", 10), break_min: gv("-break", 0) };
  }

  window.FAWeekEditor = { mount: mount, read: read, fill: fill, presets: PRESETS };
})();
