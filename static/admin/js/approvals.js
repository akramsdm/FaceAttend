(function(){
  "use strict";
  function msg(t,k){ var m=document.getElementById("message"); m.textContent=t||""; m.className="message"+(k?" "+k:""); }
  async function loadReqs(){
    var r=await FA.request("/api/admin/approvals?status=pending");
    var tb=document.getElementById("req-rows"); tb.innerHTML="";
    if(!r.ok){ tb.innerHTML='<tr><td colspan="7">Failed to load.</td></tr>'; return; }
    var data=await r.json();
    if(!data.length){ tb.innerHTML='<tr><td colspan="7" class="muted">No pending requests.</td></tr>'; return; }
    data.forEach(function(item){
      var tr=document.createElement("tr");
      var note=document.createElement("input"); note.type="text"; note.placeholder="Note (optional)"; note.size=18;
      function act(kind){
        return async function(){
          var rr=await FA.request("/api/admin/approvals/"+item.id+"/"+kind,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({note:note.value||null})});
          if(!rr.ok) msg(await FA.readError(rr),"error"); else { msg(kind==="approve"?"Approved (attendance created).":"Rejected.","success"); load(); }
        };
      }
      var bA=document.createElement("button"); bA.type="button"; bA.className="button small"; bA.textContent="Approve"; bA.addEventListener("click",act("approve"));
      var bR=document.createElement("button"); bR.type="button"; bR.className="button danger small"; bR.textContent="Reject"; bR.style.marginLeft=".3rem"; bR.addEventListener("click",act("reject"));
      var cell=document.createElement("td"); cell.appendChild(note); cell.appendChild(document.createTextNode(" ")); cell.appendChild(bA); cell.appendChild(bR);
      tr.innerHTML='<td>'+ (item.staff_name||item.staff_id) +'</td><td>'+ (item.department||"—") +'</td><td>'+item.action+'</td><td>'+item.reason_code+'</td><td>'+item.requested_at+'</td><td>'+ (item.today_shift||"—") +'</td>';
      tr.appendChild(cell); tb.appendChild(tr);
    });
  }
  async function loadMissing(){
    var r=await FA.request("/api/admin/approvals/missing-clockout");
    var tb=document.getElementById("miss-rows"); tb.innerHTML="";
    if(!r.ok){ tb.innerHTML='<tr><td colspan="5">Failed to load.</td></tr>'; return; }
    var data=await r.json();
    if(!data.length){ tb.innerHTML='<tr><td colspan="5" class="muted">No missing clock-outs.</td></tr>'; return; }
    data.forEach(function(item){
      var tr=document.createElement("tr");
      var ts=document.createElement("input"); ts.type="datetime-local"; ts.setAttribute("aria-label","Real clock-out time");
      var reason=document.createElement("input"); reason.type="text"; reason.placeholder="Reason (required)"; reason.required=true; reason.size=18;
      var btn=document.createElement("button"); btn.type="button"; btn.className="button small"; btn.textContent="Save real time";
      btn.addEventListener("click", async function(){
        if(!reason.value.trim()){ msg("Reason is required for a missing clock-out correction.","error"); reason.focus(); return; }
        if(!ts.value){ msg("Enter the real clock-out time.","error"); ts.focus(); return; }
        var rr=await FA.request("/api/admin/approvals/missing-clockout/"+item.id+"/close",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({timestamp:ts.value, reason:reason.value})});
        if(!rr.ok) msg(await FA.readError(rr),"error"); else { msg("Missing clock-out corrected (row kept).","success"); load(); }
      });
      var cell=document.createElement("td"); cell.appendChild(ts); cell.appendChild(document.createTextNode(" ")); cell.appendChild(reason); cell.appendChild(document.createTextNode(" ")); cell.appendChild(btn);
      tr.innerHTML='<td>'+ (item.staff_name||item.staff_id) +'</td><td>'+ (item.department||"—") +'</td><td>'+ (item.shift_date||"—") +'</td><td>'+item.timestamp+'</td>';
      tr.appendChild(cell); tb.appendChild(tr);
    });
  }
  async function load(){ await Promise.all([loadReqs(), loadMissing()]); }
  async function boot(){
    FA.wireLogout(); var me=await FA.requireSession(); FA.showAdminName(me.admin.username); FA.startSessionWatch();
    document.getElementById("refresh").addEventListener("click", load);
    load().catch(function(e){ msg(e.message,"error"); });
  }
  document.addEventListener("DOMContentLoaded", function(){ boot().catch(function(e){ msg(e.message,"error"); }); });
})();
