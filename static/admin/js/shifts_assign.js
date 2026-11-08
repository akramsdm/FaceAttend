(function(){
  "use strict";
  function $(id){return document.getElementById(id);}
  function msg(t,k){var m=$("message");m.textContent=t||"";m.className="message"+(k?" "+k:"");}
  function err(t){$("a-error").textContent=t||"";}
  var staff=[], patterns=[];
  async function load(){
    var [pr,sr]=await Promise.all([FA.request("/api/admin/shifts/patterns"), FA.request("/api/admin/staff")]);
    patterns=pr.ok?await pr.json():[]; staff=(sr.ok?await sr.json():[]).filter(function(s){return !s.anonymised;});
    var ps=$("a-pattern"); ps.innerHTML="";
    patterns.forEach(function(p){var o=document.createElement("option");o.value=p.id;o.textContent=p.name+(p.owner_staff_id?" (personal)":"");ps.appendChild(o);});
    var box=$("staff-checks"); box.innerHTML="";
    staff.forEach(function(s){
      var lab=document.createElement("label"); lab.style.display="block";
      var cb=document.createElement("input"); cb.type="checkbox"; cb.value=s.id; cb.style.marginRight=".4rem";
      lab.appendChild(cb); lab.appendChild(document.createTextNode(s.staff_code+" - "+s.name+(s.department?" ("+s.department+")":"")));
      box.appendChild(lab);
    });
    var today=new Date().toISOString().slice(0,10); $("a-effective").value=today;
  }
  function selectedIds(){ return Array.from($("staff-checks").querySelectorAll("input:checked")).map(function(c){return Number(c.value);}); }
  function buildSummary(){
    var pid=$("a-pattern").value, eff=$("a-effective").value, dept=$("a-dept").value.trim();
    var ids=selectedIds(); var p=patterns.find(function(x){return String(x.id)===String(pid);});
    var parts=[];
    parts.push("Pattern: "+(p?p.name:pid));
    parts.push("Effective: "+(eff||"(choose date)"));
    if(dept) parts.push("Department (bulk): "+dept);
    parts.push("Selected staff: "+(ids.length?ids.length:0));
    parts.push("Note: each selected person's previous assignment ends the day before the effective date.");
    return parts.join(" | ");
  }
  async function boot(){
    FA.wireLogout(); var me=await FA.requireSession(); FA.showAdminName(me.admin.username); FA.startSessionWatch();
    await load();
    $("preview-btn").addEventListener("click", function(){ err(""); $("summary").textContent=buildSummary(); $("confirm-btn").disabled=false; });
    $("assign-form").addEventListener("submit", async function(ev){
      ev.preventDefault(); err("");
      var payload={pattern_id:Number($("a-pattern").value), effective_from:$("a-effective").value,
        staff_ids:selectedIds(), department:$("a-dept").value.trim()||null};
      if(!payload.effective_from){ err("Effective date is required."); return; }
      if(!payload.staff_ids.length && !payload.department){ err("Select staff or enter a department."); return; }
      $("confirm-btn").disabled=true;
      var r=await FA.request("/api/admin/shifts/assignments",{method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      if(!r.ok){ err(await FA.readError(r)); $("confirm-btn").disabled=false; return; }
      var d=await r.json(); msg("Assigned to "+d.assigned+" staff member(s). Previous assignments ended the day before "+payload.effective_from+".","success");
      $("confirm-btn").disabled=true;
    });
  }
  document.addEventListener("DOMContentLoaded", function(){ boot().catch(function(e){ err(e.message); }); });
})();
