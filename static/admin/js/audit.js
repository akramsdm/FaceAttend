(function () {
  "use strict";
  var rows = document.getElementById("rows");
  var msg = document.getElementById("message");
  var info = document.getElementById("page-info");
  var offset = 0, limit = 50, total = 0;
  function setMsg(t,k){ msg.textContent=t||""; msg.className="message"+(k?" "+k:""); }
  function td(t){ var e=document.createElement("td"); e.textContent = t==null||t===""?"-":String(t); return e; }

  async function load() {
    setMsg("Loading…","info");
    try {
      var r = await FA.request("/api/admin/audit?limit="+limit+"&offset="+offset);
      if (!r.ok) throw new Error(await FA.readError(r));
      var d = await r.json(); total = d.total;
      rows.innerHTML = "";
      info.textContent = "Showing "+(d.items.length? (offset+1):0)+"–"+(offset+d.items.length)+" of "+total+".";
      if (!d.items.length) { rows.innerHTML='<tr><td colspan="5">No audit entries.</td></tr>'; setMsg(""); return; }
      d.items.forEach(function(it){
        var tr=document.createElement("tr");
        tr.appendChild(td(it.timestamp)); tr.appendChild(td(it.admin_id)); tr.appendChild(td(it.action)); tr.appendChild(td(it.target)); tr.appendChild(td(it.detail));
        rows.appendChild(tr);
      });
      document.getElementById("prev").disabled = offset===0;
      document.getElementById("next").disabled = offset+limit >= total;
      setMsg("");
    } catch(e){ if(e&&e.message&&e.message.indexOf("session")!==-1) return; setMsg("Could not load audit log: "+e.message,"error"); }
  }
  document.getElementById("prev").addEventListener("click", function(){ offset = Math.max(0, offset-limit); load(); });
  document.getElementById("next").addEventListener("click", function(){ if(offset+limit<total){ offset+=limit; load(); } });
  (async function(){ var s=await FA.requireSession(); FA.showAdminName(s.admin.username); FA.wireLogout(); FA.startSessionWatch(); load(); })();
})();
