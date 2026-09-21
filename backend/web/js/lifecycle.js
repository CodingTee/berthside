/* ==========================================================================
   ShipSync Enterprise Hub · Shipment Lifecycle & Resolution Center Module
   ========================================================================== */

(function(){
const API = "";
const FIELDS = [
  ["shipper","Shipper"],
  ["consignee","Consignee"],
  ["notify_party","Notify Party"],
  ["port_of_loading","Port of Loading"],
  ["port_of_discharge","Port of Discharge"],
  ["container_count","Container Count"],
  ["gross_weight_kg","Gross Weight"],
];
const FMAP = Object.fromEntries(FIELDS.map(f => [f[0], f[1]]));
const RESOLVE_STATES = ["VERIFIED","NEEDS_REVIEW","IN_PROGRESS","PENDING_APPROVAL","RESOLVED"];
const SEV = {consignee:"high", notify_party:"high", gross_weight_kg:"med", container_count:"med", port_of_loading:"low", port_of_discharge:"low", shipper:"low"};
function sevOf(f){ return SEV[f] || "low"; }
const SEV_TITLE = {high:"High customs risk: party name or entity code mismatch", med:"Medium risk: tally or weight discrepancy", low:"Low risk: routing descriptor difference"};

function tokenize(v){
  return String(v==null?"":v).match(/[A-Za-z0-9]+(?:[.'’\/-][A-Za-z0-9]+)*|\s+|[^\sA-Za-z0-9]+/g) || [];
}
function lcsDiff(a,b){
  const n=a.length, m=b.length;
  if(!n||!m) return null;
  if(n*m>60000) return null;
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
function diffCells(si,bl){
  const ops = lcsDiff(tokenize(si),tokenize(bl));
  if(!ops) return [ esc(si==null?"-":si), esc(bl==null?"-":bl) ];
  let L="",R="";
  for(let k=0;k<ops.length;k++){
    const t=ops[k][0], raw=ops[k][1], h=esc(raw);
    const blank=/^\s+$/.test(raw);
    if(t==="same"){ L+=h; R+=h; }
    else if(t==="del") L+= blank?h:'<span class="tk del">'+h+'</span>';
    else R+= blank?h:'<span class="tk ins">'+h+'</span>';
  }
  return [ L||"-", R||"-" ];
}

let state = { shipments: [], current: null, overview: null, versions: [], issues: [], status: null, diff: null, draft: null, dashSel: 0, dashFilterWork: "all" };
const view = document.getElementById("lcView");

/* ---------- helpers ---------- */
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function isTyping(t){ return t && (t.tagName==="INPUT"||t.tagName==="TEXTAREA"||t.tagName==="SELECT"); }
function fmtVal(field, val){
  if(val===null||val===undefined||val==="") return "-";
  if(field==="gross_weight_kg"){ const n=Number(val); return (isFinite(n)?n.toLocaleString():"")+" kg"; }
  if(field==="container_count"){ const n=Number(val); return isFinite(n)?String(n):String(val); }
  return String(val);
}
function statusKind(s){
  s=String(s||"").toUpperCase();
  if(["VERIFIED","RESOLVED","APPROVED"].includes(s)) return "ok";
  if(["PENDING_APPROVAL","NEEDS_REVIEW","IN_PROGRESS","SUGGESTED","OPEN"].includes(s)) return "warn";
  if(["REJECTED"].includes(s)) return "bad";
  return "neu";
}
function pill(text, kind){ return '<span class="pill '+(kind||"neu")+'">'+esc(text)+'</span>'; }
function statusPill(s){ return pill(s, statusKind(s)); }

/* Independent resolution signal derived from the per-shipment counts the API
   already returns. This is NOT the same axis as Verification (which answers
   "do the SI/BL agree?"): this answers "has a human acted on the issues?". */
function resolutionState(sh){
  const pend=sh.pending_count||0, res=sh.resolved_count||0, mm=sh.mismatch_count||0;
  if(res>0)   return {label:"Resolved",   kind:"ok",   sub: res+(pend?(" · "+pend+" pending"):"")};
  if(pend>0)  return {label:"Pending",    kind:"warn", sub: pend+(res?(" · "+res+" resolved"):"")};
  if(mm>0)    return {label:"Unresolved", kind:"bad",  sub: mm+" mismatch"+(mm>1?"es":"")};
  return {label:"Clear", kind:"neu", sub:""};
}
function resRank(r){ return ({Resolved:4, Pending:3, Unresolved:2, Clear:1})[r.label] || 0; }
/* Show the trade lane instead of reprinting the reference (which the key row
   already displays). Falls back to the shipper if ports are absent. */
function routeOf(si,bl){
  const f=(si&&si.extracted_fields)||(bl&&bl.extracted_fields)||{};
  const a=String(f.port_of_loading||"").trim(), b=String(f.port_of_discharge||"").trim();
  if(a||b) return (a||"?")+" → "+(b||"?");
  const p=String(f.shipper||f.consignee||"").trim();
  return p;
}
/* Strip the noisy shared prefix ("attachments/email_046_") from stored names. */
function fmtFile(name){ return name? String(name).replace(/^attachments\//,"").replace(/^email_\d+_/,"") : "-"; }
function fmtTime(d){ try{ return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}); }catch(_){ return ""; } }

/* ---------- keyboard map for the dashboard filters ---------- */
/* One table drives the chip shortcut, the hover bubble and the `?` cheat-sheet
   so the three can never disagree. Plain letters pick a Verification state,
   Shift+letter picks a Resolution state: two independent axes, two groups. */
/* Platform-aware modifier label: the same convention MODK already uses for the
   search key: a Windows user reads "Shift", a Mac user reads the ⇧ glyph. The
   bare "⇧" was unreadable for anyone not raised on Mac keycaps, so the word wins.
   SHIFT_TIP appends the "|" the tooltip and cheat-sheet renderers split on, so the
   modifier and the letter become two separate keycaps: exactly how the Search
   button already draws [Ctrl][K]. */
const IS_MAC=/Mac|iPhone|iPad/.test(navigator.platform||navigator.userAgent||"");
const MODK=IS_MAC?"\u2318":"Ctrl";
const SHIFTK=IS_MAC?"\u21e7":"Shift";
const SHIFT_TIP=SHIFTK+"|";
/* One-keycap form ("Shift+A") for the palette rows and the pinned filter chips,
   which render a single badge: a second keycap would double their width. */
const kbdOne=s=>String(s||"").replace("|","+");
/* Dashboard filter shortcuts. Plain letter = Verification axis, Shift+letter =
   Resolution axis. The chip tooltip, the `?` cheat-sheet and the keydown handler all
   read from here, so they can never disagree. */
const FILTER_KEYS = {
  ver: {
    all:    ["A", "Verification filter", "Show every shipment regardless of SI<->BL agreement"],
    ok:     ["V", "Verification filter", "Show only shipments whose SI and BL already agree"],
    review: ["N", "Verification filter", "Show only shipments with a detected mismatch"]
  },
  res: {
    all:        [SHIFT_TIP+"A", "Resolution filter", "Ignore the resolution state (show everything)"],
    clear:      [SHIFT_TIP+"C", "Resolution filter", "Show shipments with no open issues"],
    unresolved: [SHIFT_TIP+"U", "Resolution filter", "Show issues detected but not yet actioned"],
    pending:    [SHIFT_TIP+"P", "Resolution filter", "Show suggestions waiting for your decision"],
    resolved:   [SHIFT_TIP+"R", "Resolution filter", "Show shipments where every issue is resolved"]
  }
};
/* Human label + dot colour per filter. Drives both the searchable "Filters"
   group and the pinned "Your frequent filters" row. */
const FILTER_LABEL = {
  "ver:all":       ["Any verification", "var(--muted-2)"],
  "ver:ok":        ["Verified",         "var(--ok)"],
  "ver:review":    ["Needs review",     "var(--warn)"],
  "res:all":       ["Any resolution",   "var(--muted-2)"],
  "res:clear":     ["Clear",            "var(--ok)"],
  "res:unresolved":["Unresolved",       "var(--bad)"],
  "res:pending":   ["Pending",          "var(--warn)"],
  "res:resolved":  ["Resolved",         "var(--ok)"]
};
/* Free-text triggers, so a filter can be found by word and not just by name. */
const FILTER_ALIASES = {
  "ver:ok":        ["verified","verify","ok","agree","match","clean","pass"],
  "ver:review":    ["review","mismatch","mismatches","needs","flag","issue","problem","differ"],
  "res:clear":     ["clear","no issues","nothing open","clean"],
  "res:unresolved":["unresolved","open","todo","outstanding"],
  "res:pending":   ["pending","approval","waiting","suggest"],
  "res:resolved":  ["resolved","done","closed","complete","handled"]
};
/* Which filters you actually reach for: the same pinning idea as the Review
   Console's status chips, so both palettes learn from real use. */
const FREQ_KEY="sdoc_ship_filter_freq";
function filterFreq(){try{return JSON.parse(localStorage.getItem(FREQ_KEY)||"{}");}catch(_){return {};}}
function recordFilterUse(grp,val){
  const m=filterFreq(), k=grp+":"+val;
  m[k]=(m[k]||0)+1;
  try{localStorage.setItem(FREQ_KEY,JSON.stringify(m));}catch(_){}
}
function topFilters(n){
  return Object.keys(filterFreq())
    .filter(k=>FILTER_LABEL[k])
    .sort((a,b)=>filterFreq()[b]-filterFreq()[a])
    .slice(0,n)
    .map(k=>({key:k,count:filterFreq()[k]}));
}
/* Recently opened shipments, so the palette can offer real recents like the
   Review Console does for emails. */
const SHIP_RECENT_KEY="sdoc_ship_recent_v1";
function recentShipIds(){try{return JSON.parse(localStorage.getItem(SHIP_RECENT_KEY)||"[]");}catch(_){return [];}}
function recordRecentShip(id){
  let a=recentShipIds().filter(x=>x!==id);
  a.unshift(id); a=a.slice(0,6);
  try{localStorage.setItem(SHIP_RECENT_KEY,JSON.stringify(a));}catch(_){}
}
/* Shortcut metadata for action buttons, keyed by data-act: [key, title, desc]. */
const ACT_KEYS = {
  "compare":      ["C",      "Compare versions",     "Run the deterministic 7-field comparison of the two selected versions"],
  "swap":         ["S",      "Swap sides",           "Exchange the From and To versions"],
  "suggest":      [SHIFT_TIP+"S", "Suggest correction",  "Generate a deterministic correction for the first open issue"],
  "approve":      ["A",      "Approve correction",   "Accept the suggested correction for the first open issue"],
  "reject":       ["X",      "Reject correction",    "Reject the suggested correction for the first open issue"],
  "draft":        ["D",      "Generate draft",       "Build the working BL draft from the approved resolutions"],
  "refresh-dash": ["R",      "Refresh list",         "Re-fetch the shipment list from the API"]
};
/* Build the full data-tip* attribute string for an action button. */
function actAttrs(act){
  const m=ACT_KEYS[act];
  if(!m) return "";
  return ' data-tip="'+esc(m[1])+'" data-tip-desc="'+esc(m[2])+'"'+
         ' data-tip-kbd="'+esc(m[0])+'" data-tip-pos="bottom"';
}
/* Filter chips re-render the dashboard, so keep the setter in one place. */
function setDashFilter(grp, val){
  state.dashFilter = state.dashFilter || {ver:"all", res:"all"};
  state.dashFilter[grp]=val;
  recordFilterUse(grp,val);
  state.dashSel=0;
  /* Called from the palette this may run while a shipment is open: hand back to
     the router instead of painting the dashboard underneath the detail view. */
  if(state.current) go("#/"); else paintDashboard();
}
/* Click a rendered action button (so keyboard and mouse share one code path).
   Returns false when the control is absent or disabled, e.g. the draft button
   before any suggestion has been approved. */
function pressAct(name){
  const b=document.querySelector('[data-act="'+name+'"]');
  if(!b || b.disabled) return false;
  b.click();
  return true;
}
function refreshView(){
  if(state.current) openShipment(state.current.id);
  else renderDashboard();
  toast("Refreshed from the API","ok");
}

async function api(path, opts){
  const res = await fetch(API+path, Object.assign({headers:{"Content-Type":"application/json"}}, opts||{}));
  let data=null; try{ data = await res.json(); }catch(_){}
  if(!res.ok){
    const msg = (data && (data.detail||data.message)) || ("HTTP "+res.status);
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return data;
}

function toast(msg, type){
  const t=document.getElementById("toast");
  t.className="toast"+(type?" "+type:"");
  document.getElementById("toastMsg").textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer); t._timer=setTimeout(()=>t.classList.remove("show"), 3400);
}

function icon(name){
  const I={
    ship:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17h18l-2 4H5zM5 17V9a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v8"/><path d="M12 7V3M3 13h18"/></svg>',
    layers:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5M3 17l9 5 9-5"/></svg>',
    compare:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M6 9v6M18 3a3 3 0 0 1 0 6M18 15a3 3 0 0 1 0 6M18 9a9 9 0 0 1-9 9"/></svg>',
    check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
    cross:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    alert:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
    doc:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
    bolt:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg>',
    home:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>',
    mail:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
    sun:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7"/></svg>',
    moon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
    swap:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h13M13 4l4 4-4 4M20 16H7M11 12l-4 4 4 4"/></svg>',
    chev:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
    rail:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18M3 12h12M3 19h18"/></svg>',
    refresh:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/></svg>',
    shield:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    clock:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5.2l3.3 2"/></svg>',
    filter:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18l-7 8.4V20l-4-2.2v-4.4z"/></svg>'
  };
  return I[name]||"";
}
function emptyCard(title, sub, ic){
  return '<div class="empty">'+(ic||icon("doc"))+'<span class="big">'+esc(title)+'</span>'+(sub?'<span>'+esc(sub)+'</span>':'')+'</div>';
}
function errorCard(title, msg){
  return '<div class="panel"><div class="panel-body">'+icon("alert")+
    '<div class="big" style="font-weight:650;margin:6px 0 2px">'+esc(title)+'</div>'+
    '<div class="muted small">'+esc(msg||"")+'</div></div></div>';
}

/* ---------- routing ---------- */
function route(){
  const h = location.hash || "#/";
  if(h.indexOf("#/s/")===0){
    const seg = h.slice(4).trim();
    const id = parseInt(seg, 10);
    if(id>0 && String(id)===seg){ openShipment(id); return; }
    // If string key like SHP-002, resolve by key
    api("/shipments/by-key/" + encodeURIComponent(seg)).then(ov => {
      if(ov && ov.id) openShipment(ov.id);
      else if(id>0) openShipment(id);
      else renderDashboard();
    }).catch(() => {
      if(id>0) openShipment(id);
      else renderDashboard();
    });
    return;
  }
  state.dashSel = 0;
  renderDashboard();
}
/* router handled by master console */

/* ---------- dashboard ---------- */
async function renderDashboard(){
  view.innerHTML = emptyCard("Loading shipments…","",icon("ship"));
  try{
    const data = await api("/shipments");
    state.shipments = data.shipments || [];
    paintDashboard();
  }catch(e){
    view.innerHTML = errorCard("Could not load shipments", e.message);
  }
}
function paintDashboard(){
  const s = state.shipments;
  view.classList.remove("dash-mode");   /* loading / empty states scroll normally */
  state.current = null;   /* leaving detail view: keeps the palette's context commands accurate */
  clearSubnav();
  document.getElementById("lcCrumbs").innerHTML = '<b>Shipments</b>';
  if(s.length===0){
    view.innerHTML =
      '<div class="panel"><div class="panel-body">'+
      emptyCard("No shipments yet",
        "Shipments are created when inbox emails are processed (SI + BL compared). Run processing, then return here.",
        icon("ship"))+
      '</div></div>';
    return;
  }
  const total=s.length;
  state.dashFilterWork = state.dashFilterWork || "all";
  state.dashFilter = state.dashFilter || {ver:"all", res:"all"};
  state.dashSort   = state.dashSort   || {key:"id", dir:1};
  state.dashUpdated = new Date();

  function matchesWorkFilter(sh, f) {
    if (!f || f === "all") return true;
    if (f === "needs_attention") {
      return (sh.status === "NEEDS_ATTENTION" || (sh.mismatch_count || 0) > 0 ||
        (sh.missing_documents || []).length > 0 || (sh.reasons || []).length > 0);
    }
    if (f === "mismatch") {
      return (sh.mismatch_count || 0) > 0;
    }
    if (f === "needs_review") {
      return (sh.status === "NEEDS_REVIEW") ||
        (sh.reasons || []).some(r => /needs review|manual/i.test(r)) ||
        (sh.actions || []).includes("MANUAL_REVIEW");
    }
    if (f === "resolved") {
      const res = resolutionState(sh);
      return res.label === "Resolved" || sh.resolution_state === "RESOLVED" ||
        ((sh.resolved_count || 0) > 0 && (sh.pending_count || 0) === 0 && (sh.mismatch_count || 0) === 0);
    }
    return true;
  }

  const cAll = s.length;
  const cAttn = s.filter(x => matchesWorkFilter(x, "needs_attention")).length;
  const cMis = s.filter(x => matchesWorkFilter(x, "mismatch")).length;
  const cRev = s.filter(x => matchesWorkFilter(x, "needs_review")).length;
  const cRes = s.filter(x => matchesWorkFilter(x, "resolved")).length;

  // ---- filter ----
  const fv=state.dashFilter.ver, fr=state.dashFilter.res;
  let rowsArr = s.filter(sh=>{
    if (!matchesWorkFilter(sh, state.dashFilterWork)) return false;
    const res=resolutionState(sh);
    if(fv==="ok"    && sh.mismatch_count>0) return false;
    if(fv==="review"&& sh.mismatch_count===0) return false;
    if(fr!=="all"  && res.label.toLowerCase()!==fr) return false;
    return true;
  });
  // ---- sort ----
  const so=state.dashSort;
  rowsArr.sort((a,b)=>{
    let c=0;
    if(so.key==="key") c=String(a.shipment_key).localeCompare(String(b.shipment_key));
    else if(so.key==="ver") c=(a.mismatch_count>0?1:0)-(b.mismatch_count>0?1:0);
    else if(so.key==="res") c=resRank(resolutionState(a))-resRank(resolutionState(b));
    else c=(a.id||0)-(b.id||0);
    return c*so.dir;
  });

  const withMismatch=s.filter(x=>x.mismatch_count>0).length;
  const pending=s.filter(x=>x.pending_count>0).length;
  const resolved=s.filter(x=>x.resolved_count>0).length;
  const needsAttention=s.filter(x=>(x.status==="NEEDS_ATTENTION"||x.status==="NEEDS_REVIEW"||x.mismatch_count>0)).length;

  const sm=k=> (state.dashSort.key===k ? (state.dashSort.dir>0?" ▲":" ▼") : "");
  /* Each chip carries its own shortcut + hover bubble, sourced from FILTER_KEYS. */
  const chip=(grp,val,label)=>{
    const meta=FILTER_KEYS[grp][val]||["",""];
    const axis=(grp==="ver"?"Verification":"Resolution");
    return '<button class="chip'+(state.dashFilter[grp]===val?" on":"")+'" data-act="f'+grp+'" data-val="'+val+'"'+
      ' data-tip="'+esc(axis)+" \u00b7 "+esc(label)+'" data-tip-desc="'+esc(meta[2]||meta[1])+'"'+
      ' data-tip-kbd="'+esc(meta[0])+'" data-tip-pos="bottom" aria-label="Filter: '+esc(axis)+" "+esc(label)+'">'+
      esc(label)+'</button>';
  };

  const curW = state.dashFilterWork || "all";
  const wChip = (val, label, count) =>
    '<button class="chip'+(curW===val?" on":"")+'" data-act="fwork" data-val="'+val+'" style="display:inline-flex;align-items:center;gap:6px">'+
      esc(label)+' <span style="background:rgba(255,255,255,0.12);padding:1px 6px;border-radius:10px;font-size:11px;font-weight:600">'+count+'</span>'+
    '</button>';

  /* Clamp the keyboard highlight to the rows that survived the filter, so the
     arrow keys can never point at a row that is no longer on screen. */
  state.dashSel = rowsArr.length ? Math.max(0, Math.min(state.dashSel|0, rowsArr.length-1)) : -1;
  const selIdx = state.dashSel;

  let rows="", ri=0;
  for(const sh of rowsArr){
    const si=sh.si_latest, bl=sh.bl_latest;
    const ver = sh.status || "VERIFIED";
    const res = resolutionState(sh);
    const rt  = routeOf(si,bl);
    rows += '<tr data-id="'+sh.id+'"'+(ri++===selIdx?' class="sel"':'')+'>'+
      '<td title="'+esc(sh.shipment_key)+(rt?' \u00b7 '+esc(rt):'')+'"><div class="key">'+esc(sh.shipment_key)+'</div><div class="sub">'+esc(rt||(sh.reference_number||"no ref"))+'</div></td>'+
      '<td class="mono" title="'+esc(si?si.filename:"")+'">'+esc(fmtFile(si?si.filename:null))+'</td>'+
      '<td class="mono" title="'+esc(bl?bl.filename:"")+'">'+esc(fmtFile(bl?bl.filename:null))+'</td>'+
      (()=>{const vtxt=ver+' \u00b7 '+(sh.mismatch_count>0? sh.mismatch_count+" mismatch"+(sh.mismatch_count>1?"es":""):"OK");
        const rtxt=res.label+' \u00b7 '+(res.sub||"no issues");
        return '<td title="'+esc(vtxt)+'">'+statusPill(ver)+' <span class="muted small">'+esc(sh.mismatch_count>0? sh.mismatch_count+" mismatch"+(sh.mismatch_count>1?"es":""):"OK")+'</span></td>'+
      '<td title="'+esc(rtxt)+'">'+pill(res.label,res.kind)+' <span class="muted small">'+esc(res.sub||"no issues")+'</span></td>';})()+
      '</tr>';
  }

  view.classList.add("dash-mode");
  view.innerHTML =
    '<div class="kpi-bar" style="grid-template-columns:repeat(3,1fr)">'+
      kpi(total,"Active","accent")+
      kpi(needsAttention,"Needs Attention")+
      kpi(resolved,"Resolved")+
    '</div>'+
    '<div class="panel"><div class="panel-head"><div class="ttl">'+icon("ship")+'Shipment Dashboard</div>'+
      '<div class="dash-head"><span class="count">'+rowsArr.length+' / '+total+' shipments</span>'+
      '<div class="dt-hint" data-tip="Move through the list" data-tip-desc="Arrow keys move the row highlight, Enter opens the shipment" data-tip-pos="bottom"><b class="hk">\u2191</b><b class="hk">\u2193</b>select<b class="hk">Enter</b>open</div>'+
      '<span class="updated" id="dashUpdated"></span>'+
      '<button class="btn ghost sm" data-act="refresh-dash" aria-label="Refresh shipments"'+actAttrs("refresh-dash")+'>'+icon("refresh")+'Refresh</button></div>'+
      '</div>'+
      '<div class="dash-tools" style="flex-wrap:wrap;gap:12px">'+
        '<div class="dt-group" style="flex-wrap:wrap;gap:6px"><span class="dt-label" style="font-weight:600">Worklist:</span>'+
          wChip("all","All",cAll)+wChip("needs_attention","Needs Attention",cAttn)+wChip("mismatch","Mismatch",cMis)+wChip("needs_review","Needs Review",cRev)+wChip("resolved","Resolved",cRes)+
        '</div>'+
        '<div class="dt-group" style="display:none"><span class="dt-label">Verification</span>'+chip("ver","all","All")+chip("ver","ok","Verified")+chip("ver","review","Needs review")+'</div>'+
        '<div class="dt-group" style="display:none"><span class="dt-label">Resolution</span>'+chip("res","all","All")+chip("res","clear","Clear")+chip("res","unresolved","Unresolved")+chip("res","pending","Pending")+chip("res","resolved","Resolved")+'</div>'+
      '</div>'+
      '<div class="dash-scroll" id="dashScroll">'+
      '<table class="list"><thead><tr>'+
        '<th data-sort="key" style="cursor:pointer">Shipment'+sm("key")+'</th>'+
        '<th>Latest SI</th><th>Latest BL</th>'+
        '<th data-sort="ver" style="cursor:pointer">Verification'+sm("ver")+'</th>'+
        '<th data-sort="res" style="cursor:pointer">Resolution'+sm("res")+'</th>'+
      '</tr></thead><tbody>'+rows+'</tbody></table>'+
      (rowsArr.length===0?'<div class="muted small" style="padding:14px 16px">No shipments match the current filters.</div>':'')+
      '</div>'+
    '</div>';

  const uEl=document.getElementById("dashUpdated");
  if(uEl) uEl.textContent = "Updated "+fmtTime(state.dashUpdated);

  view.querySelectorAll("tbody tr").forEach((tr,i)=>{
    tr.addEventListener("click",()=>{ state.dashSel=i; location.hash = "#/s/"+tr.dataset.id; });
  });
}
function kpi(num,label,accent){
  return '<div class="kpi '+(accent||"")+'"><div class="num">'+num+'</div><div class="lbl">'+esc(label)+'</div></div>';
}

/* ---------- shipment detail ---------- */
async function openShipment(id){
  view.classList.remove("dash-mode");
  view.innerHTML = emptyCard("Loading shipment #"+id+"…","",icon("layers"));
  try{
    const [det, iss, st, ov] = await Promise.all([
      api("/shipments/"+id),
      api("/shipments/"+id+"/issues"),
      api("/shipments/"+id+"/resolution-status").catch(()=>({status:"UNKNOWN",total_issues:0,resolved:0,pending_approval:0,rejected:0})),
      api("/shipments/"+id+"/overview").catch(()=>null)
    ]);
    state.current = det;
    state.overview = ov;
    recordRecentShip(det.id);
    state.versions = (det.documents||[]).flatMap(d=>d.versions||[]);
    state.issues = iss||[];
    state.status = st;
    state.diff = null;
    state.draft = null;
    for(const it of state.issues){
      try{
        const d = await api("/issues/"+it.id);
        it._res = d.resolution;
        if(d.resolution && d.resolution.corrected_draft_path && !state.draft){
          state.draft = {
            corrected_draft_path: d.resolution.corrected_draft_path,
            applied_resolutions: [d.resolution.id],
            source_bl_version_id: "-"
          };
        }
      }
      catch(_){ it._res = null; }
    }
    renderDetail();
  }catch(e){
    if(e.status===404){
      view.innerHTML = errorCard("Shipment not found", "No shipment with id "+id+". It may not have been generated yet: process inbox emails first.");
    }else{
      view.innerHTML = errorCard("Could not load shipment", e.message);
    }
  }
}

function findVer(vid){ return state.versions.find(v=>v.id===vid); }
function verLabel(v){ return v? (v.doc_type+" v"+v.version_number) : "?"; }
/* One side of the compare bar: role ("Baseline" / "Counterpart") + detected doc
   type, then the version <select>. `selId` must stay `fromSel` / `toSel`: the
   diff, the swap button and the auto-compare all look the selects up by id. */
function cmpTile(selId, role, selectedId, opts){
  const v=findVer(+selectedId)||{};
  const dt=(v.doc_type||"-").toUpperCase();
  return '<label class="cmp-tile" data-doc="'+esc(dt)+'">'+
    '<span class="cmp-head">'+
      '<span class="cmp-dot"></span>'+
      '<span class="cmp-type" data-type-slot="'+selId+'">'+esc(dt)+'</span>'+
      '<span class="cmp-role">'+esc(role)+'</span>'+
    '</span>'+
    '<span class="cmp-field">'+
      '<select id="'+selId+'" class="cmp-sel" aria-label="'+esc(role)+' version">'+opts+'</select>'+
      '<span class="cmp-chev">'+icon("chev")+'</span>'+
    '</span>'+
  '</label>';
}
/* Keep the tile chrome honest when the <select> changes: the type chip and the
   dot colour must follow whatever document is actually selected. */
function syncCompareBar(){
  ["fromSel","toSel"].forEach(function(id){
    const sel=document.getElementById(id);
    if(!sel) return;
    const tile=sel.closest(".cmp-tile");
    if(!tile) return;
    const opt=sel.options[sel.selectedIndex];
    const dt=(opt&&opt.dataset.type)||"-";
    tile.dataset.doc=dt;
    const slot=tile.querySelector('.cmp-type[data-type-slot="'+id+'"]');
    if(slot) slot.textContent=dt;
  });
}
function swapCompare(){
  const fs=document.getElementById("fromSel"), ts=document.getElementById("toSel");
  if(!fs||!ts) return;
  const a=fs.value; fs.value=ts.value; ts.value=a;
  syncCompareBar();
  compareVersions();
}

/* ---------- topbar section nav ------------------------------------------
   Titles mirror each panel's own heading so the tab and the panel can never drift. */
const subnavEl = document.getElementById("lcSubnav");
function clearSubnav(){ if(subnavEl){ subnavEl.hidden = true; subnavEl.innerHTML = ""; } }
function setSubnav(items){
  if(!subnavEl) return;
  subnavEl.innerHTML = items.map(it =>
    '<a href="#'+it[0]+'" data-sec="'+it[0]+'" title="'+esc(it[2])+'">'+esc(it[1])+'</a>').join("");
  subnavEl.hidden = false;
  requestAnimationFrame(scrollSpy);
}
/* Scroll the panel to the top of #view itself. Computing the offset is predictable:
   scrollIntoView on a nested scroller depends on ancestor overflow and can land
   anywhere, and the offset stays correct while a smooth scroll is in flight. */
function goSection(id){
  const el = document.getElementById(id); if(!el) return false;
  const box = document.getElementById("lcView"); if(!box) return false;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const top = el.getBoundingClientRect().top - box.getBoundingClientRect().top
              + box.scrollTop - 8;
  box.scrollTo({top: Math.max(0, top), behavior: reduce ? "auto" : "smooth"});
  subnavEl && subnavEl.querySelectorAll("a").forEach(a =>
    a.classList.toggle("on", a.dataset.sec === id));
  return false;
}
/* Park the highlighted row inside the dashboard's own scroller. `block:"nearest"`
   scrollIntoView will happily tuck the row underneath the sticky <thead>, so the
   offsets are computed explicitly - the same approach as goSection(). */
function keepRowVisible(row){
  const box = document.getElementById("dashScroll");
  if(!box){ row.scrollIntoView({block:"nearest"}); return; }
  const head = box.querySelector("thead");
  const headH = head ? head.getBoundingClientRect().height : 0;
  const rb = row.getBoundingClientRect(), cb = box.getBoundingClientRect();
  const top = rb.top - cb.top + box.scrollTop;
  const bot = rb.bottom - cb.top + box.scrollTop;
  if(rb.top < cb.top + headH) box.scrollTo({top: Math.max(0, top - headH - 6), behavior:"auto"});
  else if(rb.bottom > cb.bottom) box.scrollTo({top: bot - box.clientHeight + 6, behavior:"auto"});
}
/* Highlight whichever section is currently at the top of the scroll container. */
function scrollSpy(){
  if(!subnavEl || subnavEl.hidden) return;
  const box = document.getElementById("lcView"); if(!box) return;
  const top = box.getBoundingClientRect().top;
  const links = subnavEl.querySelectorAll("a");
  let best = null;
  links.forEach(a => {
    const sec = document.getElementById(a.dataset.sec);
    if(sec && sec.getBoundingClientRect().top - top <= 28) best = a;
  });
  /* Nothing crossed the line yet → the first section is the one you are looking at. */
  if(!best && links.length) best = links[0];
  links.forEach(a => a.classList.toggle("on", a === best));
}
if(subnavEl){
  subnavEl.addEventListener("click", e => {
    const a = e.target.closest("a[data-sec]"); if(!a) return;
    e.preventDefault(); goSection(a.dataset.sec);
  });
  const box = document.getElementById("lcView");
  if(box) box.addEventListener("scroll", scrollSpy, {passive:true});
  window.addEventListener("resize", scrollSpy);
}

async function sendShipmentReview(act){
  const sh = state.current;
  if(!sh) return;
  const noteEl = document.getElementById("ship-rev-note");
  const note = (noteEl && noteEl.value.trim()) || null;
  const emails = (state.overview && state.overview.source_emails) || [];
  const emailId = emails[0] && emails[0].email_id;

  const kind = act.replace("rev-", ""); // confirm, pass, mismatch, back
  if(emailId){
    const body = { action: kind==="confirm" ? "confirm" : "override" };
    if(kind==="pass") body.status = "OK";
    else if(kind==="mismatch"){
      body.status = "MISMATCH";
      body.defect_fields = (state.overview && state.overview.mismatch_fields) || [];
    }
    else if(kind==="back") body.status = "NEEDS_REVIEW";
    if(note) body.note = note;
    try{
      await fetch(API + "/api/emails/" + encodeURIComponent(emailId) + "/review", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
      });
    }catch(_){}
  }
  const msgMap = {confirm:"Verdict confirmed.", pass:"Marked as passed.", mismatch:"Discrepancy flagged.", back:"Sent back for carrier review."};
  toast(msgMap[kind] || "Review decision recorded.", "ok");
  await openShipment(sh.id);
}

function renderDetail(){
  view.classList.remove("dash-mode");
  const sh = state.current;
  document.getElementById("lcCrumbs").innerHTML =
    '<a href="#/" data-tip="Shipment Dashboard" data-tip-desc="Back to the shipment list" data-tip-kbd="G" data-tip-pos="bottom">Shipments</a>'+
    ' <span class="muted">/</span> <b>'+esc(sh.shipment_key)+'</b>';

  const si=sh.si_latest, bl=sh.bl_latest;
  const res = resolutionState(sh);
  const rt  = routeOf(si,bl);
  const ov  = state.overview;

  // ---- 1. Shipment Header ----
  const header =
    '<div class="panel" id="sec-header"><div class="panel-body">'+
      '<div class="flex" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'+
        '<div><div class="key" style="font-size:16px;font-weight:700;letter-spacing:-0.2px">'+esc(sh.shipment_key)+'</div>'+
          '<div class="sub" style="margin-top:2px;font-size:12.5px;color:var(--muted)">Reference: <b>'+esc(sh.reference_number||"-")+'</b>'+(rt?' · Trade Lane: <b>'+esc(rt)+'</b>':'')+'</div></div>'+
        '<div class="flex" style="gap:8px;align-items:center">'+statusPill(sh.status||"VERIFIED")+pill(res.label,res.kind)+'</div>'+
      '</div>'+
      '<div style="height:10px"></div>'+
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;padding-top:8px;border-top:1px solid var(--border)">'+
        '<div class="kv"><span class="k">Current SI</span><span class="v mono">'+esc(fmtFile(si?si.filename:null))+'</span></div>'+
        '<div class="kv"><span class="k">Current BL</span><span class="v mono">'+esc(fmtFile(bl?bl.filename:null))+'</span></div>'+
        '<div class="kv"><span class="k">Discrepancies</span><span class="v" style="'+(sh.mismatch_count>0?'color:var(--bad);font-weight:600':'')+'">'+(sh.mismatch_count||0)+' detected</span></div>'+
        '<div class="kv"><span class="k">Pending / Resolved</span><span class="v">'+sh.pending_count+' / '+sh.resolved_count+'</span></div>'+
      '</div>'+
    '</div></div>';

  // ---- 2. Current SI ↔ Current BL Comparison (Primary Operational Table) ----
  let cmpTableHtml = "";
  if(si && bl){
    const fResults = (ov && ov.verification && ov.verification.field_results) || [];
    const siFields = si.extracted_fields || {};
    const blFields = bl.extracted_fields || {};
    let rows = "";
    for(const [fKey, fLabel] of FIELDS){
      const fr = fResults.find(x => x.field === fKey);
      const siVal = fr ? fr.si_value : siFields[fKey];
      const blVal = fr ? fr.bl_value : blFields[fKey];
      const isMissing = (siVal == null || siVal === "") || (blVal == null || blVal === "");
      const isMatch = fr ? fr.match : (siVal != null && blVal != null && String(siVal).trim().toLowerCase() === String(blVal).trim().toLowerCase());

      const cls = isMissing ? "miss" : (isMatch ? "match" : "diff");
      const mark = isMissing ? '<span class="vpill miss">⚠ missing</span>'
        : (isMatch ? '<span class="vpill ok">✓ match</span>'
        : '<span class="vpill bad">✕ differs</span>');

      const sv = (isMatch && !isMissing) ? null : sevOf(fKey);
      const sevBadge = sv ? '<span class="sev '+sv+'" title="'+esc(SEV_TITLE[sv])+'">'+sv.toUpperCase()+'</span>' : "";

      const pair = (isMatch || isMissing)
        ? [esc(fmtVal(fKey, siVal)), esc(fmtVal(fKey, blVal))]
        : diffCells(fmtVal(fKey, siVal), fmtVal(fKey, blVal));

      rows += '<tr class="cmp-row '+cls+'">'+
        '<td class="f">'+esc(fLabel)+'<small>'+esc(fKey)+'</small>'+sevBadge+'</td>'+
        '<td class="val si">'+pair[0]+'</td>'+
        '<td class="op">→</td>'+
        '<td class="val bl">'+pair[1]+'</td>'+
        '<td>'+mark+'</td>'+
      '</tr>';
    }
    cmpTableHtml =
      '<table class="cmp"><thead><tr>'+
        '<th>Field</th>'+
        '<th>Current Shipping Instruction (v'+si.version_number+')</th>'+
        '<th></th>'+
        '<th>Current Draft Bill of Lading (v'+bl.version_number+')</th>'+
        '<th>Verdict</th>'+
      '</tr></thead><tbody>'+rows+'</tbody></table>'+
      '<div class="note-foot" style="margin-top:10px;font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px">'+
        '<span>Differing tokens are highlighted: <span class="tk del">Current SI</span> vs <span class="tk ins">Current BL</span>. Severity (HIGH / MED / LOW) reflects customs risk.</span>'+
      '</div>';
  } else {
    cmpTableHtml =
      '<div class="note warn" style="margin:4px 0">'+
        '<b>Incomplete Document Set:</b> Both a current SI and a current BL are required for field comparison.'+
        ((ov && ov.missing_documents && ov.missing_documents.length)?'<br>Missing documents: <b>'+esc(ov.missing_documents.join(", "))+'</b>':'')+
      '</div>';
  }

  const currentComparison =
    '<div class="panel" id="sec-comparison"><div class="panel-head">'+
      '<div class="ttl">'+icon("compare")+'Current SI ↔ Current BL · Field Comparison</div>'+
      '<span class="count">'+((sh.mismatch_count > 0)? sh.mismatch_count+" discrepancies" : "Verified OK")+'</span>'+
    '</div><div class="panel-body">'+cmpTableHtml+'</div></div>';

  // ---- 3. Unified Review & Resolution Hub ----
  const reviewResolutionHub = renderReviewResolutionHub();

  // ---- 4. Document Version History & Version Diff ----
  const byType={SI:[],BL:[]};
  state.versions.forEach(v=>{ (byType[v.doc_type]||(byType[v.doc_type]=[])).push(v); });
  function verList(type){
    const rawArr=byType[type]||[];
    if(rawArr.length===0) return '<div class="muted small" style="padding:6px 2px">No '+type+' versions found.</div>';
    const uniqueMap = new Map();
    let dupCount = 0;
    for(const v of rawArr){
      if(v.duplicate_of_version_id != null){
        dupCount++;
        continue;
      }
      if(!uniqueMap.has(v.version_number)){
        uniqueMap.set(v.version_number, v);
      }
    }
    const arr = uniqueMap.size > 0 ? Array.from(uniqueMap.values()) : rawArr;
    let h="";
    for(const v of arr){
      const tags=[];
      if(v.is_latest) tags.push('<span class="tag-latest">LATEST DETECTED VERSION</span>');
      h += '<div class="ver"><span class="vn">v'+v.version_number+'</span>'+
        '<span class="fn">'+esc(v.filename)+'</span>'+
        '<span class="meta">'+(v.received_at?esc(String(v.received_at).slice(0,10)):"-")+'</span>'+
        '<span class="ix">'+tags.join(" ")+'</span></div>';
    }
    if(dupCount > 0){
      h += '<div class="muted small" style="padding:4px 6px;color:var(--muted-2)">('+dupCount+' duplicate transmission'+(dupCount>1?'s':'')+' hidden)</div>';
    }
    return h;
  }
  const versionHistory =
    '<div class="panel" id="sec-history"><div class="panel-head"><div class="ttl">'+icon("layers")+'Document Version History (V1 → V2 → V3)</div></div>'+
      '<div class="panel-body grid2">'+
        '<div><div class="wf-title" style="margin-bottom:8px">SI Lineage</div>'+verList("SI")+'</div>'+
        '<div><div class="wf-title" style="margin-bottom:8px">BL Lineage</div>'+verList("BL")+'</div>'+
      '</div></div>';

  let opts="";
  for(const type of ["SI","BL"]){
    const arr=byType[type]||[];
    if(arr.length===0) continue;
    opts += '<optgroup label="'+type+'">';
    for(const v of arr){
      opts += '<option value="'+v.id+'" data-type="'+esc(v.doc_type)+'">'+
        esc("v"+v.version_number+" · "+fmtFile(v.filename))+'</option>';
    }
    opts += '</optgroup>';
  }
  const siL = (byType.SI||[]).find(v=>v.is_latest) || (byType.SI||[])[0];
  const blL = (byType.BL||[]).find(v=>v.is_latest) || (byType.BL||[])[(byType.BL||[]).length-1];
  const defFrom = siL? siL.id : (state.versions[0]&&state.versions[0].id||"");
  const defTo = blL? blL.id : (state.versions[state.versions.length-1]&&state.versions[state.versions.length-1].id||"");
  const diffPanel =
    '<div class="panel" id="sec-diff"><div class="panel-head"><div class="ttl">'+icon("compare")+'Version Diff</div>'+
      '<span class="count">deterministic</span></div>'+
      '<div class="panel-body">'+
        '<div class="cmpbar">'+
          '<div class="cmpbar-top">'+
            '<span class="ct">Pick two versions</span>'+
            '<span class="chint"><b>7</b> tracked fields · from → to</span>'+
            '<button class="btn primary cmp-go" data-act="compare"'+actAttrs("compare")+'>'+icon("compare")+'Compare Versions</button>'+
          '</div>'+
          '<div class="cmprow">'+
            cmpTile("fromSel","From",defFrom,opts)+
            '<button class="cmp-swap" type="button" data-act="swap" aria-label="Swap the two versions" '+
              'data-tip="Swap sides" data-tip-desc="Exchange the from and to versions" data-tip-kbd="S" data-tip-pos="bottom">'+
              icon("swap")+'</button>'+
            cmpTile("toSel","To",defTo,opts)+
          '</div>'+
        '</div>'+
        '<div class="note">Field-by-field comparison of document evolution between two versions. This is deterministic: no AI interpretation is applied.</div>'+
        '<div style="height:10px"></div>'+
        '<div id="diffOut"></div>'+
      '</div></div>';

  // ---- 5. Source Email Context (Secondary / Collapsible) ----
  const sourceEmails = (ov && ov.source_emails) || [];
  let emailCards = "";
  if(sourceEmails.length === 0){
    emailCards = '<div class="muted small" style="padding:4px 0">No source emails recorded for this shipment.</div>';
  } else {
    for(const em of sourceEmails){
      const attLinks = (em.attachments||[]).map(a=>{
        const nm = a.split("/").pop();
        return '<a class="pill neu" href="/api/attachments/'+encodeURIComponent(a)+'" target="_blank" style="text-decoration:none;font-size:11.5px;padding:3px 8px">'+icon("doc")+' '+esc(nm)+'</a>';
      }).join(" ");
      emailCards +=
        '<div style="padding:10px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;background:var(--surface)">'+
          '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">'+
            '<div style="font-weight:600;font-size:13px">'+esc(em.subject||"(no subject)")+'</div>'+
            '<span class="badge" style="font-family:var(--mono);font-size:11px">'+esc(em.email_id)+'</span>'+
          '</div>'+
          '<div style="font-size:12px;color:var(--muted);margin:4px 0 6px">From: <b>'+esc(em.sender||"unknown")+'</b>'+(em.received_at?' · '+esc(em.received_at.slice(0,19).replace("T"," ")):'')+'</div>'+
          (attLinks ? '<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">'+attLinks+'</div>' : '')+
        '</div>';
    }
  }
  const sourceEmailSection =
    '<details class="panel" id="sec-emails" style="margin-top:14px">'+
      '<summary style="cursor:pointer;padding:12px 16px;font-weight:600;display:flex;align-items:center;gap:8px;user-select:none">'+
        icon("mail")+'Source Email Context ('+sourceEmails.length+' email'+(sourceEmails.length>1?'s':'')+')'+
        '<span class="muted small" style="margin-left:auto;font-weight:normal">Click to expand raw email messages &amp; attachments</span>'+
      '</summary>'+
      '<div class="panel-body" style="border-top:1px solid var(--border);padding:14px 16px">'+emailCards+'</div>'+
    '</details>';

  view.innerHTML = header + currentComparison + reviewResolutionHub + versionHistory + diffPanel + sourceEmailSection;
  setSubnav([
    ["sec-header",            "Header",              "Shipment Header"],
    ["sec-comparison",        "Current SI ↔ BL",     "Current SI ↔ Current BL Comparison"],
    ["sec-review-resolution", "Review & Resolution", "Unified Review & Resolution Hub"],
    ["sec-history",           "Version Lineage",     "Document Version History (V1 → V2 → V3)"],
    ["sec-diff",              "Version Diff",        "Two-Version Comparison Diff Tool"],
    ["sec-emails",            "Source Emails",       "Source Email Context"]
  ]);

  // defaults + auto compare
  const fs=document.getElementById("fromSel"), ts=document.getElementById("toSel");
  if(fs && defFrom) fs.value=defFrom;
  if(ts && defTo) ts.value=defTo;
  syncCompareBar();
  if(fs && ts && fs.value && ts.value) compareVersions();
}

function renderReviewResolutionHub(){
  const sh = state.current;
  const issues = state.issues || [];
  const isClean = (sh.mismatch_count === 0 && issues.length === 0);

  if(isClean){
    return '<div class="panel" id="sec-review-resolution" style="margin-top:14px;border:1px solid rgba(16,185,129,0.35);background:rgba(16,185,129,0.03)">'+
      '<div class="panel-head" style="border-bottom:1px solid rgba(16,185,129,0.2)">'+
        '<div class="ttl" style="color:var(--ok);font-weight:700">'+icon("check")+'Review &amp; Resolution · Verified Match</div>'+
        '<span class="count" style="color:var(--ok);background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.25)">0 Discrepancies</span>'+
      '</div>'+
      '<div class="panel-body" style="padding:16px">'+
        '<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px">'+
          '<div>'+
            '<div style="font-size:13.5px;font-weight:600;color:var(--text);margin-bottom:3px">'+
              'All 7 tracked fields match between Current SI and Current BL.'+
            '</div>'+
            '<div style="font-size:12px;color:var(--muted)">'+
              'Verification completed successfully. No discrepancy detected. Ready for operational clearance.'+
            '</div>'+
          '</div>'+
          '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
            '<button class="btn primary" style="background:#059669;border-color:#047857;padding:7px 18px;font-size:13px;font-weight:650" data-act="rev-pass" data-tip="Approve & Release" data-tip-desc="Mark shipment verified and release" data-tip-kbd="A" data-tip-pos="top">'+
              icon("check")+' Approve &amp; Release <span class="k" style="background:rgba(255,255,255,0.25);padding:1px 5px;border-radius:3px;margin-left:4px">A</span>'+
            '</button>'+
            '<button class="btn" data-act="rev-back" data-tip="Send back" data-tip-desc="Return for carrier document update" data-tip-kbd="R" data-tip-pos="top">'+
              '↩ Send Back <span class="k">R</span>'+
            '</button>'+
            '<input type="text" class="note-in" id="ship-rev-note" placeholder="Reviewer note (optional)" maxlength="240" autocomplete="off" style="min-width:180px">'+
          '</div>'+
        '</div>'+
      '</div>'+
    '</div>';
  }

  let issueCards = "";
  for(const it of issues){
    issueCards += issueCard(it);
  }

  let draftHtml = "";
  if(state.draft){
    const d = state.draft;
    draftHtml =
      '<div class="draft-card" style="margin-top:14px;padding:12px 16px;background:var(--surface-3);border:1px solid var(--accent);border-radius:8px">'+
        '<div class="dt" style="font-weight:700;color:var(--accent);display:flex;align-items:center;gap:6px;font-size:13px">'+
          icon("doc")+' Corrected BL Draft Generated'+
        '</div>'+
        '<div class="kv" style="margin:6px 0 3px"><span class="k" style="font-weight:600">Draft Path:</span> <span class="v" style="font-family:var(--mono);font-size:11.5px">'+esc(d.corrected_draft_path)+'</span></div>'+
        '<div class="kv" style="margin:3px 0"><span class="k" style="font-weight:600">Applied Resolutions:</span> <span class="v">'+esc((d.applied_resolutions||[]).join(", "))+'</span></div>'+
        '<div class="note" style="margin-top:6px;font-size:11px;color:var(--muted)">This is a generated working draft reflecting approved corrections. Original carrier documents remain unchanged.</div>'+
      '</div>';
  }

  const openCount = issues.filter(i => !i._res || i._res.status !== "APPROVED").length;
  const countBadge = openCount > 0
    ? '<span class="count" style="color:var(--bad);background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25)">'+openCount+' Open Action'+(openCount>1?'s':'')+'</span>'
    : '<span class="count" style="color:var(--ok);background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.25)">All Resolved</span>';

  return '<div class="panel" id="sec-review-resolution" style="margin-top:14px;border:1px solid var(--border-strong);background:var(--surface)">'+
    '<div class="panel-head">'+
      '<div class="ttl" style="font-weight:700;color:var(--text)">'+icon("alert")+'Review &amp; Resolution Hub</div>'+
      countBadge+
    '</div>'+
    '<div class="panel-body" style="padding:16px">'+
      '<div style="font-size:12.5px;color:var(--muted);margin-bottom:12px">'+
        'Field discrepancies detected between Current SI and Current BL. Click <b>Approve All &amp; Generate Corrected Draft</b> to adopt SI corrections in one click, or review individually.'+
      '</div>'+
      '<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:14px">'+issueCards+'</div>'+
      '<div class="act-bar" style="margin:0;padding:12px 14px;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">'+
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
          '<button class="btn primary" style="background:var(--accent);font-weight:650;padding:7px 18px;font-size:13px" data-act="quick-batch-resolve" data-tip="Approve all & generate draft" data-tip-desc="Batch approve SI suggestions and generate corrected BL draft" data-tip-kbd="D" data-tip-pos="top">'+
            '🚀 Approve All &amp; Generate Corrected Draft <span class="k" style="background:rgba(255,255,255,0.25);padding:1px 5px;border-radius:3px;margin-left:4px">D</span>'+
          '</button>'+
          '<button class="btn" data-act="rev-back" data-tip="Send back to carrier" data-tip-desc="Return for carrier document update" data-tip-kbd="R" data-tip-pos="top">'+
            '↩ Send Back to Carrier <span class="k">R</span>'+
          '</button>'+
        '</div>'+
        '<div style="flex:1;min-width:200px;display:flex;gap:6px">'+
          '<input type="text" class="note-in" id="ship-rev-note" placeholder="Reviewer note (optional)" maxlength="240" autocomplete="off" style="width:100%">'+
        '</div>'+
      '</div>'+
      draftHtml+
    '</div>'+
  '</div>';
}

function issueCard(it){
  const field=it.field_name;
  const label=FMAP[field]||field;
  const res=it._res;
  let sugg;
  if(res && res.suggested_value != null){
    sugg = '💡 Suggestion: Update BL <b>'+esc(label)+'</b> to <b>'+esc(fmtVal(field,res.suggested_value))+'</b>';
  }else if(it.si_value != null){
    sugg = '💡 Suggestion: Adopt SI value <b>'+esc(fmtVal(field,it.si_value))+'</b>';
  }else{
    sugg = 'No suggestion available.';
  }
  const isApproved = (res && res.status==="APPROVED") || (it.status==="APPROVED");
  const resStatus = isApproved ? statusPill("APPROVED") : (res ? statusPill(res.status) : statusPill(it.status));
  return '<div class="issue" style="border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--surface-2)">'+
    '<div class="issue-head" style="margin-bottom:8px"><span class="fld" style="font-weight:600">'+esc(label)+'</span>'+statusPill(it.status)+resStatus+'</div>'+
    '<div class="grid2x" style="margin-bottom:8px">'+
      '<div class="valbox"><div class="l">Current SI value</div><div class="v">'+esc(fmtVal(field,it.si_value))+'</div></div>'+
      '<div class="valbox"><div class="l">Current BL value</div><div class="v">'+esc(fmtVal(field,it.bl_value))+'</div></div>'+
    '</div>'+
    '<div class="explain" style="font-size:12px;margin-bottom:8px"><b>Difference:</b> '+esc(it.difference||"-")+'<br><b>Explanation:</b> '+esc(it.explanation||"-")+'</div>'+
    '<div class="suggest" style="font-size:12px;margin-bottom:8px;color:var(--accent)">'+sugg+'</div>'+
    '<div class="issue-actions" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">'+
      (isApproved ? '<span style="color:var(--ok);font-weight:600;font-size:12px">✓ Approved</span>' :
        '<button class="btn primary sm" data-act="approve" data-id="'+it.id+'"'+actAttrs("approve")+'>Approve</button>'+
        '<button class="btn sm" data-act="reject" data-id="'+it.id+'"'+actAttrs("reject")+'>Reject</button>'
      )+
      '<input class="btn review-note" type="text" placeholder="optional review note…" style="flex:1;min-width:140px;font-size:12px" />'+
    '</div>'+
  '</div>';
}

async function quickBatchResolveAndDraft(){
  const sh = state.current;
  if(!sh) return;
  const noteEl = document.getElementById("ship-rev-note");
  const note = (noteEl && noteEl.value.trim()) || null;
  const emails = (state.overview && state.overview.source_emails) || [];
  const emailId = emails[0] && emails[0].email_id;

  try{
    const res = await fetch("/shipments/" + sh.id + "/batch-resolve-draft", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        reviewed_by: "human",
        review_comment: note,
        email_id: emailId
      })
    });
    if(!res.ok){
      const err = await res.json().catch(()=>({}));
      throw new Error(err.detail || "Failed to batch resolve");
    }
    const d = await res.json();
    state.draft = d.draft;
    toast("Approved " + d.approved_count + " field(s) & generated corrected BL draft", "ok");
    await openShipment(sh.id);
  }catch(e){
    toast("Action failed: " + e.message, "err");
  }
}

async function compareVersions(){
  const fs=document.getElementById("fromSel"), ts=document.getElementById("toSel");
  const box=document.getElementById("diffOut");
  if(!fs||!ts||!box) return;
  const from=+fs.value, to=+ts.value;
  if(!from||!to){ box.innerHTML='<div class="muted small">Select two versions to compare.</div>'; return; }
  box.innerHTML='<div class="muted small">Comparing…</div>';
  try{
    const d = await api("/shipments/"+state.current.id+"/version-diff?from_version_id="+from+"&to_version_id="+to);
    state.diff=d.fields;
    let rows='<div class="diffrow head"><div>Field</div><div>'+esc(verLabel(findVer(from)))+'</div><div>'+esc(verLabel(findVer(to)))+'</div><div>Status</div></div>';
    for(const f of FIELDS){
      const cell = d.fields.find(x=>x.field===f[0]) || {field:f[0],from_value:null,to_value:null,changed:false};
      const ch = cell.changed;
      rows += '<div class="diffrow '+(ch?"changed":"unchanged")+'">'+
        '<div class="fld">'+esc(f[1])+'</div>'+
        '<div class="val">'+esc(fmtVal(f[0],cell.from_value))+'</div>'+
        '<div class="val to">'+esc(fmtVal(f[0],cell.to_value))+'</div>'+
        '<div><span class="sev '+(ch?"ch":"uc")+'">'+(ch?"CHANGED":"UNCHANGED")+'</span></div>'+
      '</div>';
    }
    box.innerHTML='<div style="border:1px solid var(--border);border-radius:10px;overflow:hidden">'+rows+'</div>';
  }catch(e){
    box.innerHTML='<div class="note warn">Version diff failed: '+esc(e.message)+'</div>';
  }
}

async function doAction(act, id, comment){
  try{
    const body = (act==="suggest") ? {} : {reviewed_by:"human", review_comment: comment||null};
    await api("/issues/"+id+"/"+act, {method:"POST", body:JSON.stringify(body)});
    toast("Issue #"+id+" · "+act+" recorded","ok");
    await openShipment(state.current.id);
  }catch(e){
    toast("Action failed: "+e.message,"err");
  }
}

async function generateDraft(){
  try{
    const d = await api("/shipments/"+state.current.id+"/generate-corrected-draft",{method:"POST",body:"{}"});
    state.draft=d;
    renderDetail(); // re-render in place so the draft card persists
    toast("Corrected draft generated","ok");
  }catch(e){
    toast("Draft generation failed: "+e.message,"err");
  }
}

/* ---------- event delegation ---------- */
view.addEventListener("click", e=>{
  const th=e.target.closest("th[data-sort]");
  if(th){
    const k=th.dataset.sort;
    if(state.dashSort.key===k) state.dashSort.dir*=-1; else state.dashSort={key:k,dir:1};
    state.dashSel=0;
    paintDashboard();
    return;
  }
  const b = e.target.closest("[data-act]");
  if(!b) return;
  const act = b.dataset.act, id = b.dataset.id;
  if(act==="fwork"){
    state.dashFilterWork = b.dataset.val;
    state.dashSel = 0;
    paintDashboard();
    return;
  }
  if(act==="quick-batch-resolve"){
    quickBatchResolveAndDraft();
    return;
  }
  if(act==="rev-confirm"||act==="rev-pass"||act==="rev-mismatch"||act==="rev-back"){
    sendShipmentReview(act);
    return;
  }
  if(act==="compare"){ compareVersions(); return; }
  if(act==="swap"){ swapCompare(); return; }
  if(act==="draft"){ generateDraft(); return; }
  if(act==="refresh-dash"){ renderDashboard(); const u=document.getElementById("dashUpdated"); if(u) u.textContent="Updated "+fmtTime(new Date()); return; }
  if(act==="fver"||act==="fres"){
    const grp=act==="fver"?"ver":"res";
    state.dashFilter[grp]=b.dataset.val;
    paintDashboard();
    return;
  }
  if(act==="suggest"||act==="approve"||act==="reject"){
    const card=b.closest(".issue");
    const note=card?card.querySelector(".review-note"):null;
    const comment=note?note.value.trim():"";
    doAction(act,id,comment);
  }
});
view.addEventListener("change", e=>{
  if(e.target.id==="fromSel"||e.target.id==="toSel"){ syncCompareBar(); compareVersions(); }
});

/* ---------- shell integration ----------
   The Review Console module owns the shared chrome: theme, sidebar rail, the
   help sheet, the Ctrl+K palette and the tooltip layer. It binds them at load,
   so binding the same elements here would run every handler twice and cancel
   the result out. This module only claims the keyboard, and only while its own
   view is on screen. */

function go(hash){
  if(location.hash===hash) route(); else location.hash=hash;
}
function goDashboard(){ go("#/"); }
function goShipments(){ go("#lifecycle"); }
function onLifecycleView(){
  const v=document.getElementById("viewLifecycle");
  return !!v && !v.hidden;
}
/* the shared palette and help sheet are driven by the Review Console module */
function sharedOverlayOpen(){
  const pal=document.getElementById("palette"), help=document.getElementById("help");
  return !!((pal && !pal.hidden) || (help && !help.hidden));
}

/* platform-aware modifier chip + tooltip key on the search button */
(function(){
  const el=document.getElementById("lcSearchBtn");
  if(!el) return;
  el.dataset.tipKbd=MODK+"|K";
  const chip=el.querySelector(".hk");
  if(chip) chip.textContent=IS_MAC?"\u2318K":"Ctrl K";
})();
document.getElementById("lcSearchBtn").addEventListener("click",function(){
  if(window.openPalette) window.openPalette();
});

/* ---------- global shortcuts ----------
   The page router already claims 0 / 1 / 2 for view switching, so this table
   only carries what is specific to the lifecycle console. */
document.addEventListener("keydown",e=>{
  if(sharedOverlayOpen()) return;
  if(!onLifecycleView()) return;
  if(isTyping(e.target)) return;
  const k=e.key;

  /* `]` and the full-width brackets are accepted as aliases so the rail still
     toggles when a CJK input mode rewrites the key. */
  if(k==="t"||k==="T"){ e.preventDefault(); if(window.toggleTheme) window.toggleTheme(); return; }
  if(k==="["||k==="]"||k==="\u3010"||k==="\u3011"||k==="\uff3b"||k==="\uff3d"){
    e.preventDefault(); if(window.toggleRail) window.toggleRail(); return;
  }
  if(k==="?"){ e.preventDefault(); if(window.openHelp) window.openHelp(); return; }
  if(k==="Escape"){ if(state.current){ e.preventDefault(); goDashboard(); } return; }
  if(k==="b"||k==="B"){ e.preventDefault(); switchView("review"); return; }
  if(k==="g"||k==="G"){ e.preventDefault(); goDashboard(); return; }

  /* ---- detail view: review actions + resolution actions + compare / swap ---- */
  if(state.current){
    /* Shift combos first. Shift+S reports e.key==="S", so the plain "S = swap"
       check must exclude shift or it would shadow "Shift+S = suggest". */
    if(e.shiftKey && (k==="s"||k==="S")){ e.preventDefault(); pressAct("suggest"); return; }
    if(!e.shiftKey && (k==="c"||k==="C") && document.getElementById("fromSel")){ e.preventDefault(); pressAct("compare"); return; }
    if(!e.shiftKey && (k==="s"||k==="S") && document.getElementById("fromSel")){ e.preventDefault(); swapCompare(); return; }
    if(k==="a"||k==="A"){ e.preventDefault(); ((state.issues&&state.issues.length===0) ? sendShipmentReview("rev-pass") : sendShipmentReview("rev-confirm")); return; }
    if(k==="o"||k==="O"){ e.preventDefault(); sendShipmentReview("rev-pass"); return; }
    if(k==="d"||k==="D"){ e.preventDefault(); ((state.issues&&state.issues.length>0) ? quickBatchResolveAndDraft() : generateDraft()); return; }
    if(k==="r"||k==="R"){ e.preventDefault(); sendShipmentReview("rev-back"); return; }
    if(k==="m"||k==="M"){ e.preventDefault(); sendShipmentReview("rev-mismatch"); return; }
    if(k==="x"||k==="X"){ e.preventDefault(); pressAct("reject"); return; }
    return;
  }

  /* ---- dashboard view: row navigation, filter chips + refresh ---- */
  {
    /* The arrows walk the row highlight and keep it in view; Enter opens it.
       This is the list counterpart of the inbox arrows, so both consoles
       behave the same way. */
    if(k==="ArrowDown"||k==="ArrowUp"){
      const rows=[...view.querySelectorAll("table.list tbody tr")];
      if(rows.length){
        e.preventDefault();
        let i=rows.findIndex(r=>r.classList.contains("sel"));
        if(i<0) i = k==="ArrowDown" ? -1 : 0;
        i = k==="ArrowDown" ? (i+1)%rows.length : (i-1+rows.length)%rows.length;
        state.dashSel=i;
        rows.forEach((r,n)=>r.classList.toggle("sel",n===i));
        keepRowVisible(rows[i]);
      }
      return;
    }
    if(k==="Enter"){
      const row=view.querySelector("table.list tbody tr.sel");
      if(row){ e.preventDefault(); state.dashSel=[...view.querySelectorAll("table.list tbody tr")].indexOf(row); location.hash="#/s/"+row.dataset.id; return; }
    }
    const fv={a:"all",v:"ok",n:"review"}, fr={c:"clear",u:"unresolved",p:"pending"};
    const lk=(k||"").toLowerCase();
    if(e.shiftKey){
      if(lk==="a"){ e.preventDefault(); setDashFilter("res","all"); return; }
      if(lk==="r"){ e.preventDefault(); setDashFilter("res","resolved"); return; }
      if(fr[lk]){ e.preventDefault(); setDashFilter("res",fr[lk]); return; }
      return;
    }
    if(fv[lk]){ e.preventDefault(); setDashFilter("ver",fv[lk]); return; }
    if(k==="r"||k==="R"){ e.preventDefault(); pressAct("refresh-dash"); return; }
  }
});
  // Expose Lifecycle API
  window.Lifecycle = {
    state,
    route,
    renderDashboard,
    openShipment,
    compareVersions,
    pressAct,
    setDashFilter,
    init: function(){
      route();
    }
  };

  // Auto-init on load if viewLifecycle is active
  if(typeof window !== "undefined"){
    const v = document.getElementById("viewLifecycle");
    const h = window.location.hash;
    if((v && !v.hidden) || h.startsWith("#lifecycle") || h.startsWith("#/s/") || h === "#/" || h === ""){
      route();
    }
  }
})();
