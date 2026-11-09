(function () {
  "use strict";
  var form = document.getElementById("settings-form");
  var msg = document.getElementById("message");
  var err = document.getElementById("error");
  var keys = ["match_threshold","ambiguity_margin","cooldown_seconds","work_start","grace_minutes","review_retention_days","spoof_challenge","missing_close_hours","followup_late_count","followup_period_days"];
  function setMsg(t,k){ msg.textContent=t||""; msg.className="message"+(k?" "+k:""); }
  function setErr(t){ err.textContent=t||""; }
  function setVal(el, v){ if(!el) return; if(el.tagName==="SELECT") el.value = (v===true||v==="true")?"true":"false"; else el.value = v; }
  function getVal(el){ if(el.tagName==="SELECT" && (el.value==="true"||el.value==="false")) return el.value==="true"; if(el.type==="number") return Number(el.value); return el.value; }

  async function load() {
    setMsg("Loading…","info"); setErr("");
    try {
      var r = await FA.request("/api/admin/settings");
      if (!r.ok) throw new Error(await FA.readError(r));
      var d = await r.json();
      keys.forEach(function(k){ setVal(document.getElementById(k), d[k]); });
      setMsg("");
    } catch(e){ if(e&&e.message&&e.message.indexOf("session")!==-1) return; setMsg("Could not load settings: "+e.message,"error"); }
  }

  form.addEventListener("submit", async function(e){
    e.preventDefault(); setErr(""); setMsg("Saving…","info");
    var payload = {};
    keys.forEach(function(k){ var el=document.getElementById(k); if(!el) return; var v=el.value; if(v==="") return; payload[k]= getVal(el); });
    try {
      var r = await FA.request("/api/admin/settings",{method:"PUT", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
      if (!r.ok) throw new Error(await FA.readError(r));
      var d = await r.json();
      keys.forEach(function(k){ setVal(document.getElementById(k), d[k]); });
      setMsg("Settings saved and applied (each change recorded in audit log).","ok");
    } catch(e2){ if(e2&&e2.message&&e2.message.indexOf("session")!==-1) return; setMsg(""); setErr(e2.message); }
  });

  (async function(){ var s=await FA.requireSession(); FA.showAdminName(s.admin.username); FA.wireLogout(); FA.startSessionWatch(); load(); })();
})();
