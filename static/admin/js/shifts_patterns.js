(function () {
  "use strict";
  var DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
  function $(id){ return document.getElementById(id); }
  function msg(t,k){ var m=$("message"); m.textContent=t||""; m.className="message"+(k?" "+k:""); }
  function ferr(t){ $("form-error").textContent=t||""; }
  function cerr(t){ $("copy-error").textContent=t||""; }
  function buildGrid(values){
    var tb=$("grid-body"); tb.innerHTML="";
    for(var wd=0; wd<7; wd++){
      var v = (values||[]).find(function(d){return d.weekday===wd;}) || {weekday:wd,enabled:wd<5,start_time:"08:00",end_time:"17:00",overnight:false};
      var tr=document.createElement("tr");
      tr.innerHTML = '<th scope="row">'+DAYS[wd]+'</th>'+
        '<td><input type="checkbox" data-f="enabled" data-wd="'+wd+'" '+(v.enabled?'checked':'')+' aria-label="'+DAYS[wd]+' enabled"></td>'+
        '<td><input type="text" data-f="start_time" data-wd="'+wd+'" value="'+(v.start_time||'08:00')+'" size="6" placeholder="08:00"></td>'+
        '<td><input type="text" data-f="end_time" data-wd="'+wd+'" value="'+(v.end_time||'17:00')+'" size="6" placeholder="17:00"></td>'+
        '<td><input type="checkbox" data-f="overnight" data-wd="'+wd+'" '+(v.overnight?'checked':'')+' aria-label="'+DAYS[wd]+' overnight"></td>';
      tb.appendChild(tr);
    }
  }
  function readGrid(){
    var days=[];
    for(var wd=0; wd<7; wd++){
      function q(f){ return document.querySelector('[data-wd="'+wd+'"][data-f="'+f+'"]'); }
      days.push({weekday:wd, enabled:q("enabled").checked, start_time:q("start_time").value.trim(),
        end_time:q("end_time").value.trim(), overnight:q("overnight").checked});
    }
    return days;
  }
  function fillPattern(p){
    $("pattern-id").value=p?p.id:""; $("p-name").value=p?p.name:"";
    $("p-early").value=p?p.early_window_min:30; $("p-grace").value=p?p.grace_min:10; $("p-break").value=p?p.break_min:0;
    buildGrid(p?p.days:null);
    $("cancel-edit").hidden=!p; $("save-pattern").textContent=p?"Save changes":"Save pattern";
  }
  function renderRows(list, myAdmin){
    var tb=$("pattern-rows"); tb.innerHTML="";
    if(!list.length){ tb.innerHTML='<tr><td colspan="4" class="muted">No patterns.</td></tr>'; return; }
    list.forEach(function(p){
      var tr=document.createElement("tr");
      var owner = p.owner_staff_id ? ("personal staff #"+p.owner_staff_id) : (p.is_default?"default (shared)":"shared");
      var acts = document.createElement("td");
      // edit/delete only when sensible (shared defaults editable by admin; owned/shared manager rules enforced server-side)
      var bE=document.createElement("button"); bE.type="button"; bE.className="button secondary small"; bE.textContent="Edit";
      bE.addEventListener("click", function(){ fillPattern(p); $("p-name").focus(); window.scrollTo(0,0); });
      var bD=document.createElement("button"); bD.type="button"; bD.className="button danger small"; bD.textContent="Delete";
      bD.style.marginLeft="0.4rem";
      bD.addEventListener("click", async function(){
        if(!confirm("Delete pattern '"+p.name+"'?")) return;
        var r=await FA.request("/api/admin/shifts/patterns/"+p.id,{method:"DELETE"});
        if(!r.ok){ msg(await FA.readError(r),"error"); } else { msg("Pattern deleted.","success"); load(); }
      });
      acts.appendChild(bE); acts.appendChild(bD);
      tr.innerHTML='<td>'+p.name+'</td><td>'+owner+'</td><td>'+p.early_window_min+'/'+p.grace_min+'/'+p.break_min+' min</td>';
      tr.appendChild(acts); tb.appendChild(tr);
    });
  }
  async function loadStaffAndPatterns(){
    var [pr,sr] = await Promise.all([FA.request("/api/admin/shifts/patterns"), FA.request("/api/admin/staff")]);
    var pats = pr.ok?await pr.json():[]; var staff = sr.ok?await sr.json():[];
    var ps=$("copy-pattern"); ps.innerHTML=""; pats.forEach(function(p){ var o=document.createElement("option"); o.value=p.id; o.textContent=p.name+(p.owner_staff_id?" (personal)":""); ps.appendChild(o); });
    var ss=$("copy-staff"); ss.innerHTML=""; staff.filter(function(s){return !s.anonymised;}).forEach(function(s){ var o=document.createElement("option"); o.value=s.id; o.textContent=s.staff_code+" - "+s.name+(s.department?" ("+s.department+")":""); ss.appendChild(o); });
    renderRows(pats);
  }
  async function load(){ try{ await loadStaffAndPatterns(); }catch(e){ msg(e.message,"error"); } }
  async function boot(){
    FA.wireLogout(); var me=await FA.requireSession(); FA.showAdminName(me.admin.username); FA.startSessionWatch();
    buildGrid(null); load();
    $("cancel-edit").addEventListener("click", function(){ fillPattern(null); ferr(""); });
    $("pattern-form").addEventListener("submit", async function(ev){
      ev.preventDefault(); ferr("");
      var id=$("pattern-id").value;
      var payload={name:$("p-name").value.trim(), early_window_min:Number($("p-early").value), grace_min:Number($("p-grace").value),
        break_min:Number($("p-break").value), days:readGrid()};
      if(!payload.name){ ferr("Name is required."); return; }
      var path = id?("/api/admin/shifts/patterns/"+id):"/api/admin/shifts/patterns";
      var method = id?"PUT":"POST";
      var r=await FA.request(path,{method:method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      if(!r.ok){ ferr(await FA.readError(r)); return; }
      msg(id?"Pattern updated.":"Pattern created.","success"); fillPattern(null); load();
    });
    $("copy-form").addEventListener("submit", async function(ev){
      ev.preventDefault(); cerr("");
      var pid=$("copy-pattern").value, sid=$("copy-staff").value;
      if(!pid||!sid){ cerr("Choose a pattern and a staff member."); return; }
      var r=await FA.request("/api/admin/shifts/patterns/"+pid+"/customise/"+sid,{method:"POST"});
      if(!r.ok){ cerr(await FA.readError(r)); return; }
      msg("Personal copy created for the selected person.","success"); load();
    });
  }
  document.addEventListener("DOMContentLoaded", function(){ boot().catch(function(e){ msg(e.message,"error"); }); });
})();
