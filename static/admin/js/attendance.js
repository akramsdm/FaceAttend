(function () {
  "use strict";
  var msg = document.getElementById("message");
  var daysEl = document.getElementById("days");
  var chipsEl = document.getElementById("chips");
  var matchEl = document.getElementById("match-count");
  var pager = document.getElementById("pager");
  var pageInfo = document.getElementById("page-info");
  var prevBtn = document.getElementById("prev-page");
  var nextBtn = document.getElementById("next-page");
  var form = document.getElementById("filters-form");
  var editor = document.getElementById("editor");
  var editForm = document.getElementById("edit-form");
  var editId = document.getElementById("edit-id");
  var editStaff = document.getElementById("edit-staff");
  var editType = document.getElementById("edit-type");
  var editTime = document.getElementById("edit-time");
  var editReason = document.getElementById("edit-reason");
  var editError = document.getElementById("edit-error");

  var state = { page: 1, last: null };

  function setMsg(t, k) { msg.textContent = t || ""; msg.className = "message" + (k ? " " + k : ""); }
  function setErr(t) { editError.textContent = t || ""; }
  function esc(s) { return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function fmtDate(d) { return d.toISOString().slice(0,10); }

  function val(id){ var el=document.getElementById(id); return el?el.value:""; }
  function setVal(id,v){ var el=document.getElementById(id); if(el) el.value=v; }
  function setPreset(kind) {
    var now = new Date();
    var from = new Date(now), to = new Date(now);
    if (kind === "week") { var day = (now.getDay()+6)%7; from.setDate(now.getDate()-day); to.setDate(from.getDate()+6); }
    else if (kind === "last7") { from.setDate(now.getDate()-6); }
    else if (kind === "today") { /* same */ }
    setVal("from", fmtDate(from)); setVal("to", fmtDate(to));
  }

  function checked(name) {
    if(!form) return [];
    return Array.prototype.slice.call(form.querySelectorAll('input[name="'+name+'"]:checked')).map(function(i){ return i.value; });
  }

  function buildParams(includePage) {
    var p = new URLSearchParams();
    var from = val("from"), to = val("to");
    if (from) p.set("from", from); if (to) p.set("to", to);
    ["department","search","method","days_per_page"].forEach(function(id){ var v=val(id).trim(); if(v) p.set(id,v); });
    ["arrival","departure","session","record"].forEach(function(g){ checked(g).forEach(function(v){ p.append(g,v); }); });
    var amin = val("arrival_min"); if (amin !== "") p.set("arrival_min_minutes", amin);
    var dmin = val("departure_min"); if (dmin !== "") p.set("departure_min_minutes", dmin);
    if (includePage) p.set("page", String(state.page || 1));
    return p;
  }

  var LABELS = {
    arrival:{early:"Early",on_time:"On time",late:"Late"}, departure:{early_departure:"Early departure",on_time:"On time",late_clock_out:"Late clock-out"},
    session:{in_progress:"In progress",missing_clock_out:"Missing clock-out",orphan_out:"Out without in"},
    record:{manual:"Manual",edited:"Edited",approved_request:"Approved request"}
  };
  function renderChips() {
    chipsEl.innerHTML = "";
    var any = false;
    function addChip(label, clearFn) {
      any = true; var c=document.createElement("span"); c.className="chip"; c.textContent=label+" ";
      var b=document.createElement("button"); b.type="button"; b.setAttribute("aria-label","Remove "+label); b.textContent="×";
      b.addEventListener("click", function(){ clearFn(); load(true); }); c.appendChild(b); chipsEl.appendChild(c);
    }
    ["arrival","departure","session","record"].forEach(function(g){
      checked(g).forEach(function(v){ addChip((LABELS[g]||{})[v]||v, function(){ var i=form.querySelector('input[name="'+g+'"][value="'+v+'"]'); if(i) i.checked=false; }); });
    });
    var amin=val("arrival_min"); if(amin!=="") addChip("Arrival ≥ "+amin+" min", function(){ setVal("arrival_min",""); });
    var dmin=val("departure_min"); if(dmin!=="") addChip("Departure ≥ "+dmin+" min", function(){ setVal("departure_min",""); });
    var dept=val("department").trim(); if(dept) addChip("Dept: "+dept, function(){ setVal("department",""); });
    var srch=val("search").trim(); if(srch) addChip("Search: "+srch, function(){ setVal("search",""); });
    var meth=val("method"); if(meth) addChip("Method: "+meth, function(){ setVal("method",""); });
    if (!any) { chipsEl.innerHTML = '<span class="muted small">No active filters.</span>'; }
  }

  function badge(cls, text) { return '<span class="badge '+cls+'">'+esc(text)+'</span>'; }

  function fmtMins(m, earlyWord, lateWord, atWord) {
    // m is minutes_vs_* (negative = early for arrival / before end for departure)
    if (m == null) return "";
    var a = Math.abs(m);
    if (m < 0) return earlyWord+", "+a+" min before "+atWord;
    if (m > 0) return lateWord+", "+a+" min after "+atWord;
    return "At "+atWord;
  }

  function statusBadges(s) {
    var out = [];
    // session states
    if ((s.state_tags||[]).indexOf("in_progress") !== -1) out.push(badge("b-prog","Still in"));
    if ((s.state_tags||[]).indexOf("missing_clock_out") !== -1) out.push(badge("b-miss","Missing clock-out (warning)"));
    if ((s.state_tags||[]).indexOf("orphan_out") !== -1) out.push(badge("b-orphan","Out without in"));
    // arrival
    if (s.arrival_status === "late") out.push(badge("b-late", fmtMins(s.minutes_vs_start,"Early","Late","start")));
    else if (s.arrival_status === "early") out.push(badge("b-early", fmtMins(s.minutes_vs_start,"Early","Late","start")));
    else if (s.arrival_status === "on_time") out.push(badge("b-ok","On time (arrival)"));
    // departure
    if (s.departure_status === "early_departure") out.push(badge("b-earlydep", "Left "+Math.abs(s.minutes_vs_end||0)+" min early"));
    else if (s.departure_status === "late_clock_out") out.push(badge("b-lateout", "Late clock-out, +"+(s.minutes_vs_end||0)+" min"));
    else if (s.departure_status === "on_time") out.push(badge("b-ok","On time (departure)"));
    // record
    if (s.edited) out.push(badge("b-rec","Edited"));
    if ((s.record_tags||[]).indexOf("manual") !== -1) out.push(badge("b-rec","Manual"));
    if ((s.record_tags||[]).indexOf("approved_request") !== -1) out.push(badge("b-rec","Approved request"));
    if (!out.length) return '<span class="muted">—</span>';
    return '<span class="badges">'+out.join(" ")+'</span>';
  }

  function humanReason(r) {
    if (!r) return null;
    var t = String(r);
    if (t.indexOf("token:") === 0) return null; // hide system reason
    return t;
  }

  function dur(mins) { return (mins == null) ? "—" : (mins + " min"); }

  function renderDay(day) {
    var wrap = document.createElement("section"); wrap.className = "day-block";
    var head = document.createElement("div"); head.className = "day-head";
    var sum = day.summary || {};
    var parts = [];
    function pc(k,l){ if(sum[k]) parts.push(l+": "+sum[k]); }
    pc("arrival:late","Late"); pc("arrival:early","Early"); pc("departure:early_departure","Left early");
    pc("departure:late_clock_out","Late clock-out"); pc("session:in_progress","In progress");
    pc("session:missing_clock_out","Missing clock-out"); pc("session:orphan_out","Out without in");
    head.innerHTML = '<h3>'+esc(day.date)+'</h3><span class="day-summary">'+esc(parts.join(" · ") || "No status flags")+' · '+(day.sessions||[]).length+' session(s)</span>';
    var tog = document.createElement("button"); tog.type="button"; tog.className="button secondary"; tog.textContent="Collapse";
    head.appendChild(tog); wrap.appendChild(head);
    var body = document.createElement("div"); body.className="day-body";
    var ul = document.createElement("ul"); ul.className="sess-list";
    (day.sessions||[]).forEach(function(s){ ul.appendChild(renderSession(s)); });
    if (!(day.sessions||[]).length) { var li=document.createElement("li"); li.className="sess-row muted"; li.textContent="No sessions for this day with the active filters."; ul.appendChild(li); }
    body.appendChild(ul); wrap.appendChild(body);
    tog.addEventListener("click", function(){ var h=body.hidden=!body.hidden; tog.textContent=h?"Expand":"Collapse"; });
    return wrap;
  }

  function renderSession(s) {
    var li = document.createElement("li"); li.className="sess-row";
    var reason = humanReason(s.reason);
    var evBtn = (s.event_ids && s.event_ids.length) ? '<button type="button" class="button secondary" data-ev>Events ('+s.event_ids.length+')</button>' : '';
    li.innerHTML =
      '<div class="sess-main">'+
        '<div class="name"><b><a href="person.html?id='+encodeURIComponent(s.staff_id)+'">'+esc(s.name||"")+'</a></b><span class="sub">'+esc([s.staff_code,s.department].filter(Boolean).join(" · "))+'</span></div>'+
        '<div><span class="cell-lbl">Shift</span>'+esc(s.shift_name||"—")+'<span class="sub">'+esc((s.shift_start?s.shift_start.slice(11,16):"")+"–"+(s.shift_end?s.shift_end.slice(11,16):""))+'</span></div>'+
        '<div><span class="cell-lbl">Clock in</span>'+esc(s.clock_in_display||"—")+'</div>'+
        '<div><span class="cell-lbl">Clock out</span>'+esc(s.clock_out_display || ((s.state_tags||[]).indexOf("in_progress")!==-1?"Still in":"—"))+'</div>'+
        '<div><span class="cell-lbl">Duration</span>'+esc(dur(s.duration_minutes))+'</div>'+
        '<div><span class="cell-lbl">Status</span>'+statusBadges(s)+(reason?(' <span class="muted small" title="'+esc(reason)+'">· Reason: '+esc(reason.length>40?reason.slice(0,40)+"…":reason)+'</span>'):"")+(s.edited&&!reason?' <span class="muted small" title="Edited (reason hidden or system)">· Edited</span>':"")+'</div>'+
        '<div>'+evBtn+'</div>'+
      '</div>';
    var evb = li.querySelector("[data-ev]");
    if (evb) evb.addEventListener("click", function(){ toggleEvents(li, s, evb); });
    return li;
  }

  async function toggleEvents(li, s, btn) {
    var existing = li.querySelector(".ev-panel");
    if (existing) { existing.remove(); btn.textContent = "Events ("+s.event_ids.length+")"; return; }
    btn.textContent = "Loading…"; btn.disabled = true;
    try {
      // fetch underlying events for this staff & day via old list endpoint (kept working), filter to event_ids
      var p = new URLSearchParams({ staff_id: String(s.staff_id), start: s.shift_date, end: s.shift_date });
      var r = await FA.request("/api/admin/attendance?" + p.toString());
      if (!r.ok) throw new Error(await FA.readError(r));
      var all = await r.json();
      var ids = {}; (s.event_ids||[]).forEach(function(id){ ids[id]=true; });
      var evs = all.filter(function(e){ return ids[e.id]; });
      var panel = document.createElement("div"); panel.className="ev-panel";
      var table = document.createElement("table"); table.innerHTML = '<thead><tr><th>ID</th><th>Type</th><th>Local time</th><th>Method</th><th>Edited by</th><th>Reason</th><th>Action</th></tr></thead>';
      var tb = document.createElement("tbody");
      evs.forEach(function(ev){
        var tr = document.createElement("tr");
        var reason = humanReason(ev.edit_reason);
        tr.innerHTML = '<td>'+ev.id+'</td><td>'+esc(ev.type)+'</td><td>'+esc(ev.local_time||"")+'</td><td>'+esc(ev.method||"")+'</td><td>'+esc(ev.edited_by==null?"—":ev.edited_by)+'</td><td>'+(reason?('<span title="'+esc(ev.edit_reason)+'">'+esc(reason)+'</span>'):(ev.edit_reason?'<span class="muted">—</span>':'—'))+'</td>';
        var act = document.createElement("td");
        var b = document.createElement("button"); b.type="button"; b.className="button ghost"; b.textContent="Edit";
        b.addEventListener("click", function(){ openEdit(ev); });
        act.appendChild(b); tr.appendChild(act); tb.appendChild(tr);
      });
      table.appendChild(tb); panel.appendChild(table); li.appendChild(panel);
      btn.textContent = "Hide events";
    } catch(e){ setMsg("Could not load events: "+(e.message||e),"error"); btn.textContent = "Events ("+s.event_ids.length+")"; }
    finally { btn.disabled = false; }
  }

  async function load(resetPage) {
    if (resetPage) state.page = 1;
    setMsg("Loading…","info"); daysEl.innerHTML = ""; pager.hidden = true;
    renderChips();
    try {
      var qs = buildParams(true).toString();
      var r = await FA.request("/api/admin/attendance/sessions?" + qs);
      if (!r.ok) throw new Error(await FA.readError(r));
      var body = await r.json(); state.last = body;
      var count = 0; (body.days||[]).forEach(function(d){ count += (d.sessions||[]).length; });
      matchEl.textContent = count + " matching session(s) on this page · days " + body.from + " to " + body.to + " (page " + body.page + ", " + body.days_per_page + " day(s)/page, " + body.total_days + " day(s) total). Timezone: " + (body.timezone||"UTC") + ".";
      (body.days||[]).forEach(function(d){ daysEl.appendChild(renderDay(d)); });
      if (!(body.days||[]).length) daysEl.innerHTML = '<p class="muted">No days in range.</p>';
      var maxPage = Math.max(1, Math.ceil((body.total_days||1) / (body.days_per_page||7)));
      pager.hidden = maxPage <= 1;
      pageInfo.textContent = "Page " + body.page + " of " + maxPage;
      prevBtn.disabled = body.page <= 1; nextBtn.disabled = body.page >= maxPage;
      setMsg("");
    } catch(e){ if(e&&e.message&&e.message.indexOf("session")!==-1) return; setMsg("Could not load sessions: "+(e.message||e),"error"); }
  }

  function openEdit(item) {
    editor.hidden = false; setErr("");
    document.getElementById("editor-title").textContent = item ? ("Correct record #" + item.id) : "Manual attendance record";
    editId.value = item ? item.id : "";
    editStaff.value = item ? item.staff_id : "";
    editStaff.disabled = !!item;
    editType.value = item ? item.type : "in";
    editTime.value = item ? String(item.local_time).slice(0,16) : "";
    editReason.value = "";
    editReason.focus(); editor.scrollIntoView({behavior:"smooth", block:"start"});
  }

  editForm.addEventListener("submit", async function(e){
    e.preventDefault(); setErr("");
    var reason = editReason.value.trim();
    if (!reason) { setErr("Reason is required."); editReason.focus(); return; }
    var ts = editTime.value; if (!ts) { setErr("Local time is required."); return; }
    var id = editId.value;
    try {
      var r;
      if (id) {
        r = await FA.request("/api/admin/attendance/" + id, { method:"PATCH", headers:{"Content-Type":"application/json"}, body: JSON.stringify({type: editType.value, timestamp: ts, reason: reason}) });
      } else {
        var sid = editStaff.value; if (!sid) { setErr("Staff ID is required for a new record."); return; }
        r = await FA.request("/api/admin/attendance", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({staff_id: Number(sid), type: editType.value, timestamp: ts, reason: reason}) });
      }
      if (!r.ok) throw new Error(await FA.readError(r));
      editor.hidden = true; setMsg(id ? "Record corrected (reason recorded)." : "Manual record added (reason recorded).", "ok");
      load(false);
    } catch(e2){ if(e2&&e2.message&&e2.message.indexOf("session")!==-1) return; setErr(e2.message||String(e2)); }
  });

  document.getElementById("add-record").addEventListener("click", function(){ openEdit(null); });
  document.getElementById("cancel-edit").addEventListener("click", function(){ editor.hidden = true; });
  form.addEventListener("submit", function(e){ e.preventDefault(); load(true); });
  prevBtn.addEventListener("click", function(){ state.page = Math.max(1, (state.page||1)-1); load(false); });
  nextBtn.addEventListener("click", function(){ state.page = (state.page||1)+1; load(false); });
  document.querySelectorAll("[data-preset]").forEach(function(b){ b.addEventListener("click", function(){ setPreset(b.getAttribute("data-preset")); load(true); }); });
  document.getElementById("export-csv").addEventListener("click", async function(){
    try {
      var qs = buildParams(false).toString();
      var r = await FA.request("/api/admin/attendance/sessions.csv?" + qs, { headers: {"Accept":"text/csv"} });
      if (!r.ok) throw new Error(await FA.readError(r));
      var blob = await r.blob(); var a=document.createElement("a"); a.href=URL.createObjectURL(blob);
      a.download="attendance-sessions.csv"; document.body.appendChild(a); a.click(); a.remove();
    } catch(e){ setMsg("CSV download failed: "+(e.message||e),"error"); }
  });
  document.getElementById("monthly").addEventListener("click", async function(){
    try {
      var d=new Date(), y=d.getFullYear(), m=d.getMonth()+1;
      var r = await FA.request("/api/admin/attendance/report/monthly.csv?year="+y+"&month="+m, { headers: {"Accept":"text/csv"} });
      if (!r.ok) throw new Error(await FA.readError(r));
      var blob = await r.blob(); var a=document.createElement("a"); a.href=URL.createObjectURL(blob);
      a.download="monthly-report.csv"; document.body.appendChild(a); a.click(); a.remove();
    } catch(e){ setMsg("Download failed: "+(e.message||e),"error"); }
  });

  (async function(){
    var s = await FA.requireSession();
    FA.showAdminName(s.admin.username); FA.wireLogout(); FA.startSessionWatch();
    setPreset("last7"); load(true);
  })();
})();
