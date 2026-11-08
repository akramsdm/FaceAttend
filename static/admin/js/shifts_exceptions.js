(function(){
  "use strict";
  function $(id){return document.getElementById(id);}
  function msg(t,k){var m=$("message");m.textContent=t||"";m.className="message"+(k?" "+k:"");}
  function err(t){$("ex-error").textContent=t||"";}
  async function load(){
    var sr=await FA.request("/api/admin/staff"); var staff=(sr.ok?await sr.json():[]).filter(function(s){return !s.anonymised;});
    var ss=$("ex-staff"); ss.innerHTML="";
    staff.forEach(function(s){var o=document.createElement("option");o.value=s.id;o.textContent=s.staff_code+" - "+s.name+(s.department?" ("+s.department+")":"");ss.appendChild(o);});
    $("ex-date").value=new Date().toISOString().slice(0,10);
  }
  function toggle(){ $("custom-times").hidden = $("ex-kind").value!=="custom_hours"; }
  async function boot(){
    FA.wireLogout(); var me=await FA.requireSession(); FA.showAdminName(me.admin.username); FA.startSessionWatch();
    await load(); toggle();
    $("ex-kind").addEventListener("change", toggle);
    $("ex-form").addEventListener("submit", async function(ev){
      ev.preventDefault(); err("");
      var payload={staff_id:Number($("ex-staff").value), date:$("ex-date").value, kind:$("ex-kind").value,
        reason:$("ex-reason").value.trim(),
        start_time:$("ex-kind").value==="custom_hours"?($("ex-start").value.trim()||null):null,
        end_time:$("ex-kind").value==="custom_hours"?($("ex-end").value.trim()||null):null};
      if(!payload.reason){ err("Reason is required."); return; }
      var r=await FA.request("/api/admin/shifts/exceptions",{method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      if(!r.ok){ err(await FA.readError(r)); return; }
      msg("Exception saved ("+payload.kind+" for "+payload.date+"). It overrides the pattern for that day.","success");
    });
  }
  document.addEventListener("DOMContentLoaded", function(){ boot().catch(function(e){ err(e.message); }); });
})();
