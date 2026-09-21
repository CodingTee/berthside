/* ==========================================================================
   ShipSync Review Console: inbox, verdict, field diff and command palette
   ========================================================================== */

(function(){
const $ = s => document.querySelector(s);
const API = "";
let state = {items:[], active:null, summary:null, detail:null};

const CATEGORIES = ["BL_COMPARISON","SI_REQUEST","INVOICE_QUERY","GENERAL","SPAM"];
const FIELDS_LABEL = {shipper:"Shipper",consignee:"Consignee",notify_party:"Notify party",
  port_of_loading:"Port of loading",port_of_discharge:"Port of discharge",
  container_count:"Container count",gross_weight_kg:"Gross weight (kg)"};

/* ---------- field severity: front-end heuristic ----------
   The API exposes no severity column, so we grade each mismatch by how much a
   wrong value would cost at customs / on the manifest. Flagged as a heuristic in
   the UI so it is never mistaken for the engine's own judgement. */
const FIELD_SEVERITY = {
  consignee:"high", notify_party:"high", shipper:"high",
  gross_weight_kg:"high", container_count:"high",
  port_of_loading:"med", port_of_discharge:"med"
};
const SEV_TITLE = {
  high:"Parties / cargo quantities: a wrong value blocks customs release",
  med :"Routing fields: a wrong port changes the voyage",
  low :"Supporting detail"
};
const sevOf = f => FIELD_SEVERITY[f] || "low";

/* ---------- token-level diff ----------
   Tokenise on word boundaries (keeping separators), then run an LCS so only the
   tokens that truly differ get marked: e.g. "CNNTG" vs "CNNTA" inside two
   otherwise identical port strings. */
function tokenize(v){
  return String(v==null?"":v).match(/[A-Za-z0-9]+(?:[.'’\/-][A-Za-z0-9]+)*|\s+|[^\sA-Za-z0-9]+/g) || [];
}
function lcsDiff(a,b){
  const n=a.length, m=b.length;
  if(!n||!m) return null;
  if(n*m>60000) return null;                       // pathological-input guard
  const dp=[];
  for(let i=0;i<=n;i++) dp.push(new Uint16Array(m+1));
  for(let i=n-1;i>=0;i--)
    for(let j=m-1;j>=0;j--)
      dp[i][j] = a[i]===b[j] ? dp[i+1][j+1]+1 : (dp[i+1][j]>=dp[i][j+1]?dp[i+1][j]:dp[i][j+1]);
  const out=[]; let i=0,j=0;
  while(i<n&&j<m){
    if(a[i]===b[j]){ out.push(["same",a[i]]); i++; j++; }
    else if(dp[i+1][j]>=dp[i][j+1]){ out.push(["del",a[i]]); i++; }
    else { out.push(["ins",b[j]]); j++; }
  }
  while(i<n) out.push(["del",a[i++]]);
  while(j<m) out.push(["ins",b[j++]]);
  return out;
}
/* Both cells are built from ONE diff so they stay symmetric:
   tokens found only on the SI side are struck through, tokens found only on the
   BL side are highlighted. Identical tokens stay plain on both sides. */
function diffCells(si,bl){
  const ops = lcsDiff(tokenize(si),tokenize(bl));
  if(!ops) return [ esc(si==null?"-":si), esc(bl==null?"-":bl) ];
  let L="",R="";
  for(let k=0;k<ops.length;k++){
    const t=ops[k][0], raw=ops[k][1], h=esc(raw);
    /* whitespace-only drift is invisible: keep the character, drop the mark */
    const blank=/^\s+$/.test(raw);
    if(t==="same"){ L+=h; R+=h; }
    else if(t==="del") L+= blank?h:'<span class="tk del">'+h+'</span>';
    else R+= blank?h:'<span class="tk ins">'+h+'</span>';
  }
  return [ L||"-", R||"-" ];
}

/* ---------- decision audit trail ----------
   Machine stages are reconstructed from the stored report itself: the compat API
   exposes no timestamps for them, so we show the *stage* rather than fabricate a
   clock time. Human decisions are timestamped in this browser at submit time:
   that is when the event genuinely happened. */
const AUDIT_KEY="sdoc_audit_v1";
const AUDIT_KIND={confirm:"confirmed the machine verdict",pass:"passed the shipment",
  mismatch:"flagged a mismatch",back:"sent back for documents"};
function auditStore(){ try{ return JSON.parse(localStorage.getItem(AUDIT_KEY))||{}; }catch(_){ return {}; } }
function auditFor(id){ return auditStore()[id]||[]; }
function auditAppend(id,ev){
  const s=auditStore(), list=s[id]||(s[id]=[]);
  list.push(ev);
  if(list.length>20) s[id]=list.slice(-20);
  try{ localStorage.setItem(AUDIT_KEY,JSON.stringify(s)); }catch(_){}
}
function clockOf(ts){
  if(!ts) return "";
  const d=new Date(ts);
  if(isNaN(d.getTime())) return "";
  return [d.getHours(),d.getMinutes(),d.getSeconds()]
    .map(n=>String(n).padStart(2,"0")).join(":");
}
function renderAudit(d){
  const e=d.email||{}, r=d.result||{}, cps=d.comparisons||[], atts=e.attachments||[];
  const nodes=[];
  nodes.push({cls:"acc", t:"Email received",
    d:"From <em>"+esc(e.from||"unknown")+"</em> · "+atts.length+" attachment"+(atts.length===1?"":"s")});
  nodes.push({cls:"acc", t:"Classified",
    d:"Category <code>"+esc(r.category||"-")+"</code> · decided by <em>"+esc(r.decided_by||"rule")+"</em>"});
  if(cps.length){
    const okN=cps.filter(c=>c.match&&!c.missing).length;
    const dfN=cps.filter(c=>!c.match&&!c.missing).length;
    const msN=cps.filter(c=>c.missing).length;
    const bits=["<em>"+okN+"</em> of "+cps.length+" fields match"];
    if(dfN) bits.push("<em>"+dfN+"</em> differ");
    if(msN) bits.push("<em>"+msN+"</em> missing");
    nodes.push({cls:(dfN||msN)?"warn":"done", t:"SI ↔ draft BL compared", d:bits.join(" · ")});
  }
  const vbits=["<code>"+esc(r.status||"-")+"</code>"];
  if(r.has_defect&&(r.defect_fields||[]).length)
    vbits.push("defects: "+r.defect_fields.map(f=>"<em>"+esc(FIELDS_LABEL[f]||f)+"</em>").join(", "));
  if(r.review_reason) vbits.push("escalation reason <code>"+esc(r.review_reason)+"</code>");
  nodes.push({cls:r.status==="OK"?"done":(r.status==="MISMATCH"?"bad":"warn"),
    t:"Verdict issued", d:vbits.join(" · ")});

  const local=auditFor(e.email_id);
  if(local.length){
    local.forEach(ev=>nodes.push({
      cls:ev.status==="OK"?"done":(ev.status==="MISMATCH"?"bad":"warn"),
      human:true, ts:ev.ts, note:ev.note,
      t:"Human "+(AUDIT_KIND[ev.kind]||"reviewed"),
      d:"<code>"+esc(ev.status||"-")+"</code>"}));
  } else if(d.human_review){
    const hr=d.human_review;
    nodes.push({cls:"acc", human:true, ts:null, note:hr.note,
      t:"Human "+(hr.action==="confirm"?"confirmed the machine verdict":"overrode the verdict"),
      d:"<code>"+esc(hr.status||"-")+"</code>"});
  } else if(r.status==="MISMATCH"||r.status==="NEEDS_REVIEW"){
    nodes.push({cls:"todo", t:"Awaiting human decision",
      d:"Use the action bar below (<code>A</code> to confirm)"});
  }

  const body=nodes.map(n=>{
    const ts=n.ts?'<span class="ts">'+esc(clockOf(n.ts))+'</span>'
      :(n.human?'<span class="ts">recorded</span>':"");
    const tag=n.human?'<span class="au-tag human">HUMAN</span>':"";
    const note=n.note?'<div class="au-note">\u201c'+esc(n.note)+'\u201d</div>':"";
    return '<div class="au '+(n.cls||"")+'"><div class="au-dot"></div>'
      +'<div class="au-t"><b>'+n.t+'</b>'+tag+ts+'</div>'
      +'<div class="au-d">'+(n.d||"")+'</div>'+note+'</div>';
  }).join("");

  return '<section><h3>'+ICONS2.audit+' Decision audit trail</h3>'
    +'<div class="audit">'+body+'</div>'
    +'<div class="audit-foot">'+ICONS2.info
    +'<span>Machine stages are reconstructed from the stored report (the API keeps no timestamps for them). '
    +'Human decisions are timestamped locally in this browser.</span></div></section>';
}
const ICONS = {
  ok:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
  bad:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
  miss:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  file:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>'
};
const ICON_FOCUS_MAX='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>';
const ICON_FOCUS_MIN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h3a2 2 0 0 1 2 2v3M21 8h-3a2 2 0 0 0-2 2v3M3 16h3a2 2 0 0 0 2-2v-3M21 16h-3a2 2 0 0 1-2-2v-3"/></svg>';
const esc = s => (s??"").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const fmt = n => (n??0).toLocaleString();

function catBadge(c){if(!c)return `<span class="badge cat" style="--c:var(--muted-2)">-</span>`;const col=`var(--cat-${c})`;return `<span class="badge cat" style="--c:${col}">${c.replace(/_/g," ")}</span>`;}
function stBadge(s){if(!s)return `<span class="badge st" style="--c:var(--muted-2)">-</span>`;return `<span class="badge st ${s}">${s.replace(/_/g," ")}</span>`;}
/* ---------- confidence heat strip ----------
   The compat API exposes no confidence/score field, so we do NOT invent one.
   Tone is read straight off the verdict; the differing-field count only deepens
   the intensity. Reuses the same colour tokens as the status badges, so the
   strip needs no legend of its own. */
function heatOf(r){
  const st=(r&&r.status)||"";
  const n=Array.isArray(r&&r.defect_fields)?r.defect_fields.length:0;
  const tone={MISMATCH:["bad","var(--bad)"],
              NEEDS_REVIEW:["warn","var(--warn)"],
              OK:["ok","var(--ok)"]}[st]||["skip","var(--muted-2)"];
  const op = st==="MISMATCH"      ? Math.min(.78+n*.09,1)
           : st==="NEEDS_REVIEW"  ? .82
           : st==="OK"            ? .78
           : .45;                       /* SKIPPED / unclassified: recessive */
  const how = {MISMATCH:"Fields differ: needs a decision",
               NEEDS_REVIEW:"Escalated: machine is not confident",
               OK:"Cleared: no mismatch detected"}[st]||"Not classified: nothing to act on";
  let tip=how+(n?` · ${n} field${n>1?"s":""} differ`:"")+(st?` · ${st}`:"");
  if(r&&r.decided_by==="human")tip+=" · human reviewed";
  return {k:tone[0],tone:tone[1],op:+op.toFixed(2),tip};
}
function byIcon(b){
  if(b==="human") return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg> human';
  if(b==="llm") return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 4.5L18 9l-4.2 1.5L12 15l-1.8-4.5L6 9l4.2-1.5z"/><path d="M18 14l.9 2.3L21 17l-2.1.7L18 20l-.9-2.3L15 17l2.1-.7z"/></svg> llm';
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></svg> rule';
}

/* ---------- data ---------- */
async function loadSummary(){
  state.summary = await (await fetch(API+"/api/summary")).json();
  renderKPIs();
}

/* ---------------------------------------------------------------- integrations
   The Dashboard and the Gmail side panel share one stored result set. This
   reads that storage (GET /api/results): it never reprocesses a document.
   Called once at boot; there is no polling timer anywhere. */
async function loadIntegrations(){
  const box=$("#intgList"), sum=$("#intgSummary");
  if(!box||!sum) return;
  try{
    const rows=await (await fetch(API+"/api/results?limit=8")).json();
    if(!rows.length){
      sum.innerHTML='<span style="color:var(--muted-2)">No email processed through the API yet.</span>';
      box.innerHTML="";
      return;
    }
    const bad=rows.filter(r=>r.status==="MISMATCH").length;
    const rev=rows.filter(r=>r.status==="NEEDS_REVIEW").length;
    sum.innerHTML=`<b>${rows.length}</b> stored result(s) · <span style="color:var(--bad)">${bad} mismatch</span> · <span style="color:var(--warn)">${rev} needs review</span>`;
    box.innerHTML=rows.map(r=>`
      <div style="display:flex;align-items:center;gap:8px;font-size:11.5px">
        <span class="id">${esc(r.shipment_id||"-")}</span>
        <span style="color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.email_id)}</span>
        ${stBadge(r.status)}
      </div>`).join("");
  }catch(_){
    sum.innerHTML='<span style="color:var(--muted-2)">Integration API unavailable.</span>';
  }
}
function clearFilters(){$("#f-cat").value="";$("#f-status").value="";loadList();}
async function loadList(){
  try{
    const cat=$("#f-cat").value, st=$("#f-status").value;
    const p=new URLSearchParams({limit:500});
    if(cat)p.set("category",cat); if(st)p.set("status",st);
    const data=await (await fetch(API+"/api/emails?"+p)).json();
    state.items=(data&&data.items)||[];
    $("#listCount").textContent=fmt(data.total!=null?data.total:state.items.length);
    const el=$("#list");
    if(!state.items.length){
      // Distinguish "your filters matched nothing" from "the backend has no data
      // at all": the latter means a misconfigured DATA_SOURCE, not a real empty result.
      el.innerHTML=(cat||st)
        ? `<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><span class="big">No emails match.</span><span>Nothing in the inbox matches the current category / status filter.</span><button class="btn" data-tip="Clear filters" data-tip-desc="Reset the category and status filters" data-tip-kbd="C" data-tip-pos="bottom" onclick="clearFilters()">Clear filters</button></div>`
        : `<div class="empty">
             <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 7.5l8.5 5.5 8.5-5.5"/></svg>
             <span class="big">Inbox is empty</span>
             <span>The API answered, but returned <b>0</b> emails: so the backend is reading an empty data folder, not a filter problem.</span>
             <span class="hint">DATA_SOURCE must point at the folder that contains <b>inbox/</b> + <b>attachments/</b>.<br>Set it in <b>backend/.env</b> and restart the server:<br><b>DATA_SOURCE=&lt;project&gt;/sdoc-hackathon-bundle</b></span>
             <button class="btn" onclick="loadList()">Retry</button>
           </div>`;
      return;
    }
    el.innerHTML=state.items.map((r,i)=>{
      const active=r.email_id===state.active?"active":"";
      const hum=r.decided_by==="human"?'<span class="badge human">HUMAN</span>':"";
      const h=heatOf(r);
      return `<div class="row ${active}" data-i="${i}" onclick="openEmail('${esc(r.email_id)}')">
        <span class="heat ${h.k}" style="--c:${h.tone};--o:${h.op}" role="img" aria-label="${esc(h.tip)}" title="${esc(h.tip)}"></span>
        <div class="top"><span class="sbj">${esc(r.subject||"(no subject)")}</span>${stBadge(r.status)}</div>
        <div class="from">${esc(r.from||"unknown sender")}</div>
        <div class="mt">${catBadge(r.category)}<span class="id">${esc(r.email_id)}</span><span>📎 ${r.n_attachments}</span>${hum}</div>
      </div>`;
    }).join("");
  }catch(err){
    $("#list").innerHTML=`<div class="empty"><span class="big">Failed to load inbox</span><span>${esc(err.message||"Network error")}</span><button class="btn" style="margin-top:6px" onclick="loadList()">Retry</button></div>`;
  }
}
/* Fill (or clear) the docked verdict footer. It lives outside the scroller so the
   buttons are on screen from the moment the email opens. */
function setDock(html){
  const d=document.getElementById("detailDock");
  if(!d) return;
  d.innerHTML=html||"";
  d.hidden=!html;
}
async function openEmail(id){
  state.active=id;
  recordRecent(id);
  if(decodeURIComponent(location.hash.slice(1))!==id){history.replaceState(null,"","#"+id);}

  // Exit focus mode if active so the console table is visible
  if(appEl && appEl.classList.contains("focus")){
    setFocus(false);
  }

  // If the email is not present in the current view (filtered out), reset category/status filters so it can be revealed
  const existsInView = state.items && state.items.some(r => r.email_id === id);
  if(!existsInView && ($("#f-cat").value || $("#f-status").value)){
    $("#f-cat").value = "";
    $("#f-status").value = "";
    await loadList();
  }

  // Mark row active, scroll smoothly into view, and trigger pulse animation
  let activeRow = null;
  document.querySelectorAll(".row").forEach(el=>{
    const isCurrent = state.items[+el.dataset.i]?.email_id === id;
    el.classList.toggle("active", isCurrent);
    if(isCurrent) activeRow = el;
  });
  if(activeRow){
    activeRow.scrollIntoView({block:"center", behavior:"smooth"});
    activeRow.classList.remove("pulse");
    void activeRow.offsetWidth; // trigger reflow
    activeRow.classList.add("pulse");
  }

  setDock("");
  $("#detail").innerHTML=`<div class="skel"><div class="sk-line" style="width:28%"></div><div class="sk-line" style="width:62%"></div><div class="sk-block"></div><div class="sk-line" style="width:82%"></div><div class="sk-line" style="width:46%"></div></div>`;
  try{
    const res=await fetch(API+"/api/emails/"+encodeURIComponent(id));
    const d=await res.json().catch(()=>null);
    /* Guard: a 404 returns {detail:"email not found: …"}: no .email/.result.
       Without this check renderDetail() would throw a confusing
       "Cannot read properties of undefined (reading 'status')". */
    if(!res.ok||!d||!d.email){
      throw new Error((d&&(d.detail||d.error))||`Email ${id} is unavailable (HTTP ${res.status})`);
    }
    /* The detail endpoint is read-only, so an email that has not been processed
       yet comes back with result:null and processed:false instead of silently
       being processed on load. Show that state, don't treat it as a failure. */
    if(!d.result){
      state.detail=d;
      $("#detail").innerHTML=`<div class="empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>
        <span class="big">Not processed yet</span>
        <span>${esc(d.email.subject||d.email.email_id)}</span>
        <span class="muted">Reading this email does not start the pipeline. Run it explicitly below.</span>
        <button class="btn primary" style="margin-top:6px" onclick="processEmail('${esc(id)}')">Process this email</button>
      </div>`;
      setDock("");
      return;
    }
    state.detail=d;
    renderDetail(d);
    reflectWorkflow(d.result.status);
  }catch(err){
    setDock("");
    $("#detail").innerHTML=`<div class="empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/></svg>
      <span class="big">Failed to load email</span>
      <span>${esc(err.message||"Network error")}</span>
      <button class="btn" style="margin-top:6px" onclick="openEmail('${esc(id)}')">Retry</button>
    </div>`;
  }
}


/* ---------- explicit processing (the only way an email gets processed) ----------
   Kept as an explicit user action: the detail endpoint is read-only so that
   opening or refreshing a page can never start OCR/extraction/verification. */
async function processEmail(id){
  setDock("");
  $("#detail").innerHTML=`<div class="skel"><div class="sk-line" style="width:28%"></div><div class="sk-line" style="width:62%"></div><div class="sk-block"></div></div>`;
  try{
    const res=await fetch(API+"/emails/"+encodeURIComponent(id)+"/process",{method:"POST"});
    if(!res.ok) throw new Error("HTTP "+res.status);
    const r=await res.json();
    toast(`Processed: ${r.status||"done"}.`);
    await loadList();
    await openEmail(id);
  }catch(err){
    $("#detail").innerHTML=`<div class="empty">
      <span class="big">Could not process this email</span>
      <span>${esc(err.message||"Network error")}</span>
      <button class="btn" style="margin-top:6px" onclick="openEmail('${esc(id)}')">Back</button>
    </div>`;
  }
}
window.processEmail = processEmail;


/* ---------- empty detail helper (P1-d) ---------- */
function hintRow(k,v){ return '<div class="eh"><span class="eh-k">'+esc(k)+'</span><span class="eh-v">'+esc(v)+'</span></div>'; }function renderEmptyDetail(){
  const rec=(recentIds().map(id=>(state.items||[]).find(x=>x.email_id===id)).filter(Boolean)).slice(0,4);
  const recHtml = rec.length
    ? '<div class="rec-list">'+rec.map(r=>'<button class="rec-item" type="button" data-tip="Open email" data-tip-desc="Jump to this message" data-tip-pos="right" onclick="openEmail(\''+esc(r.email_id)+'\')"><span class="rec-from">'+esc(r.from||"-")+'</span><span class="rec-sub">'+esc(r.subject||"(no subject)")+'</span></button>').join("")+'</div>'
    : '<div class="muted small">Your recently opened emails will appear here.</div>';
  setDock("");
  $("#detail").innerHTML =
    '<div class="empty rich">'+
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l9 6 9-6M3 7v10a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V7M3 7a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1"/></svg>'+
      '<span class="big">Select an email to inspect</span>'+
      '<span class="hint-lead">Review the SI &harr; BL field comparison, then decide Approve / Reject.</span>'+
      '<div class="empty-hints">'+hintRow("Ctrl K","Search & jump to any email")+hintRow("T","Toggle light / dark")+hintRow("F","Focus mode (hide inbox)")+hintRow("[","Collapse the sidebar")+'</div>'+
      '<div class="rec-wrap"><div class="rec-title">Recent</div>'+recHtml+'</div>'+
    '</div>';
}
/* ---------- KPIs ---------- */
function renderKPIs(){
  const s=state.summary, bs=s.by_status||{};
  const ok=bs.OK||0, mm=bs.MISMATCH||0, nr=bs.NEEDS_REVIEW||0, tot=s.total||0;
  const pct=v=>tot?Math.round(v/tot*100):0;
  const other=Math.max(0,tot-(ok+mm+nr));
  $("#kpis").innerHTML=`
    <div class="kpi accent"><div class="lab"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l9 6 9-6M3 7v10a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V7M3 7a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1"/></svg>Emails processed</div><div class="num">${fmt(tot)}</div><div class="sub">across the operations inbox</div></div>
    <div class="kpi"><div class="lab"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>Cleared · OK</div><div class="num" style="color:var(--ok)">${fmt(ok)}</div><div class="sub">${pct(ok)}% no mismatch</div></div>
    <div class="kpi"><div class="lab"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>Mismatches</div><div class="num" style="color:var(--bad)">${fmt(mm)}</div><div class="sub">fields differ · SI ↔ BL</div></div>
    <div class="kpi"><div class="lab"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/></svg>Escalated</div><div class="num" style="color:var(--warn)">${fmt(nr)}</div><div class="sub">${fmt(s.reviewed||0)} human reviews</div></div>
    <div style="grid-column:1/-1">
      <div class="dist">
        <span class="seg" style="flex:${ok};background:var(--ok)" title="OK ${ok}"></span>
        <span class="seg" style="flex:${mm};background:var(--bad)" title="Mismatch ${mm}"></span>
        <span class="seg" style="flex:${nr};background:var(--warn)" title="Review ${nr}"></span>
        <span class="seg other" style="flex:${other};background:var(--border-strong)" title="Other ${other}"></span>
      </div>
      <div class="dist-cap">
        <span style="flex:${ok}">OK ${pct(ok)}%</span>
        <span style="flex:${mm}">Mismatch ${pct(mm)}%</span>
        <span style="flex:${nr}">Review ${pct(nr)}%</span>
        <span style="flex:${other}">Other ${pct(other)}%</span>
      </div>
      <div class="kpi-foot">
        <span class="updated" id="updatedAt"></span>
        <button class="btn ghost sm" type="button" data-tip="Refresh inbox" data-tip-desc="Re-fetch emails and KPIs" data-tip-kbd="R" data-tip-pos="bottom" onclick="loadSummary();loadList();toast('Inbox refreshed')">&#8635; Refresh</button>
      </div>
    </div>`;
  const _relNum=$("#relNum"); if(_relNum) _relNum.textContent=fmt(nr);
  const _relPulse=$("#relPulse"); if(_relPulse) _relPulse.style.background = nr>0 ? "var(--warn)" : "var(--ok)";
  const ua=document.getElementById("updatedAt"); if(ua) ua.textContent="Updated "+new Date().toLocaleTimeString();
}

/* ---------- workflow stepper ---------- */
function reflectWorkflow(status){
  const map={OK:["classify","extract","compare"],MISMATCH:["classify","extract","compare"],NEEDS_REVIEW:["classify","extract","compare","escalate"]};
  const lit=map[status]||["classify"];
  document.querySelectorAll(".step").forEach(s=>s.classList.toggle("lit",lit.includes(s.dataset.step)));
  const last=lit[lit.length-1];
  document.querySelectorAll(".step").forEach(s=>s.classList.toggle("active",s.dataset.step===last));
}

/* ---------- detail ---------- */
function renderDetail(d){
  const e=d.email, r=d.result;
  let cmp="";
  if(d.comparisons && d.comparisons.length){
    const rows=d.comparisons.map(c=>{
      const cls=c.missing?"miss":(c.match?"match":"diff");
      const mark=c.missing?`<span class="vpill miss">${ICONS.miss} missing</span>`
        :c.match?`<span class="vpill ok">${ICONS.ok} match</span>`
        :`<span class="vpill bad">${ICONS.bad} differs</span>`;
      const fld=FIELDS_LABEL[c.field]||(c.field||"-").replace(/_/g," ");
      const sv=(c.match&&!c.missing)?null:sevOf(c.field);
      const sev=sv?`<span class="sev ${sv}" title="${esc(SEV_TITLE[sv])}">${sv.toUpperCase()}</span>`:"";
      const pair=(c.match||c.missing)
        ? [esc(c.si_value==null?"-":c.si_value), esc(c.bl_value==null?"-":c.bl_value)]
        : diffCells(c.si_value,c.bl_value);
      return `<tr class="cmp-row ${cls}"><td class="f">${fld}<small>${c.field}</small>${sev}</td>
        <td class="val si">${pair[0]}</td><td class="op">→</td>
        <td class="val bl">${pair[1]}</td><td>${mark}</td></tr>`;
    }).join("");
    cmp=`<section><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h7M14 12h7M12 3v7M12 14v7"/><circle cx="12" cy="12" r="2"/></svg>SI → draft BL · field comparison</h3>
      <table class="cmp"><thead><tr><th>Field</th><th>Shipping Instruction</th><th></th><th>Draft Bill of Lading</th><th>Verdict</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="note-foot">${ICONS2.info}<span>Differing tokens are marked inline: <span class="tk del">SI only</span> vs <span class="tk ins">BL only</span>. Severity (HIGH/MED/LOW) is a front-end heuristic based on what a wrong value costs at customs; it is not produced by the engine.</span></div></section>`;
  }
  let review="", actBar="";
  if(r.status==="NEEDS_REVIEW"||r.status==="MISMATCH"||d.human_review){
    const hr=d.human_review;
    const reason=r.status==="NEEDS_REVIEW"?`<div class="rb-why"><b>Escalation reason:</b> ${esc(r.review_reason||"-")}</div>`:"";
    const recorded=hr?`<div class="recorded">${ICONS.ok}<span>Last decision: <b>${esc(hr.action)}</b>${hr.status?" → "+esc(hr.status):""}${hr.note?" · \u201c"+esc(hr.note)+"\u201d":""}</span></div>`:"";
    review=`<section><h3>${ICONS2.audit}Human review</h3>
      <div class="review-box ${r.status==='NEEDS_REVIEW'?'escalated':''}">
        ${r.status==="NEEDS_REVIEW"?'<div class="rb-head">'+ICONS.miss+' Escalated for human review</div>':''}
        ${reason}${recorded}
      </div></section>`;
    actBar=`<div class="act-bar dock">
          <button class="act primary" data-kind="confirm" data-tip="Confirm verdict" data-tip-desc="Confirm this email’s comparison verdict" data-tip-kbd="A" data-tip-pos="right" onclick="sendReview('${esc(e.email_id)}','confirm',this)">${ICONS.ok} Confirm<span class="k">A</span></button>
          <button class="act pass" data-kind="pass" data-tip="Pass" data-tip-desc="Mark passed: no mismatch" data-tip-kbd="O" data-tip-pos="right" onclick="sendReview('${esc(e.email_id)}','pass',this)">${ICONS2.pass} Pass<span class="k">O</span></button>
          <button class="act flag" data-kind="mismatch" data-tip="Mismatch" data-tip-desc="Flag this email as SI↔BL mismatch" data-tip-kbd="M" data-tip-pos="right" onclick="sendReview('${esc(e.email_id)}','mismatch',this)">${ICONS2.flag} Mismatch<span class="k">M</span></button>
          <button class="act back" data-kind="back" data-tip="Send back" data-tip-desc="Return this email for correction" data-tip-kbd="R" data-tip-pos="right" onclick="sendReview('${esc(e.email_id)}','back',this)">${ICONS2.back} Send Back<span class="k">R</span></button>
          ${(d.shipment_id || r.status==="MISMATCH" || r.status==="NEEDS_REVIEW") ? `<button class="act" type="button" style="background:var(--surface-3);border-color:var(--accent);color:var(--accent);font-weight:650;" onclick="openShipmentFromReview(${d.shipment_id || 'null'})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/></svg> Open Shipment</button>` : ''}
          <input type="text" class="note-in" id="ov-note" placeholder="Reviewer note (optional)" maxlength="240" autocomplete="off">
        </div>`;
  }
  const atts=(e.attachments||[]).map(a=>{
    const name=a.split("/").pop(); const ext=(name.split(".").pop()||"").toLowerCase();
    const textish=["txt","csv","md","json","log"].includes(ext);
    const url=API+"/api/attachments/"+esc(a);
    return `<div class="att" data-path="${esc(a)}" data-text="${textish}" data-name="${esc(name)}">
      <div class="ah" onclick="toggleAtt(this)"><span class="ic">${ICONS.file}</span>
        <div style="flex:1;min-width:0"><div class="nm">${esc(name)}</div><div class="ty">${esc(ext||"file")}</div></div>
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="var(--muted-2)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </div>
      <div class="ap">
        <a class="lnk" href="${url}" target="_blank" rel="noopener" title="Open ${esc(name)} in a new tab"
           onclick="event.stopPropagation()">${ICONS2.ext}<span class="lbl">Open</span></a>
        <a class="lnk" href="${url}" download="${esc(name)}" title="Save ${esc(name)} to your computer"
           onclick="event.stopPropagation();flashSaved(this)">${ICONS2.dl}<span class="lbl">Download</span></a>
      </div>
      <div class="aprev"></div>
    </div>`;
  }).join("");
  const attSection=atts?`<section><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>Attachments</h3><div class="att-grid">${atts}</div></section>`:"";

  const verdict = r.has_defect?`defect in ${r.defect_fields.map(f=>`<b>${esc(FIELDS_LABEL[f]||f)}</b>`).join(", ")}`
    :(r.status==="OK"?"no mismatch detected":esc(r.review_reason||r.status));
  const hum=r.decided_by==="human"?'<span class="badge human">HUMAN REVIEWED</span>':"";
  const audit=renderAudit(d);
  const shBtn = (d.shipment_id || r.status==="MISMATCH" || r.status==="NEEDS_REVIEW")
    ? `<button class="btn sm primary" type="button" style="margin-left:10px;padding:4px 10px;font-size:11.5px;" onclick="openShipmentFromReview(${d.shipment_id || 'null'})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/></svg> Open Shipment${d.shipment_key ? ' ('+esc(d.shipment_key)+')' : ''}</button>`
    : "";

  $("#detail").innerHTML=`<div class="fade">
    <div class="detail-head">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${catBadge(r.category)} ${stBadge(r.status)} ${hum}</div>
      <h2>${esc(e.subject||"(no subject)")}</h2>
      <div class="kv"><b>From:</b> ${esc(e.from||"unknown")} &nbsp;·&nbsp; <b>ID:</b> <span style="font-family:var(--mono)">${esc(e.email_id)}</span></div>
      <div class="kv"><b>Decided by:</b> <span class="dotby">${byIcon(r.decided_by)}</span>${r.rule&&r.decided_by!=="human"?` <span style="color:var(--muted-2)">· ${esc(r.rule)}</span>`:""}</div>
      <div class="verdict-line" style="display:flex;align-items:center;flex-wrap:wrap;gap:6px"><b style="color:var(--muted)">Verdict:</b> <span>${verdict}</span>${shBtn}</div>
    </div>
    ${cmp}${review}${audit}${attSection}
    <section><h3><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z"/><path d="M4 7l8 5 8-5"/></svg>Email body</h3><pre class="body">${esc(e.body||"")}</pre></section>
  </div>`;
  setDock(actBar);
  renderFocusIcons();
  observeDiff();
}

/* ---------- diff row highlight ---------- */
let diffObs;
function observeDiff(){
  if(diffObs)diffObs.disconnect();
  const rows=document.querySelectorAll("tr.cmp-row.diff");
  if(!("IntersectionObserver" in window)||!rows.length)return;
  diffObs=new IntersectionObserver(es=>{
    es.forEach(en=>{if(en.isIntersecting){en.target.classList.add("pulse");diffObs.unobserve(en.target);}});
  },{threshold:.2});
  rows.forEach(r=>diffObs.observe(r));
}

/* ---------- attachment viewer ----------
   The action row (.ap) is permanent now, so opening a preview must NOT clobber
   it: the preview lives in its own .aprev container below. Download is a plain
   <a download> so it works natively (streams, any file size) even if this JS
   never runs; flashSaved() is a cosmetic confirmation on top of that. */
async function toggleAtt(head){
  const card=head.closest(".att");
  const prev=card.querySelector(".aprev");
  if(!prev)return;
  if(card.dataset.open==="1"){card.dataset.open="0";prev.innerHTML="";return;}
  card.dataset.open="1";
  if(card.dataset.text!=="true"){
    prev.innerHTML='<div class="loading">No inline preview for this file type: use Open or Download.</div>';
    return;
  }
  prev.innerHTML='<div class="loading">Loading preview…</div>';
  try{
    const r=await fetch(API+"/api/attachments/"+card.dataset.path);
    if(!r.ok)throw new Error("HTTP "+r.status);
    const txt=await r.text();
    if(card.dataset.open!=="1")return;      /* closed again while fetching */
    prev.innerHTML="<pre>"+esc(txt)+"</pre>";
  }catch(err){
    prev.innerHTML='<div class="loading">Preview unavailable ('+esc(err.message||"error")+').</div>';
  }
}
function flashSaved(el){
  const lb=el.querySelector(".lbl");
  if(!lb||el.dataset.busy==="1")return;
  el.dataset.busy="1";
  const old=lb.textContent;
  lb.textContent="Saved"; el.classList.add("saved");
  setTimeout(()=>{lb.textContent=old;el.classList.remove("saved");el.dataset.busy="0";},1700);
}

/* ---------- review submit ----------
   Single entry point for the four verdict actions. `kind` maps onto the API's
   confirm/override pair; every decision is appended to the local audit trail
   with a real timestamp before the view reloads. */
const REVIEW_MSG={confirm:"Verdict accepted.",pass:"Marked as passed.",
  mismatch:"Flagged as mismatch.",back:"Sent back for documents."};
async function sendReview(id,kind,btn){
  const cur=state.detail||{}, r=cur.result||{};
  const nm=$("#ov-note");
  const note=(nm&&nm.value.trim())||null;
  const body={action: kind==="confirm"?"confirm":"override"};
  if(kind!=="confirm"){
    if(kind==="pass") body.status="OK";
    else if(kind==="mismatch"){
      body.status="MISMATCH";
      body.defect_fields=(cur.comparisons||[]).filter(c=>!c.match&&!c.missing).map(c=>c.field);
    }
    else body.status="NEEDS_REVIEW";
  }
  if(note) body.note=note;
  if(btn) btn.disabled=true;
  try{
    const res=await fetch(API+"/api/emails/"+id+"/review",
      {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!res.ok) throw new Error("HTTP "+res.status);
    auditAppend(id,{ts:Date.now(),kind,status:body.status||r.status||null,note});
    toast(REVIEW_MSG[kind]||"Decision recorded.");
    await loadSummary(); await loadList(); openEmail(id);
  }catch(err){
    if(btn) btn.disabled=false;
    toast("Could not save the decision: "+(err.message||"network error"));
  }
}
let toastT;
function toast(msg){$("#toastMsg").textContent=msg;$("#toast").classList.add("show");clearTimeout(toastT);toastT=setTimeout(()=>$("#toast").classList.remove("show"),2600);}

/* ---------- events & init ---------- */
$("#f-cat").innerHTML='<option value="">All categories</option>'+CATEGORIES.map(c=>`<option value="${c}">${c.replace("_"," ")}</option>`).join("");
/* search is now a single button -> opens the command palette */
const IS_MAC=/Mac|iPhone|iPad|iPod/.test(navigator.platform||navigator.userAgent||"");
$("#qBtn").addEventListener("click",openPalette);
(function(){const pt=$("#palToggle");
  if(pt)pt.textContent=IS_MAC?"⌘K":"Ctrl+K";})();
$("#f-cat").addEventListener("change",loadList);
$("#f-status").addEventListener("change",e=>{const v=e.target.value;if(v)recordStatusUse(v);loadList();});

/* ---------- collapsible rail ---------- */
const appEl=$(".app");
function setRail(v){
  appEl.classList.toggle("collapsed",v);
  localStorage.setItem("sdoc-rail",v?"1":"0");
  const b=$("#collapseBtn");
  if(b){b.dataset.tip=v?"Expand sidebar":"Collapse sidebar";b.setAttribute("aria-label",v?"Expand sidebar":"Collapse sidebar");}
}
let railAnimT=null;
function toggleRail(){
  /* Scoped to the transition window so the rail "pops" on a user collapse
     without replaying itself every time the page loads in rail mode. */
  appEl.classList.add("rail-anim");
  setRail(!appEl.classList.contains("collapsed"));
  clearTimeout(railAnimT);
  railAnimT=setTimeout(()=>appEl.classList.remove("rail-anim"),480);
}
const _collapseBtn=$("#collapseBtn"); if(_collapseBtn) _collapseBtn.addEventListener("click",toggleRail);

/* ---------- responsive Ctrl+K chip on the search button ----------
   The inbox panel is resizable and the layout settles after fonts load, so we
   re-check from several angles. Once the button is too narrow the chip would
   render as a clipped "Ctr", so we drop it entirely instead. */
function syncSearchChip(){
  const btn=$("#qBtn"); if(!btn) return;
  btn.classList.toggle("narrow", btn.clientWidth > 0 && btn.clientWidth < 118);
}
(function(){
  const btn=$("#qBtn"); if(!btn) return;
  if(window.ResizeObserver) new ResizeObserver(syncSearchChip).observe(btn);
  window.addEventListener("resize", syncSearchChip);
  window.addEventListener("load", syncSearchChip);
  syncSearchChip();
})();

/* ---------- focus mode ---------- */
function renderFocusIcons(){
  const min=appEl.classList.contains("focus");
  document.querySelectorAll(".focus-ico").forEach(s=>s.innerHTML=min?ICON_FOCUS_MIN:ICON_FOCUS_MAX);
  document.querySelectorAll(".focus-btn").forEach(b=>{
    b.dataset.tip=min?"Exit focus mode":"Focus mode";
    b.dataset.tipDesc=min?"Bring the inbox back":"Hide the inbox and expand the SI ↔ BL comparison";
  });
}
function setFocus(v){appEl.classList.toggle("focus",v);localStorage.setItem("sdoc-focus",v?"1":"0");renderFocusIcons();}
function toggleFocus(){setFocus(!appEl.classList.contains("focus"));}
document.addEventListener("click",e=>{const b=e.target.closest&&e.target.closest(".focus-btn");if(b){e.stopPropagation();toggleFocus();}});

function isTyping(){const el=document.activeElement;if(!el)return false;
  const t=el.tagName;return t==="INPUT"||t==="TEXTAREA"||t==="SELECT"||el.isContentEditable===true;}

/* ---------- shortcut registry ---------- */
/* Single source of truth. The hover tooltips, the `?` cheat-sheet and the
   palette badges are all derived from these tables so they cannot drift apart. */
const MODK=IS_MAC?"\u2318":"Ctrl";
const SHIFTK="\u21e7";
const NAV={
  gateway:  {href:"#gateway",     key:"0", label:"Secure Ingestion",
             desc:"Quarantine buffer: safety checks, dangerous extension blocking & staged triage"},
  console:  {href:"#review",      key:"1", label:"Review",
             desc:"Email & document review: process inbox and verify SI ↔ BL mismatches"},
  shipments:{href:"#lifecycle",  key:"2", label:"Shipments",
             desc:"Shipment hub: overview, documents, version history, diffs and resolution"},
  back:     {href:"#review",      key:"B", label:"Back to Review",
             desc:"Return to the review queue"}
};
const SHORTCUTS=[
  {section:"Navigation"},
  {keys:[MODK,"K"], label:"Search the inbox", desc:"Find an email by subject, sender or id: or run a command"},
  {keys:["0"], label:NAV.gateway.label,   desc:NAV.gateway.desc},
  {keys:["1"], label:NAV.console.label,   desc:NAV.console.desc},
  {keys:["2"], label:NAV.shipments.label, desc:NAV.shipments.desc},
  {keys:["B"], label:NAV.back.label,      desc:NAV.back.desc},
  {keys:["T"], label:"Toggle theme", desc:"Switch between dark and light mode"},
  {keys:["F"], label:"Focus mode", desc:"Hide the inbox and expand the comparison"},
  {keys:["["], label:"Collapse sidebar", desc:"Expand or collapse the sidebar into an icon rail"},
  {keys:["?"], label:"This panel", desc:"Show or hide this shortcut list"},
  {keys:["Esc"], label:"Close", desc:"Close the palette or this panel"},
  {section:"Review an email"},
  {keys:["A"], label:"Confirm verdict", desc:"Confirm the open email's verdict"},
  {keys:["O"], label:"Pass", desc:"Skip the open email"},
  {keys:["M"], label:"Mismatch", desc:"Flag the open email as mismatched"},
  {keys:["R"], label:"Send back", desc:"Return the open email for correction"},
  {section:"Inbox"},
  {keys:["R"], label:"Refresh inbox", desc:"Re-fetch emails and KPIs (when no email is open)"},
  {keys:["C"], label:"Clear filters", desc:"Reset the category and status filters"},
  {keys:["\u2191","\u2193"], label:"Next / previous email", desc:"Move through the inbox and open the highlighted email"}
];
function navTo(h){ location.href=h; }
function goGateway(){ switchView("gateway"); }
function goConsole(){ switchView("review"); }
function goShipments(){ switchView("lifecycle"); }

function openShipmentFromReview(shipmentId){
  switchView("lifecycle");
  if(shipmentId && window.Lifecycle && window.Lifecycle.openShipment){
    window.Lifecycle.openShipment(shipmentId);
  } else if(window.Lifecycle && window.Lifecycle.renderDashboard){
    window.Lifecycle.renderDashboard();
  }
}
window.openShipmentFromReview = openShipmentFromReview;

const helpEl=$("#help");
function renderHelp(){
  let html="";
  for(const s of SHORTCUTS){
    if(s.section){ html+='<div class="help-sec">'+esc(s.section)+'</div>'; continue; }
    html+='<div class="help-row"><div class="ht"><b>'+esc(s.label)+'</b><span>'+esc(s.desc)+'</span></div>'+
      '<div class="kk">'+s.keys.map(k=>'<b>'+esc(k)+'</b>').join("")+'</div></div>';
  }
  $("#helpRows").innerHTML=html;
}
function openHelp(){ renderHelp(); helpEl.hidden=false; }
function closeHelp(){ helpEl.hidden=true; }
function toggleHelp(){ helpEl.hidden?openHelp():closeHelp(); }
$("#helpClose").addEventListener("click",closeHelp);
helpEl.addEventListener("mousedown",e=>{ if(e.target===helpEl) closeHelp(); });

document.addEventListener("keydown",e=>{
  if((e.key==="k"||e.key==="K")&&(e.metaKey||e.ctrlKey)){e.preventDefault();$("#palette").hidden?openPalette():closePalette();return;}
  if(!$("#palette").hidden){
    if(e.key==="Escape"){e.preventDefault();closePalette();}
    else if(e.key==="ArrowDown"){e.preventDefault();palMove(1);}
    else if(e.key==="ArrowUp"){e.preventDefault();palMove(-1);}
    else if(e.key==="Enter"){e.preventDefault();palRun();}
    return;
  }
  if(!helpEl.hidden){
    if(e.key==="Escape"||e.key==="?"){e.preventDefault();closeHelp();}
    return;
  }
  /* The overlay blocks above are shared, so they stay in front of this guard:
     a palette opened from the Lifecycle tab still closes from anywhere. Past
     that point the Lifecycle console owns the keyboard while it is on screen. */
  const lc=$("#viewLifecycle");
  if(lc && !lc.hidden) return;
  if(isTyping())return;
  if(e.key==="/"&&!(e.metaKey||e.ctrlKey)){e.preventDefault();openPalette();}
  if(e.key==="?"){e.preventDefault();openHelp();return;}
  if(e.key==="0"){e.preventDefault();goGateway();return;}
  if(e.key==="1"){e.preventDefault();goConsole();return;}
  if(e.key==="2"){e.preventDefault();goShipments();return;}
  if(e.key==="b"||e.key==="B"){e.preventDefault();goConsole();return;}
  if(e.key==="t"||e.key==="T"){e.preventDefault();toggleTheme();}
  if(e.key==="f"||e.key==="F"){e.preventDefault();toggleFocus();}
  if(e.key==="["){e.preventDefault();toggleRail();}
  /* verdict shortcuts (A/O/M/R): they only fire when the open email actually
     renders an action bar, so they stay inert on passed / skipped mail. Modifier
     combos are ignored so Ctrl+A (select all) still works. */
  const vk=(e.metaKey||e.ctrlKey||e.altKey)?null:
    ({a:"confirm",o:"pass",m:"mismatch",r:"back"})[(e.key||"").toLowerCase()];
  if(vk){
    const vb=document.querySelector('.act[data-kind="'+vk+'"]');
    if(vb){e.preventDefault();vb.click();return;}
  }
  /* Inbox shortcuts: only on the list view (no email open). Refresh (R) yields to
     the "Send back" verdict when an action bar is present, handled just above. */
  if(!state.active && !e.metaKey && !e.ctrlKey && !e.altKey){
    if(e.key==="r"||e.key==="R"){ e.preventDefault(); loadSummary(); loadList(); toast("Inbox refreshed"); return; }
    if(e.key==="c"||e.key==="C"){ e.preventDefault(); clearFilters(); return; }
  }
  if(["ArrowDown","ArrowUp"].includes(e.key)){
    const rows=[...document.querySelectorAll(".row")];if(!rows.length)return;
    let idx=rows.findIndex(r=>r.classList.contains("active"));
    idx=e.key==="ArrowDown"?(idx+1)%rows.length:(idx-1+rows.length)%rows.length;
    rows[idx].scrollIntoView({block:"nearest"});openEmail(state.items[+rows[idx].dataset.i].email_id);
  }
});
/* The theme toggle wears the CURRENT mode: a sun while you are in light mode and
   a moon while you are in dark mode. Same rule on every surface that mentions the
   theme (sidebar toggle, tooltip, palette entry) so nothing can disagree. */
const THEME_GLYPH={
  sun:'<circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7"/>',
  moon:'<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'
};
function themeGlyph(t){ return t==="light"?THEME_GLYPH.sun:THEME_GLYPH.moon; }
function curTheme(){ return document.documentElement.getAttribute("data-theme")==="dark"?"dark":"light"; }
function themeIconSvg(t){
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '+
    'stroke-linecap="round" stroke-linejoin="round">'+themeGlyph(t)+'</svg>';
}
function applyTheme(t){
  document.documentElement.setAttribute("data-theme",t);
  try{ localStorage.setItem("sdoc-theme",t); }catch(_){}
  const lbl=$("#themeLbl"); if(lbl) lbl.textContent=t==="dark"?"Dark mode":"Light mode";
  const ico=$("#themeIcon"); if(ico) ico.innerHTML=themeGlyph(t);
  const btn=$("#themeBtn");
  if(btn){
    const next=t==="dark"?"light":"dark";
    btn.dataset.tip="Switch to "+next+" mode";
    btn.dataset.tipDesc="Currently in "+t+" mode";
    btn.setAttribute("aria-label","Switch to "+next+" mode");
  }
}
function toggleTheme(){ applyTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark"); }
$("#themeBtn").addEventListener("click",toggleTheme);
/* the theme is shared with every other console tab and the ShipMail desk */
window.addEventListener("storage",e=>{
  if(e.key==="sdoc-theme"&&(e.newValue==="light"||e.newValue==="dark")) applyTheme(e.newValue);
});
(function(){applyTheme(localStorage.getItem("sdoc-theme")||"dark");})();
setRail(localStorage.getItem("sdoc-rail")==="1");

/* ---------- contextual tooltips ---------- */
/* Elements opt in with data-tip / data-tip-desc / data-tip-kbd / data-tip-pos.
   This replaces the slow native `title` bubble so the icon rail stays readable
   once the sidebar is collapsed to icons. */
(function(){
  const tip=$("#tip");
  let cur=null;
  function place(el){
    const r=el.getBoundingClientRect(), pos=el.dataset.tipPos||"bottom";
    tip.style.left="-9999px"; tip.style.top="0px";
    const tw=tip.offsetWidth||220, th=tip.offsetHeight||40;
    let x, y;
    if(pos==="right"){
      x=r.right+16; y=r.top+(r.height-th)/2;
      if(x+tw>innerWidth-8) x=Math.max(8, r.left-tw-16);
    }else{
      x=r.left; y=r.bottom+9;
      if(x+tw>innerWidth-8) x=innerWidth-8-tw;
      if(y+th>innerHeight-8) y=r.top-th-9;
    }
    tip.style.left=Math.round(Math.max(8,x))+"px";
    tip.style.top=Math.round(Math.max(8, Math.min(y, innerHeight-8-th)))+"px";
  }
  function open(el){
    if(cur===el) return;
    if(!$("#palette").hidden || !helpEl.hidden) return;
    /* A screen reader never sees the hover card, so tie the element to the live
       tooltip via aria-describedby (it fires on focusin too). */
    if(cur) cur.removeAttribute("aria-describedby");
    cur=el;
    el.setAttribute("aria-describedby","tip");
    const keys=(el.dataset.tipKbd||"").split("|").filter(Boolean);
    tip.innerHTML=`<div class="tip-t">${esc(el.dataset.tip)}</div>`+
      (el.dataset.tipDesc?`<div class="tip-d">${esc(el.dataset.tipDesc)}</div>`:"")+
      (keys.length?`<div class="tip-k">${keys.map(k=>`<b>${esc(k)}</b>`).join("")}</div>`:"");
    tip.classList.add("show");
    place(el);
  }
  function close(){ if(cur) cur.removeAttribute("aria-describedby"); cur=null; tip.classList.remove("show"); }
  document.addEventListener("mouseover",e=>{
    const el=e.target.closest&&e.target.closest("[data-tip]");
    if(el) open(el);
    else if(cur && !cur.contains(e.target)) close();
  });
  document.addEventListener("mouseout",e=>{
    if(cur && !cur.contains(e.relatedTarget)) close();
  });
  document.addEventListener("focusin",e=>{
    const el=e.target.closest&&e.target.closest("[data-tip]");
    if(el) open(el); else close();
  });
  document.addEventListener("focusout",close);
  document.addEventListener("click",close);
  window.addEventListener("scroll",close,true);
  window.addEventListener("blur",close);
  window.addEventListener("resize",close);
})();
/* platform-aware modifier chip on the search button */
(function(){
  const el=$("#qBtn");
  if(!el) return;
  el.dataset.tipKbd=MODK+"|K";
  const chip=el.querySelector(".hk");
  if(chip) chip.textContent=IS_MAC?"\u2318K":"Ctrl K";
})();
$("#themeBtn").addEventListener("keydown",e=>{ if(e.key==="Enter"||e.key===" "){e.preventDefault();toggleTheme();} });

/* ---------- draggable split: resize inbox ⇄ detail ---------- */
(function(){
  const ws=document.querySelector(".workspace"), grip=$("#splitGrip");
  if(!ws||!grip)return;
  const KEY="sdoc-inbox-w", PAD=20, DEF=392, MIN=248;
  let cur=DEF, dragging=false;
  const maxW=()=>Math.max(MIN,Math.min(720,ws.clientWidth*0.62));
  function apply(w,save){
    w=Math.round(Math.max(MIN,Math.min(maxW(),w)));
    cur=w; ws.style.setProperty("--inbox-w",w+"px");
    if(save!==false){try{localStorage.setItem(KEY,String(w));}catch(_){}}
  }
  const saved=parseFloat(localStorage.getItem(KEY));
  if(isFinite(saved)&&saved>0)apply(saved,false); else apply(DEF,false);
  window.resetSplit=()=>apply(DEF);
  window.nudgeSplit=d=>apply(cur+d);
  const grabX=e=>Math.max(MIN,Math.min(maxW(),(e.clientX||0)-ws.getBoundingClientRect().left-PAD));
  grip.addEventListener("pointerdown",e=>{
    if(e.pointerType==="mouse"&&e.button!==0)return;
    dragging=true; grip.classList.add("on"); document.body.classList.add("col-resizing");
    try{grip.setPointerCapture(e.pointerId);}catch(_){}
    e.preventDefault();
  });
  grip.addEventListener("pointermove",e=>{if(dragging){apply(grabX(e));e.preventDefault();}});
  function endDrag(e){
    if(!dragging)return; dragging=false; grip.classList.remove("on"); document.body.classList.remove("col-resizing");
    try{grip.releasePointerCapture(e.pointerId);}catch(_){}
  }
  grip.addEventListener("pointerup",endDrag);
  grip.addEventListener("pointercancel",endDrag);
  grip.addEventListener("dblclick",()=>apply(DEF));
  grip.addEventListener("keydown",e=>{
    const step=e.shiftKey?48:16; let d=0;
    if(e.key==="ArrowLeft")d=-step;
    else if(e.key==="ArrowRight")d=step;
    else if(e.key==="Enter"||e.key==="Home"||e.key==="End")d=DEF-cur;
    if(!d)return;
    e.preventDefault(); apply(cur+d);
  });
  let rt;
  window.addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(()=>apply(cur,false),90);});
})();

setFocus(localStorage.getItem("sdoc-focus")==="1");
window.addEventListener("hashchange",()=>{const h=decodeURIComponent(location.hash.slice(1));if(h&&h!=="review"&&h!=="gateway"&&!h.startsWith("lifecycle")&&!h.startsWith("/")&&h!==state.active)openEmail(h);});
/* ---------- command palette ---------- */
const ICONS2={
  shield:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  jump:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  focus:ICON_FOCUS_MAX,
  theme:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  sun:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7"/></svg>',
  moon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  rail:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18M3 12h12M3 19h18"/></svg>',
  ship:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/></svg>',
  kbd:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>',
  clear:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  refresh:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M21 3.6V9h-5.4"/></svg>',
  mail:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 7.5l8.5 5.5 8.5-5.5"/></svg>',
  clock:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  split:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>',
  audit:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12a8.5 8.5 0 1 0 2.8-6.3"/><path d="M3 4.5V9h4.5"/><path d="M12 8.2V12l2.8 1.8"/></svg>',
  info:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 16.5V11M12 7.8h.01"/></svg>',
  pass:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.6 2.6L16 9.7"/></svg>',
  flag:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 21V4"/><path d="M5 5h11l-1.6 3.5L16 12H5"/></svg>',
  back:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>',
  dl:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 19h16"/></svg>',
  ext:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6"/><path d="M20 4l-8.5 8.5"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>'
};
const RECENT_KEY="sdoc_recent_v1";
function recentIds(){try{return JSON.parse(localStorage.getItem(RECENT_KEY)||"[]");}catch(_){return [];}}
function recordRecent(id){let a=recentIds().filter(x=>x!==id);a.unshift(id);a=a.slice(0,6);try{localStorage.setItem(RECENT_KEY,JSON.stringify(a));}catch(_){}}
/* status keyword → backend status filter */
const STATUS_ALIASES={
  MISMATCH:["mismatch","mismatched","diff","conflict","discrepancy","defect"],
  NEEDS_REVIEW:["review","escalat","pending","needs","audit","flag"],
  OK:["ok","clear","cleared","pass","passed","good","clean"]
};
function statusForQuery(q){for(const st of ["MISMATCH","NEEDS_REVIEW","OK"])for(const a of STATUS_ALIASES[st])if(q.includes(a))return st;return null;}
/* frequent status usage → pinned chips */
const FREQ_KEY="sdoc_status_freq";
const STATUS_LABEL={MISMATCH:"Mismatches",NEEDS_REVIEW:"Needs review",OK:"Cleared"};
const STATUS_DOT={MISMATCH:"var(--bad)",NEEDS_REVIEW:"var(--warn)",OK:"var(--ok)"};
function freqMap(){try{return JSON.parse(localStorage.getItem(FREQ_KEY)||"{}");}catch(_){return {};}}
function recordStatusUse(st){const m=freqMap();m[st]=(m[st]||0)+1;try{localStorage.setItem(FREQ_KEY,JSON.stringify(m));}catch(_){}}
function topStatuses(n=3){return Object.entries(freqMap()).sort((a,b)=>b[1]-a[1]).slice(0,n).map(([k,v])=>({key:k,count:v}));}
function pinRun(key){const el=$("#palQ");el.value=STATUS_ALIASES[key][0];$("#palField").classList.add("has-text");el.dispatchEvent(new Event("input",{bubbles:true}));palIdx=0;renderPalette();$("#palQ").focus();}
let CMDS=[],palIdx=0,palShown=[],palEmails=[],palRecent=[],palTimer=null,palSeq=0;
let palShips=[], palShipsLoaded=false;
function buildCmds(){
  const firstMismatch=state.items.find(r=>r.status==="MISMATCH");
  const firstReview=state.items.find(r=>r.status==="NEEDS_REVIEW");
  CMDS=[
    {t:"Open Enterprise Hub",sub:"Multi-inbox intake, AI triage & dispatch",ico:ICONS2.shield,k:"0",run:goGateway},
    {t:"Go to first mismatch",sub:firstMismatch?firstMismatch.email_id:"none in current view",ico:ICONS2.jump,run:()=>firstMismatch&&openEmail(firstMismatch.email_id)},
    {t:"Go to first review",sub:firstReview?firstReview.email_id:"none in current view",ico:ICONS2.jump,run:()=>firstReview&&openEmail(firstReview.email_id)},
    {t:"Toggle focus mode",sub:"Hide inbox · expand comparison",ico:ICONS2.focus,k:"F",run:toggleFocus},
    {t:"Switch theme",sub:"Currently "+curTheme()+": switch to "+(curTheme()==="dark"?"light":"dark"),ico:themeIconSvg(curTheme()),k:"T",run:toggleTheme},
    {t:"Toggle sidebar",sub:"Expand / collapse rail",ico:ICONS2.rail,k:"[",run:toggleRail},
    {t:"Open Advanced Shipment Workflow",sub:"Shipment-centric console",ico:ICONS2.ship,k:"2",run:goShipments},
    {t:"Keyboard shortcuts",sub:"Every shortcut in one place",ico:ICONS2.kbd,k:"?",run:openHelp},
    {t:"Refresh inbox",sub:"Re-fetch emails and KPIs",ico:ICONS2.refresh,k:"R",run:()=>{loadSummary();loadList();toast("Inbox refreshed");}},
    {t:"Clear filters",sub:"Reset inbox category & status",ico:ICONS2.clear,run:()=>clearFilters()},
    {t:"Reset panel split",sub:"Resize inbox back to default width",ico:ICONS2.split,run:()=>window.resetSplit&&window.resetSplit()}
  ];
}
function renderPalette(){
  const q=$("#palQ").value.trim().toLowerCase();
  let list;
  if(!q){ list=[...palRecent,...CMDS]; }
  else{
    const cmds=CMDS.filter(c=>(c.t+" "+(c.sub||"")).toLowerCase().includes(q));
    list=[...cmds,...palEmails,...palShipItems(q)];
  }
  palShown=list;
  if(palIdx>=palShown.length)palIdx=Math.max(0,palShown.length-1);
  if(!palShown.length){
    $("#palList").innerHTML=`<div class="palette-item" style="cursor:default">${q?"No results.":"No matching commands."}</div>`;
    return;
  }
  let html="";
  if(!q){
    const top=topStatuses(3);
    html+=`<div class="pal-group">Your frequent filters</div>`;
    html+=top.length
      ? `<div class="pal-pins">`+top.map(s=>`<button type="button" class="pal-pin" onclick="pinRun('${s.key}')"><span class="dot" style="background:${STATUS_DOT[s.key]}"></span>${STATUS_LABEL[s.key]}<span class="pal-pin-c">${s.count}</span></button>`).join("")+`</div>`
      : `<div class="pal-hint">Type a status like <b>mismatch</b> or <b>review</b>, or filter the inbox by status: the ones you use most get pinned here.</div>`;
  }
  let last=null;
  palShown.forEach((c,i)=>{
    const lbl=c.kind==="email"?"Emails":c.kind==="ship"?"Shipments":c.kind==="recent"?"Recent":"Commands";
    if(c.kind!==last){html+=`<div class="pal-group">${lbl}</div>`;last=c.kind;}
    html+=`<div class="palette-item ${i===palIdx?'active':''}" data-i="${i}" onmouseenter="palHover(${i})" onpointerdown="palRun(${i})" onclick="palRun(${i})">
      <div class="pi-ico">${c.ico}</div><div class="pi-txt"><b>${esc(c.t)}</b><span>${esc(c.sub||"")}</span></div>${c.k?`<span class="pi-kbd">${c.k}</span>`:""}</div>`;
  });
  $("#palList").innerHTML=html;
}
async function loadRecents(){
  const ids=recentIds();
  if(!ids.length){palRecent=[];renderPalette();return;}
  try{
    const got=await Promise.all(ids.map(id=>
      fetch(API+"/api/emails/"+encodeURIComponent(id)).then(r=>r.ok?r.json():null).catch(()=>null)));
    palRecent=got.filter(Boolean).map(d=>({kind:"recent",t:d.email.subject||"(no subject)",
      sub:`${d.email.email_id} · ${d.email.from||"-"} · ${(d.result&&d.result.status)||"not processed"}`,ico:ICONS2.clock,run:()=>openEmail(d.email.email_id)}));
  }catch(_){ palRecent=[]; }
  renderPalette();
}
async function palSearchEmails(q){
  const my=++palSeq, st=statusForQuery(q);
  const url= st ? API+"/api/emails?status="+st+"&limit=8"
                : API+"/api/emails?limit=8&q="+encodeURIComponent(q);
  try{
    const r=await (await fetch(url)).json();
    if(my!==palSeq)return;
    palEmails=(r.items||[]).map(e=>({kind:"email",t:e.subject||"(no subject)",
      sub:`${e.email_id} · ${e.from||"-"} · ${e.status}`,ico:ICONS2.mail,run:()=>openEmail(e.email_id)}));
  }catch(_){ if(my!==palSeq)return; palEmails=[]; }
  renderPalette();
}
/* Shipment hits. The Lifecycle tab searches through this same palette, so the
   shared list carries shipments next to emails and commands. Loaded once and
   filtered locally: the list is small and this keeps the keystroke path sync. */
function shipHay(s){
  return [s.shipment_key, s.reference_number,
    s.si_latest && s.si_latest.filename, s.bl_latest && s.bl_latest.filename]
    .filter(Boolean).join(" ").toLowerCase();
}
function palShipItems(q){
  if(!palShipsLoaded) return [];
  return palShips.filter(s=>shipHay(s).includes(q)).slice(0,8).map(s=>({
    kind:"ship", ico:ICONS2.ship, t:s.shipment_key,
    sub:(s.si_latest && s.si_latest.filename ? s.si_latest.filename+" · " : "")+
        (s.mismatch_count>0 ? s.mismatch_count+" mismatch"+(s.mismatch_count>1?"es":"") : "OK"),
    run:()=>{ location.hash="#/s/"+s.id; if(window.switchView) window.switchView("lifecycle"); }
  }));
}
function palEnsureShips(){
  if(palShipsLoaded) return;
  const my=palSeq;
  fetch(API+"/shipments").then(r=>r.ok?r.json():null).then(d=>{
    palShips=(d && d.shipments)||[]; palShipsLoaded=true;
    if(my===palSeq && !$("#palette").hidden) renderPalette();
  }).catch(()=>{});
}
function palHover(i){
  palIdx=i;
  const items=document.querySelectorAll("#palList .palette-item");
  items.forEach((el,idx)=>el.classList.toggle("active",idx===i));
}
function openPalette(){buildCmds();palIdx=0;palEmails=[];palRecent=[];palSeq++;clearTimeout(palTimer);$("#palQ").value="";$("#palField").classList.remove("has-text");$("#palette").hidden=false;renderPalette();loadRecents();$("#palQ").focus();}
/* Closing must also drop focus: the input lives inside a hidden container, and a
   focused-but-hidden field would make isTyping() swallow every later shortcut. */
function closePalette(){
  $("#palette").hidden=true;
  const a=document.activeElement;
  if(a && a.closest && a.closest("#palette") && a.blur) a.blur();
}
function togglePalette(){ $("#palette").hidden ? openPalette() : closePalette(); }
function palMove(d){const n=palShown.length;if(!n)return;palIdx=(palIdx+d+n)%n;renderPalette();const a=$("#palList").querySelector(".palette-item.active");if(a)a.scrollIntoView({block:"nearest"});}
function palRun(i){
  const idx = (typeof i === "number" && !isNaN(i)) ? i : palIdx;
  const c = palShown[idx];
  if(!c) return;
  const st = statusForQuery($("#palQ").value.trim().toLowerCase());
  if(st && c.kind === "email") recordStatusUse(st);
  closePalette();
  if(c.run) c.run();
}
function palOnInput(){
  const raw=$("#palQ").value, q=raw.trim();
  const f=$("#palField"); if(f)f.classList.toggle("has-text",!!raw);
  clearTimeout(palTimer); palSeq++;
  if(!q){palEmails=[];palIdx=0;renderPalette();return;}
  palEmails=[];renderPalette();
  palTimer=setTimeout(()=>palSearchEmails(q),140);
  palEnsureShips();
}
$("#palQ").addEventListener("input",palOnInput);
$("#palClear").addEventListener("mousedown",e=>{e.preventDefault();$("#palQ").value="";palIdx=0;palOnInput();$("#palQ").focus();});
$("#palette").addEventListener("click",e=>{if(e.target.id==="palette")closePalette();});

/* Delegated click handler on the palette list for 100% click reliability */
$("#palList").addEventListener("click",e=>{
  const it=e.target.closest&&e.target.closest(".palette-item");
  if(it && it.dataset.i !== undefined){
    palRun(+it.dataset.i);
  }
});

loadSummary().then(loadList).then(()=>{const h=decodeURIComponent(location.hash.slice(1));if(h&&h!=="review"&&h!=="gateway"&&!h.startsWith("lifecycle")&&!h.startsWith("/"))openEmail(h); else renderEmptyDetail();});
loadIntegrations();   /* stored API results only: no reprocessing */



/* Inline `onclick` attributes in the console markup resolve against the global
   scope, so the handlers they name are published here. The shared chrome is
   published too: the Lifecycle module delegates to it instead of binding the
   same buttons and shortcut keys a second time. */
Object.assign(window, {
  clearFilters,
  closeHelp,
  closePalette,
  esc,
  flashSaved,
  loadList,
  loadSummary,
  openEmail,
  openHelp,
  openPalette,
  palHover,
  palRun,
  pinRun,
  processEmail,
  sendReview,
  toast,
  toggleAtt,
  togglePalette,
  toggleRail,
  toggleTheme
});
})();
