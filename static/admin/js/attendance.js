(function () {
  "use strict";
  var rows = document.getElementById("rows");
  var count = document.getElementById("count");
  var msg = document.getElementById("message");
  var editor = document.getElementById("editor");
  var editForm = document.getElementById("edit-form");
  var editId = document.getElementById("edit-id");
  var editStaff = document.getElementById("edit-staff");
  var editType = document.getElementById("edit-type");
  var editTime = document.getElementById("edit-time");
  var editReason = document.getElementById("edit-reason");
  var editError = document.getElementById("edit-error");

  function setMsg(t, k) { msg.textContent = t || ""; msg.className = "message" + (k ? " " + k : ""); }
  function setErr(t) { editError.textContent = t || ""; }
  function td(t) { var e = document.createElement("td"); e.textContent = t == null || t === "" ? "-" : String(t); return e; }
  function qs() {
    var p = new URLSearchParams();
    ["start","end","staff_id","department"].forEach(function(id){
      var v = document.getElementById(id.replace("_","-")).value.trim();
      if (v) p.set(id, v);
    });
    return p.toString();
  }

  async function load() {
    setMsg("Loading…","info");
    try {
      var q = qs();
      var r = await FA.request("/api/admin/attendance" + (q ? "?" + q : ""));
      if (!r.ok) throw new Error(await FA.readError(r));
      var data = await r.json();
      rows.innerHTML = "";
      count.textContent = data.length + " record(s).";
      if (!data.length) { rows.innerHTML = '<tr><td colspan="9">No records found.</td></tr>'; setMsg(""); return; }
      data.forEach(function(item){
        var tr = document.createElement("tr");
        tr.appendChild(td(item.id));
        var nc = td(""); var a = document.createElement("a"); a.href = "person.html?id=" + encodeURIComponent(item.staff_id);
        a.textContent = (item.staff_name||"") + " (#" + item.staff_id + (item.staff_code? ", "+item.staff_code:"") + ")"; nc.appendChild(a); tr.appendChild(nc);
        tr.appendChild(td(item.department));
        tr.appendChild(td(item.type));
        tr.appendChild(td(item.local_time));
        tr.appendChild(td(item.method));
        tr.appendChild(td(item.edited_by));
        tr.appendChild(td(item.edit_reason));
        var act = document.createElement("td");
        var b = document.createElement("button");
        b.type="button"; b.className="button ghost"; b.textContent="Edit";
        b.addEventListener("click", function(){ openEdit(item); });
        act.appendChild(b); tr.appendChild(act);
        rows.appendChild(tr);
      });
      setMsg("");
    } catch(e){ if(e&&e.message&&e.message.indexOf("session")!==-1) return; setMsg("Could not load attendance: "+e.message,"error"); }
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
    editReason.focus();
  }

  async function download(path) {
    try {
      var q = qs();
      var r = await FA.request(path + (q ? "?" + q : ""), { headers: { "Accept": "text/csv" }});
      if (!r.ok) throw new Error(await FA.readError(r));
      var blob = await r.blob();
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = path.indexOf("monthly")!==-1 ? "monthly-report.csv" : "attendance-export.csv";
      document.body.appendChild(a); a.click(); a.remove();
    } catch(e){ setMsg("Download failed: "+e.message,"error"); }
  }

  editForm.addEventListener("submit", async function(e){
    e.preventDefault(); setErr("");
    var reason = editReason.value.trim();
    if (!reason) { setErr("Reason is required."); editReason.focus(); return; }
    var ts = editTime.value;
    if (!ts) { setErr("Local time is required."); return; }
    var id = editId.value;
    try {
      var r;
      if (id) {
        r = await FA.request("/api/admin/attendance/" + id, {
          method: "PATCH", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({type: editType.value, timestamp: ts, reason: reason})
        });
      } else {
        var sid = editStaff.value;
        if (!sid) { setErr("Staff ID is required for a new record."); return; }
        r = await FA.request("/api/admin/attendance", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({staff_id: Number(sid), type: editType.value, timestamp: ts, reason: reason})
        });
      }
      if (!r.ok) throw new Error(await FA.readError(r));
      editor.hidden = true; setMsg(id ? "Record corrected (reason recorded)." : "Manual record added (reason recorded).", "ok");
      load();
    } catch(e2){ if(e2&&e2.message&&e2.message.indexOf("session")!==-1) return; setErr(e2.message); }
  });

  document.getElementById("add-record").addEventListener("click", function(){ openEdit(null); });
  document.getElementById("cancel-edit").addEventListener("click", function(){ editor.hidden = true; });
  document.getElementById("filters-form").addEventListener("submit", function(e){ e.preventDefault(); load(); });
  document.getElementById("export").addEventListener("click", function(){ download("/api/admin/attendance/export.csv"); });
  document.getElementById("monthly").addEventListener("click", function(){
    var d = new Date(); var y = d.getFullYear(), m = d.getMonth()+1;
    download("/api/admin/attendance/report/monthly.csv?year="+y+"&month="+m + (qs() ? "" : ""));
  });

  (async function(){
    var s = await FA.requireSession();
    FA.showAdminName(s.admin.username); FA.wireLogout(); FA.startSessionWatch();
    load();
  })();
})();
