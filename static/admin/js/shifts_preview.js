(function(){
  "use strict";
  function $(id){return document.getElementById(id);}
  function err(t){$("pr-error").textContent=t||"";}
  async function load(){
    var sr=await FA.request("/api/admin/staff"); var staff=(sr.ok?await sr.json():[]).filter(function(s){return !s.anonymised;});
    var ss=$("pr-staff"); ss.innerHTML="";
    staff.forEach(function(s){var o=document.createElement("option");o.value=s.id;o.textContent=s.staff_code+" - "+s.name+(s.department?" ("+s.department+")":"");ss.appendChild(o);});
    // default to Monday of this week
    var d=new Date(); var day=(d.getDay()+6)%7; d.setDate(d.getDate()-day); $("pr-start").value=d.toISOString().slice(0,10);
  }
  function fmt(dt){ if(!dt) return "—"; return dt.replace("T"," ").slice(0,16); }
  async function boot(){
    FA.wireLogout(); var me=await FA.requireSession(); FA.showAdminName(me.admin.username); FA.startSessionWatch();
    await load();
    $("prev-form").addEventListener("submit", async function(ev){
      ev.preventDefault(); err("");
      var sid=$("pr-staff").value, start=$("pr-start").value;
      if(!sid||!start){ err("Staff and week start are required."); return; }
      var endD=new Date(start); endD.setDate(endD.getDate()+6); var end=endD.toISOString().slice(0,10);
      var r=await FA.request("/api/admin/shifts/staff/"+sid+"/resolved?start="+start+"&end="+end);
      if(!r.ok){ err(await FA.readError(r)); return; }
      var data=await r.json(); var tb=$("prev-rows"); tb.innerHTML="";
      data.items.forEach(function(it){
        var tr=document.createElement("tr");
        if(!it.shift){ tr.innerHTML='<td>'+it.date+'</td><td colspan="5" class="muted">No shift (day off / not assigned)</td>'; }
        else { var sh=it.shift; tr.innerHTML='<td>'+it.date+'</td><td>'+fmt(sh.start)+'</td><td>'+fmt(sh.end)+'</td><td>'+(sh.overnight?'Yes':'No')+'</td><td>'+sh.source+'</td><td>'+sh.early_window+'/'+sh.grace+'/'+sh.break_min+' min</td>'; }
        tb.appendChild(tr);
      });
    });
  }
  document.addEventListener("DOMContentLoaded", function(){ boot().catch(function(e){ err(e.message); }); });
})();
