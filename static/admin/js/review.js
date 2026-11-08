(function () {
  "use strict";
  var items = document.getElementById("items");
  var msg = document.getElementById("message");
  function setMsg(t,k){ msg.textContent=t||""; msg.className="message"+(k?" "+k:""); }

  async function loadStaffOptions() {
    try {
      var r = await FA.request("/api/admin/staff?active=true");
      if (!r.ok) return [];
      return await r.json();
    } catch(e){ return []; }
  }

  function staffSelect(staff, selectedId) {
    var sel = document.createElement("select");
    sel.setAttribute("aria-label","Assign to staff member");
    var def = document.createElement("option"); def.value=""; def.textContent="Select staff…"; sel.appendChild(def);
    staff.forEach(function(s){
      var o=document.createElement("option"); o.value=String(s.id); o.textContent=s.name+" ("+s.staff_code+")"; if(s.id===selectedId) o.selected=true; sel.appendChild(o);
    });
    return sel;
  }

  async function load() {
    setMsg("Loading…","info");
    var staff = await loadStaffOptions();
    try {
      var r = await FA.request("/api/admin/review?resolved=false");
      if (!r.ok) throw new Error(await FA.readError(r));
      var data = await r.json();
      items.innerHTML = "";
      if (!data.length) { items.textContent = "No unresolved review items."; setMsg(""); return; }
      data.forEach(function(item){
        var card = document.createElement("article"); card.className="review-item";
        var h = document.createElement("h3"); h.textContent = "Attempt #"+item.id+" — "+item.outcome; card.appendChild(h);
        var meta = document.createElement("p"); meta.className="muted small";
        meta.textContent = "Time: "+item.timestamp+" · Kiosk: "+item.kiosk_id+" · Best candidate: "+(item.best_staff_name||"none")+(item.best_staff_id?" (#"+item.best_staff_id+")":"")+".";
        card.appendChild(meta);
        if (item.frame_path) {
          var img = document.createElement("img"); img.alt = "Review frame for attempt "+item.id; img.src = "/api/admin/review/"+item.id+"/frame"; card.appendChild(img);
        } else {
          var nf = document.createElement("p"); nf.className="muted"; nf.textContent="No frame available."; card.appendChild(nf);
        }
        var sel = staffSelect(staff, item.best_staff_id);
        var reason = document.createElement("input"); reason.type="text"; reason.maxLength=500; reason.placeholder="Reason (required)"; reason.setAttribute("aria-label","Reason for resolution");
        var row = document.createElement("div"); row.className="button-row";
        var assign = document.createElement("button"); assign.type="button"; assign.className="button"; assign.textContent="Assign";
        var dismiss = document.createElement("button"); dismiss.type="button"; dismiss.className="button secondary"; dismiss.textContent="Dismiss";
        async function resolve(action){
          var rv = reason.value.trim();
          if (!rv) { setMsg("Reason is required to resolve attempt #"+item.id+".","error"); reason.focus(); return; }
          var payload = {action: action, reason: rv};
          if (action === "assign") { if(!sel.value){ setMsg("Select a staff member to assign.","error"); sel.focus(); return; } payload.staff_id = Number(sel.value); }
          assign.disabled = dismiss.disabled = true;
          try {
            var rr = await FA.request("/api/admin/review/"+item.id+"/resolve",{method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
            if (!rr.ok) throw new Error(await FA.readError(rr));
            setMsg("Attempt #"+item.id+" "+action+"ed. Frame deleted.","ok"); load();
          } catch(e){ assign.disabled=dismiss.disabled=false; if(e&&e.message&&e.message.indexOf("session")!==-1) return; setMsg(e.message,"error"); }
        }
        assign.addEventListener("click", function(){ resolve("assign"); });
        dismiss.addEventListener("click", function(){ resolve("dismiss"); });
        var f1=document.createElement("div"); f1.className="field"; var l1=document.createElement("label"); l1.textContent="Assign to"; l1.appendChild(sel); f1.appendChild(l1);
        var f2=document.createElement("div"); f2.className="field"; var l2=document.createElement("label"); l2.textContent="Reason (required)"; l2.appendChild(reason); f2.appendChild(l2);
        row.appendChild(assign); row.appendChild(dismiss);
        card.appendChild(f1); card.appendChild(f2); card.appendChild(row);
        items.appendChild(card);
      });
      setMsg("");
    } catch(e){ if(e&&e.message&&e.message.indexOf("session")!==-1) return; setMsg("Could not load review queue: "+e.message,"error"); }
  }

  (async function(){ var s=await FA.requireSession(); FA.showAdminName(s.admin.username); FA.wireLogout(); FA.startSessionWatch(); document.getElementById("refresh").addEventListener("click", load); load(); })();
})();
