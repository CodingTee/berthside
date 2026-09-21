/* ==========================================================================
   ShipSync Enterprise Hub · Gateway & Quarantine Sandbox Module
   ========================================================================== */

(function(){
  let currentScenario = "clean_bl";
  let currentReturnStage = null;
  let statusPollTimer = null;
  let gatewayItems = [];        // last loaded staged list (mailbox-scoped)
  let gatewaySearch = "";        // active search query
  let gatewayIncludeQuar = false; // 'Include quarantine' toggle
  let gatewayMatrix = [];       // rendered source list  [{mb,role,mode,inherit,staged,custom}]
  let matrixSearch = "";        // active Source Trust Matrix search query
  const MX_SOURCES_KEY  = "sdoc-mx-sources";     // {mailbox: role} registry of UI-added sources
  const INB_COLLAPSE_KEY = "sdoc-inb-collapsed"; // "1" = Inbound Stream table collapsed

  const SRC_ROLES = {
    "sdoc-hackathon-bundle@averis.com": "Benchmark EDI / API bundle",
    "operations@shipsync.demo": "Direct mail influx (live ops)",
    "docs.export@averis.com": "Export documentation",
    "booking@averis.com": "Booking confirmations",
    "april.shipping@averis.com": "APRIL pulp & paper BU",
    "finance@averis.com": "Freight & finance desk",
    "transpacific@averis.com": "Transpacific trade lane",
  };

  function toast(msg, type="ok"){
    if(window.showToast) {
      window.showToast(msg, type);
      return;
    }
    const t = document.getElementById("toast");
    if(!t) return;
    t.textContent = msg;
    t.className = "toast show " + type;
    setTimeout(() => t.className = "toast", 3200);
  }

  function effectiveMode(mb){
    const sp = (window.hubPolicy && window.hubPolicy.source_policies) || {};
    if(mb && sp[mb]) return sp[mb];
    return (window.hubPolicy && window.hubPolicy.ingest_mode) || "auto";
  }

  function renderDistBar(stats){
    if(!stats) return;
    const segs = [
      ["distSegSafe", stats.safe_ingested],
      ["distSegPending", stats.pending_approval],
      ["distSegSpam", stats.quarantined_spam],
      ["distSegBlocked", stats.blocked_malware],
    ];
    const total = segs.reduce((a, s) => a + (Number(s[1]) || 0), 0);
    segs.forEach(([id, v]) => {
      const el = document.getElementById(id);
      if(!el) return;
      el.style.flexGrow = total > 0 ? String(Number(v) || 0) : "1";
    });
  }

  async function loadStatus(){
    try{
      const res = await fetch("/api/v1/gateway/status");
      const data = await res.json();
      
      const elTotal = document.getElementById("kpiTotal");
      const elSafe = document.getElementById("kpiSafe");
      const elBlocked = document.getElementById("kpiBlocked");
      const elSpam = document.getElementById("kpiSpam");
      const elPending = document.getElementById("kpiPending");

      if(elTotal) elTotal.textContent = data.statistics.total_processed;
      if(elSafe) elSafe.textContent = data.statistics.safe_ingested;
      if(elBlocked) elBlocked.textContent = data.statistics.blocked_malware;
      if(elSpam) elSpam.textContent = data.statistics.quarantined_spam;
      if(elPending) elPending.textContent = data.statistics.pending_approval;
      renderDistBar(data.statistics);

      const selEng = document.getElementById("selEngine");
      const selMd = document.getElementById("selMode");
      if(selEng) selEng.value = data.policy.engine;
      if(selMd) selMd.value = data.policy.ingest_mode;

      window.hubPolicy = {
        ingest_mode: data.policy.ingest_mode,
        source_policies: data.policy.source_policies || {},
      };
      renderMatrix(data.policy.source_policies || {}, data.source_staged || {}, data.available_mailboxes || []);

      const ob = document.getElementById("ollamaBadge");
      const ot = document.getElementById("ollamaText");
      if(ob && ot){
        if(data.policy.engine === "rule"){
          ob.className = "badge-live";
          ob.style.borderColor = "rgba(45,212,191,.4)";
          ob.style.color = "var(--accent)";
          ot.textContent = "Deterministic Rules Ready (Local Regex · <1ms)";
        } else if(data.policy.engine === "ollama"){
          ob.style.borderColor = "";
          ob.style.color = "";
          if(data.engines.ollama.online && data.engines.ollama.model_ready){
            ob.className = "badge-live";
            ot.textContent = `Local Ollama Ready (${data.engines.ollama.current_model})`;
          } else {
            ob.className = "badge-live offline";
            ot.textContent = "Local Ollama Offline (Auto Cloud Cascade)";
          }
        } else {
          ob.className = "badge-live";
          ob.style.borderColor = "rgba(56,189,248,.4)";
          ob.style.color = "#38bdf8";
          ot.textContent = `Cloud Cascade Active (${data.engines.cloud.primary})`;
        }
      }
    }catch(e){
      console.warn("Could not load gateway status", e);
    }
  }

  async function changePolicy(){
    const selEng = document.getElementById("selEngine");
    const selMd = document.getElementById("selMode");
    if(!selEng || !selMd) return;
    const engine = selEng.value;
    const ingest_mode = selMd.value;
    try{
      const res = await fetch("/api/v1/gateway/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({engine, ingest_mode})
      });
      if(res.ok){
        toast("Hub policy updated successfully", "ok");
        loadStatus();
      }
    }catch(e){
      toast("Failed to update gateway policy", "bad");
    }
  }

  async function loadStagedEmails(){
    const selMb = document.getElementById("selMailbox");
    const mb = selMb ? selMb.value : "ALL";
    const url = "/api/v1/gateway/emails?limit=1000" + (mb !== "ALL" ? `&source_mailbox=${encodeURIComponent(mb)}` : "");
    try{
      const res = await fetch(url);
      const data = await res.json();
      gatewayItems = Array.isArray(data) ? data : [];
      applyGatewayFilter();
    }catch(e){
      console.error(e);
    }
  }

  const stagedById = new Map();   // stage_id -> item, for the row-detail modal
  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }
  function q(s){ return String(s).replace(/'/g, "\\'"); }

  // Full action set: used inside the row-detail modal.
  function stageActionsHtml(it){
    const stop = "event.stopPropagation()";
    if(it.status === "STAGED"){
      return `
        <button class="btn btn-sm btn-primary" onclick="${stop};Gateway.approveEmail('${q(it.stage_id)}')">✅ Approve &amp; Ingest</button>
        <button class="btn btn-sm btn-danger" onclick="${stop};Gateway.openReturn('${q(it.stage_id)}', true)">📤 Reject &amp; Clarify</button>
      `;
    }
    if(it.status === "QUARANTINED"){
      if(it.security_status === "BLOCKED"){
        return `<span style="color:var(--bad);font-weight:600;font-size:12px;">🔒 Malware Blocked</span>`;
      }
      return `
        <button class="btn btn-sm" style="border-color:var(--warn);color:var(--warn);background:rgba(251,191,36,.08);" onclick="${stop};Gateway.approveEmail('${q(it.stage_id)}')">🔓 Release &amp; Ingest</button>
        <button class="btn btn-sm" onclick="${stop};Gateway.openReturn('${q(it.stage_id)}', true)">↩️ Return to Sender</button>
      `;
    }
    if(it.status === "APPROVED" || it.status === "AUTO_INGESTED"){
      return `
        <span style="color:var(--ok);font-weight:600;font-size:12px;">✓ Ingested</span>
        <button class="btn btn-sm" onclick="${stop};Gateway.openReturn('${q(it.stage_id)}', false)">↩️ Return to Sender</button>
      `;
    }
    if(it.status === "RETURNED"){
      return `
        <span style="color:var(--accent);font-weight:600;font-size:12px;">↩️ Returned via ${esc(it.source_mailbox)}</span>
        <button class="btn btn-sm" onclick="${stop};Gateway.openReturn('${q(it.stage_id)}', false)">Return Again</button>
      `;
    }
    return `
      <span style="color:var(--muted);font-size:12px;">Rejected</span>
      <button class="btn btn-sm" onclick="${stop};Gateway.openReturn('${q(it.stage_id)}', false)">↩️ Return to Sender</button>
    `;
  }

  // One compact primary action per row; everything else lives in the modal.
  function stagePrimaryHtml(it){
    if(it.status === "STAGED"){
      return `<button class="btn btn-sm btn-primary" onclick="event.stopPropagation();Gateway.approveEmail('${q(it.stage_id)}')">✅ Approve</button>`;
    }
    if(it.status === "QUARANTINED"){
      if(it.security_status === "BLOCKED"){
        return `<span style="color:var(--bad);font-weight:600;font-size:12px;">🔒 Blocked</span>`;
      }
      return `<button class="btn btn-sm" style="border-color:var(--warn);color:var(--warn);background:rgba(251,191,36,.08);" onclick="event.stopPropagation();Gateway.approveEmail('${q(it.stage_id)}')">🔓 Release</button>`;
    }
    if(it.status === "APPROVED" || it.status === "AUTO_INGESTED"){
      return `<span style="color:var(--ok);font-weight:600;font-size:12px;margin-right:6px;">✓</span><button class="btn btn-sm" onclick="event.stopPropagation();Gateway.openReturn('${q(it.stage_id)}', false)">↩ Return</button>`;
    }
    if(it.status === "RETURNED"){
      return `<button class="btn btn-sm" onclick="event.stopPropagation();Gateway.openReturn('${q(it.stage_id)}', false)">↩ Return Again</button>`;
    }
    return `<button class="btn btn-sm" onclick="event.stopPropagation();Gateway.openReturn('${q(it.stage_id)}', false)">↩ Return</button>`;
  }

  function renderStagedList(items){
    const tbody = document.getElementById("stagedList");
    const badge = document.getElementById("stagedCountBadge");
    if(badge) badge.textContent = `(${items ? items.length : 0} records)`;
    if(!tbody) return;
    stagedById.clear();
    (items || []).forEach(it => stagedById.set(it.stage_id, it));

    if(!items || items.length === 0){
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:30px;">No staged records found for this mailbox channel</td></tr>`;
      return;
    }
    const statusTone = {
      STAGED: "st-warn", QUARANTINED: "st-warn", APPROVED: "st-ok",
      AUTO_INGESTED: "st-ok", RETURNED: "st-accent", REJECTED: "st-bad"
    };
    let html = "";
    items.forEach(it => {
      const secShort = it.security_status === "BLOCKED"
        ? `<span class="pill pill-blocked">⛔ Blocked</span>`
        : `<span class="pill pill-clean">🛡️ Clean</span>`;
      const catPill = it.category === "SPAM"
        ? `<span class="pill pill-warn">SPAM</span>`
        : `<span class="pill" style="background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.3);">${esc(it.category)}</span>`;
      const statusChip = `<span class="stg-status ${statusTone[it.status] || ""}">${esc(it.status)}</span>`;
      const mode = effectiveMode(it.source_mailbox);

      html += `
        <tr data-id="${it.stage_id}" class="stg-row" title="Click for full detail" onclick="Gateway.openStageDetail('${q(it.stage_id)}')">
          <td style="font-family:var(--mono);font-size:11.5px;color:var(--muted);">${esc(it.stage_id)}</td>
          <td>
            <div class="stg-subj" title="${esc(it.subject)}">${esc(it.subject)}</div>
            <div class="stg-meta">
              <span class="stg-sender" title="${esc(it.sender || "")}">${esc(it.sender || "-")}</span>
              <span class="pill pill-src" title="Target mailbox">${esc(it.source_mailbox || "-")}</span>
              <span class="eff-tag ${mode === "auto" ? "auto" : "manual"}">${mode === "auto" ? "AUTO" : "MANUAL"}</span>
            </div>
          </td>
          <td><span class="stg-verdict">${secShort}${catPill}${statusChip}</span></td>
          <td onclick="event.stopPropagation()">${stagePrimaryHtml(it)}</td>
        </tr>
      `;
    });
    tbody.innerHTML = html;
  }

  function openStageDetail(stageId){
    const it = stagedById.get(stageId);
    if(!it) return;
    const set = (id, v) => { const el = document.getElementById(id); if(el) el.innerHTML = v; };
    set("sdSubject", esc(it.subject));
    set("sdStage", esc(it.stage_id));
    set("sdSender", esc(it.sender || "-"));
    const mode = effectiveMode(it.source_mailbox);
    set("sdMailbox",
      `<span class="pill pill-src">${esc(it.source_mailbox || "-")}</span> ` +
      `<span class="eff-tag ${mode === "auto" ? "auto" : "manual"}">${mode === "auto" ? "AUTO" : "MANUAL"}</span>`);
    set("sdSecurity", it.security_status === "BLOCKED"
      ? `<span class="pill pill-blocked">⛔ Malware Intercepted</span>`
      : `<span class="pill pill-clean">🛡️ Verified Clean</span>`);
    set("sdAttachments", it.attachments && it.attachments.length
      ? "📎 " + it.attachments.map(esc).join("<br>📎 ")
      : `<span style="color:var(--muted);">none</span>`);
    set("sdCategory", it.category === "SPAM"
      ? `<span class="pill pill-warn">SPAM / Phishing</span>`
      : `<span class="pill" style="background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.3);">${esc(it.category)}</span>`);
    set("sdStatus", `<span style="font-family:var(--mono);font-size:12px;">${esc(it.status)}</span>`);
    set("sdActions", stageActionsHtml(it));
    const m = document.getElementById("stageDetailModal");
    if(m) m.classList.remove("hidden");
  }

  function closeStageDetail(){
    const m = document.getElementById("stageDetailModal");
    if(m) m.classList.add("hidden");
  }

  async function _approveOne(stageId){
    try{ const res = await fetch(`/api/v1/gateway/emails/${stageId}/approve`, {method: "POST"}); return res.ok; }
    catch(e){ return false; }
  }
  async function approveEmail(stageId){
    try{
      const res = await fetch(`/api/v1/gateway/emails/${stageId}/approve`, {method: "POST"});
      if(res.ok){
        toast(`Email ${stageId} approved and synced to Review Console!`, "ok");
        loadStatus();
        loadStagedEmails();
      }
    }catch(e){
      toast("Approval failed", "bad");
    }
  }

  async function openReturn(stageId, rejectFirst){
    currentReturnStage = stageId;
    let draft;
    try{
      if(rejectFirst){
        const r = await fetch(`/api/v1/gateway/emails/${stageId}/reject`, {method: "POST"});
        draft = await r.json();
      } else {
        const r = await fetch(`/api/v1/gateway/emails/${stageId}/return`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({dry_run: true})
        });
        draft = await r.json();
      }
      document.getElementById("retVia").textContent = draft.reply_via || "-";
      document.getElementById("retTo").textContent = draft.reply_to || "-";
      document.getElementById("retSub").value = draft.subject || draft.draft_subject || "";
      document.getElementById("retBody").value = draft.body || draft.draft_body || "";
      const delivery = document.getElementById("retDelivery");
      if(delivery){
        if(draft.decision === "REJECTED"){
          delivery.textContent = "(reply: rejection / clarification)";
        } else {
          delivery.textContent = "(reply: verification result)";
        }
      }
      updateMailtoLink();
      document.getElementById("returnModal").classList.remove("hidden");
    }catch(e){
      toast("Failed to prepare return", "bad");
    }
  }

  function updateMailtoLink(){
    const btn = document.getElementById("retMailtoBtn");
    if(!btn) return;
    const to = (document.getElementById("retTo").textContent || "").trim();
    const sub = document.getElementById("retSub").value || "";
    const body = document.getElementById("retBody").value || "";
    if(to && to !== "-"){
      btn.href = `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(sub)}&body=${encodeURIComponent(body)}`;
    } else {
      btn.href = "#";
    }
  }

  function closeReturnModal(){
    const m = document.getElementById("returnModal");
    if(m) m.classList.add("hidden");
    loadStatus();
    loadStagedEmails();
  }

  async function confirmReturn(){
    if(!currentReturnStage) return;
    const subject = document.getElementById("retSub").value;
    const body = document.getElementById("retBody").value;
    try{
      const res = await fetch(`/api/v1/gateway/emails/${currentReturnStage}/return`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({subject, body})
      });
      const d = await res.json();
      toast(`Returned via ${d.reply_via} (${d.delivery})`, "ok");
      closeReturnModal();
    }catch(e){
      toast("Failed to dispatch return", "bad");
    }
  }

  function openSimModal(){
    const mx = document.getElementById("mxModal");
    if(mx) mx.classList.add("hidden");
    const m = document.getElementById("simModal");
    if(m) m.classList.remove("hidden");
  }
  function closeSimModal(){
    const m = document.getElementById("simModal");
    if(m) m.classList.add("hidden");
  }

  function selectScenario(sc, el){
    currentScenario = sc;
    document.querySelectorAll(".sc-card").forEach(c => c.classList.remove("selected"));
    if(el) el.classList.add("selected");
  }

  async function executeDrop(){
    closeSimModal();
    toast("Simulating incoming email transmission & running security scan...", "ok");
    try{
      const selMb = document.getElementById("selMailbox");
      const mailbox = (selMb && selMb.value !== "ALL") ? selMb.value : "docs.export@averis.com";
      const senderInput = document.getElementById("simSender");
      const sender = (senderInput && senderInput.value.trim()) ? senderInput.value.trim() : "customer@fastlogistics.com";
      const res = await fetch("/api/v1/gateway/simulate-drop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({scenario: currentScenario, source_mailbox: mailbox, sender: sender})
      });
      const d = await res.json();
      if(d.security_status === "BLOCKED"){
        toast(`⚠️ Intercepted malicious Trojan! Dispatched to quarantine.`, "bad");
      } else {
        toast(`✓ Security verified! Status: ${d.status}`, "ok");
      }
      loadStatus();
      loadStagedEmails();
    }catch(e){
      toast("Simulation drop failed", "bad");
    }
  }

  /* ---- Source Trust Matrix · one row per source ---------------------------- */
  function mxEsc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
  function mxExtra(){
    try{
      const o = JSON.parse(localStorage.getItem(MX_SOURCES_KEY) || "{}");
      return (o && typeof o === "object" && !Array.isArray(o)) ? o : {};
    }catch(e){ return {}; }
  }
  function mxSaveExtra(o){ try{ localStorage.setItem(MX_SOURCES_KEY, JSON.stringify(o)); }catch(e){} }
  function mxRoleOf(mb){ return SRC_ROLES[mb] || mxExtra()[mb] || "Mailbox source"; }

  function applyMatrixCollapse(){
    return;   // the matrix lives in a modal now; nothing to collapse inline
  }
  function toggleMatrix(){
    openMatrixModal();
  }
  function openMatrixModal(){
    const m = document.getElementById("mxModal");
    if(m) m.classList.remove("hidden");
    loadStatus();   // refresh matrix rows and the kebab summary
  }
  function closeMatrixModal(){
    const m = document.getElementById("mxModal");
    if(m) m.classList.add("hidden");
  }

  /* ---- Inbound Stream panel · the same collapse affordance as the matrix --- */
  function applyInboundCollapse(){
    const body = document.getElementById("inbBody");
    if(!body) return;
    const panel = document.getElementById("inbPanel");
    const btn = document.getElementById("inbToggle");
    const collapsed = localStorage.getItem(INB_COLLAPSE_KEY) === "1";
    body.hidden = collapsed;
    if(panel) panel.classList.toggle("collapsed", collapsed);
    if(btn){
      btn.textContent = collapsed ? "\u25B8" : "\u25BE";
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btn.title = collapsed ? "Expand the inbound stream" : "Collapse the inbound stream";
    }
  }
  function setInboundCollapsed(collapsed){
    try{ localStorage.setItem(INB_COLLAPSE_KEY, collapsed ? "1" : "0"); }catch(e){}
    applyInboundCollapse();
  }
  function toggleInbound(){
    const body = document.getElementById("inbBody");
    if(!body) return;
    setInboundCollapsed(!body.hidden);
  }
  function expandInbound(){
    const body = document.getElementById("inbBody");
    if(body && body.hidden) setInboundCollapsed(false);
  }

  function renderMatrix(policies, counts, mailboxes){
    const tbody = document.getElementById("matrixGrid");
    if(!tbody) return;
    const extra = mxExtra();
    const list = [];
    (mailboxes || []).forEach(mb => { if(mb && list.indexOf(mb) < 0) list.push(mb); });
    Object.keys(extra).forEach(mb => { if(mb && list.indexOf(mb) < 0) list.push(mb); });
    gatewayMatrix = list.map(mb => {
      const forced = policies ? policies[mb] : null;
      return {
        mb: mb,
        role: mxRoleOf(mb),
        mode: forced || effectiveMode(mb),
        inherit: !forced,
        staged: (counts && counts[mb]) || 0,
        custom: !SRC_ROLES[mb]
      };
    });
    applyMatrixCollapse();
    syncMailboxFilter(list);
    renderMatrixRows();
  }

  function syncMailboxFilter(list){
    const sel = document.getElementById("selMailbox");
    if(!sel) return;
    const have = {};
    for(let i = 0; i < sel.options.length; i++) have[sel.options[i].value] = true;
    list.forEach(mb => {
      if(have[mb]) return;
      const o = document.createElement("option");
      o.value = mb;
      o.textContent = mb + " (" + mxRoleOf(mb) + ")";
      sel.appendChild(o);
      have[mb] = true;
    });
  }

  function renderMatrixRows(){
    const tbody = document.getElementById("matrixGrid");
    if(!tbody) return;
    const q = matrixSearch.trim().toLowerCase();
    const rows = q
      ? gatewayMatrix.filter(it => it.mb.toLowerCase().indexOf(q) >= 0 || it.role.toLowerCase().indexOf(q) >= 0)
      : gatewayMatrix;
    if(!gatewayMatrix.length){
      tbody.innerHTML = '<tr><td class="mx-none" colspan="5">No mailbox sources registered yet.</td></tr>';
    } else if(!rows.length){
      tbody.innerHTML = '<tr><td class="mx-none" colspan="5">No source matches \u201C' + mxEsc(matrixSearch.trim()) + '\u201D.</td></tr>';
    } else {
      tbody.innerHTML = rows.map(mxRowHtml).join("");
    }
    const clr = document.getElementById("mxSearchClear");
    if(clr) clr.hidden = !matrixSearch.trim();
    updateMatrixSummary(rows.length);
  }

  function mxRowHtml(it){
    const attr = mxEsc(it.mb);
    const js = it.mb.replace(/'/g, "\\'");
    const mode = it.mode;
    return '<tr data-mb="' + attr + '">'
      + '<td><div class="mx-mb" title="' + attr + '">' + mxEsc(it.mb) + '</div>'
      + '<div class="mx-role" title="' + mxEsc(it.role) + '">' + mxEsc(it.role) + '</div></td>'
      + '<td><div class="seg" data-mb="' + attr + '">'
      + '<button class="' + (it.inherit ? 'active inherit' : 'inherit') + '" onclick="Gateway.setSourcePolicy(\'' + js + '\', \'inherit\')">Inherit</button>'
      + '<button class="' + (!it.inherit && mode === 'auto' ? 'active auto' : 'auto') + '" onclick="Gateway.setSourcePolicy(\'' + js + '\', \'auto\')">Auto</button>'
      + '<button class="' + (!it.inherit && mode === 'manual' ? 'active manual' : 'manual') + '" onclick="Gateway.setSourcePolicy(\'' + js + '\', \'manual\')">Manual</button>'
      + '</div></td>'
      + '<td><span class="mx-eff"><b style="color:' + (mode === 'auto' ? 'var(--ok)' : 'var(--warn)') + '">' + mode.toUpperCase() + '</b>'
      + (it.inherit ? '<span class="mx-pill inherit">Inherits global</span>' : '<span class="mx-pill forced">Overridden</span>')
      + '</span></td>'
      + '<td><span class="mx-cnt' + (it.staged ? '' : ' zero') + '">' + it.staged + '</span></td>'
      + '<td><div class="mx-actions">'
      + '<button class="btn btn-sm btn-primary" onclick="Gateway.bulkApproveMailbox(\'' + js + '\')">Approve</button>'
      + '<button class="btn btn-sm" onclick="Gateway.bulkReturnMailbox(\'' + js + '\')">Return</button>'
      + (it.custom ? '<button class="btn btn-sm btn-danger" title="Remove this source from the matrix" aria-label="Remove source" onclick="Gateway.removeSource(\'' + js + '\')">\u2715</button>' : '')
      + '</div></td></tr>';
  }

  function updateMatrixSummary(shown){
    const total = gatewayMatrix.length;
    const auto = gatewayMatrix.filter(it => it.mode === "auto").length;
    const manual = total - auto;
    const over = gatewayMatrix.filter(it => !it.inherit).length;
    const waiting = gatewayMatrix.reduce((a, it) => a + it.staged, 0);
    const q = matrixSearch.trim();
    let s = (q && typeof shown === "number" && shown !== total)
      ? shown + " / " + total + " sources"
      : total + (total === 1 ? " source" : " sources");
    s += " \u00B7 " + over + " overridden";
    if(waiting) s += " \u00B7 " + waiting + " awaiting triage";
    const el = document.getElementById("mxSummary");
    if(el) el.textContent = s;
    const kebab = document.getElementById("mxKebabSum");
    if(kebab) kebab.textContent = total + " src \u00B7 " + auto + " auto / " + manual + " manual";
  }

  function onMatrixSearch(v){
    matrixSearch = v || "";
    renderMatrixRows();
  }
  function clearMatrixSearch(){
    matrixSearch = "";
    const el = document.getElementById("mxSearch");
    if(el){ el.value = ""; el.focus(); }
    renderMatrixRows();
  }

  function openAddSource(){
    const box = document.getElementById("mxAdd");
    if(!box) return;
    box.hidden = false;
    const err = document.getElementById("mxAddErr"); if(err) err.textContent = "";
    const mb = document.getElementById("mxAddMb"); if(mb){ mb.value = ""; mb.focus(); }
    const role = document.getElementById("mxAddRole"); if(role) role.value = "";
  }
  function closeAddSource(){
    const box = document.getElementById("mxAdd");
    if(box) box.hidden = true;
    const err = document.getElementById("mxAddErr"); if(err) err.textContent = "";
  }
  function toggleAddSource(){
    const box = document.getElementById("mxAdd");
    if(!box) return;
    if(box.hidden) openAddSource(); else closeAddSource();
  }

  async function addSource(){
    const mbEl = document.getElementById("mxAddMb");
    const roleEl = document.getElementById("mxAddRole");
    const errEl = document.getElementById("mxAddErr");
    const fail = m => { if(errEl) errEl.textContent = m; if(mbEl && mbEl.focus) mbEl.focus(); };
    const mb = ((mbEl && mbEl.value) || "").trim().toLowerCase();
    const role = ((roleEl && roleEl.value) || "").trim();
    if(!mb) return fail("Enter the source mailbox address.");
    if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(mb)) return fail("That does not look like a valid email address.");
    if(gatewayMatrix.some(it => it.mb.toLowerCase() === mb)) return fail("This source is already in the matrix.");
    const extra = mxExtra();
    extra[mb] = role;
    mxSaveExtra(extra);
    const sp = Object.assign({}, (window.hubPolicy && window.hubPolicy.source_policies) || {});
    sp[mb] = "manual";   // a brand-new source is held for operator triage by default
    try{
      await fetch("/api/v1/gateway/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({source_policies: sp})
      });
    }catch(e){}
    closeAddSource();
    toast("Source " + mb + " added \u00B7 held for manual triage", "ok");
    loadStatus();
  }

  function removeSource(mb){
    const extra = mxExtra();
    delete extra[mb];
    mxSaveExtra(extra);
    const sp = Object.assign({}, (window.hubPolicy && window.hubPolicy.source_policies) || {});
    delete sp[mb];
    const sel = document.getElementById("selMailbox");
    if(sel){
      for(let i = 0; i < sel.options.length; i++){
        if(sel.options[i].value === mb){ sel.options[i].remove(); break; }
      }
      if(sel.value === mb) sel.value = "ALL";
    }
    gatewayMatrix = gatewayMatrix.filter(it => it.mb !== mb);
    renderMatrixRows();
    toast("Source " + mb + " removed from the matrix", "ok");
    fetch("/api/v1/gateway/config", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source_policies: sp})
    }).then(() => { loadStatus(); }).catch(() => { loadStatus(); });
  }

  async function setSourcePolicy(mb, mode){
    const sp = Object.assign({}, (window.hubPolicy && window.hubPolicy.source_policies) || {});
    if(mode === "inherit"){
      delete sp[mb];
    } else {
      sp[mb] = mode;
    }
    try{
      const res = await fetch("/api/v1/gateway/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({source_policies: sp})
      });
      if(res.ok){
        const d = await res.json();
        window.hubPolicy = {ingest_mode: d.ingest_mode, source_policies: d.source_policies || {}};
        toast(`Source policy for ${mb} set to ${mode}`, "ok");
        loadStatus();
        loadStagedEmails();
      }
    }catch(e){
      toast("Failed to update source policy", "bad");
    }
  }

  async function reapplyPolicy(){
    toast("Re-applying source trust policy to backlog...", "ok");
    try{
      const res = await fetch("/api/v1/gateway/policy/reapply", {method: "POST"});
      if(res.ok){
        const d = await res.json();
        toast(`Policy re-applied: ${d.auto_ingested} email(s) auto-ingested`, "ok");
        loadStatus();
        loadStagedEmails();
      }
    }catch(e){
      toast("Failed to re-apply policy", "bad");
    }
  }

  function _currentFilter(){
    const selMb = document.getElementById("selMailbox");
    return selMb ? selMb.value : "ALL";
  }

  function _bulkLabel(){
    const mb = _currentFilter();
    const src = mb === "ALL" ? "all sources" : mb;
    const q = gatewaySearch.trim();
    const scope = q ? ' · "' + q + '"' : ' · ' + src;
    const a = document.getElementById("bulkScopeLabel"); if(a) a.textContent = scope;
    const r = document.getElementById("retScopeLabel"); if(r) r.textContent = scope;
  }

  async function bulkApprove(){
    _bulkLabel();
    const mb = _currentFilter();
    const q = gatewaySearch.trim().toLowerCase();
    if(q){
      const ids = getVisibleStageIds();
      if(!ids.length){ toast("No matching rows to approve", "bad"); return; }
      let ok=0, skip=0;
      for(const id of ids){
        const it = gatewayItems.find(x => x.stage_id === id);
        if(it && (it.status === "STAGED" || (it.status === "QUARANTINED" && it.security_status !== "BLOCKED"))){
          if(await _approveOne(id)) ok++; else skip++;
        } else skip++;
      }
      toast(`Approved ${ok} matching (skipped ${skip} not actionable)`, "ok");
      loadStatus(); loadStagedEmails();
      return;
    }
    const include_quarantined = gatewayIncludeQuar;
    try{
      const res = await fetch("/api/v1/gateway/emails/bulk-approve", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({source_mailbox: mb, include_quarantined})
      });
      if(res.ok){
        const d = await res.json();
        toast(`Bulk approved ${d.approved} (skipped ${d.skipped_blocked} blocked)`, "ok");
        loadStatus();
        loadStagedEmails();
      }
    }catch(e){
      toast("Bulk approve failed", "bad");
    }
  }

  async function bulkReturn(){
    _bulkLabel();
    const mb = _currentFilter();
    const q = gatewaySearch.trim().toLowerCase();
    if(q){
      const ids = getVisibleStageIds();
      if(!ids.length){ toast("No matching rows to return", "bad"); return; }
      let ok=0;
      for(const id of ids){ if(await _returnOne(id)) ok++; }
      toast(`Returned ${ok} matching via origin mailbox`, "ok");
      loadStatus(); loadStagedEmails();
      return;
    }
    try{
      const res = await fetch("/api/v1/gateway/emails/bulk-return", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({source_mailbox: mb})
      });
      if(res.ok){
        const d = await res.json();
        toast(`Bulk returned ${d.returned} email(s) via origin mailbox`, "ok");
        loadStatus();
        loadStagedEmails();
      }
    }catch(e){
      toast("Bulk return failed", "bad");
    }
  }

  async function bulkApproveMailbox(mb){
    try{
      const res = await fetch("/api/v1/gateway/emails/bulk-approve", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({source_mailbox: mb})
      });
      if(res.ok){
        const d = await res.json();
        toast(`Approved ${d.approved} from ${mb} (skipped ${d.skipped_blocked} blocked)`, "ok");
        loadStatus();
        loadStagedEmails();
      }
    }catch(e){
      toast("Bulk approve failed", "bad");
    }
  }

  async function bulkReturnMailbox(mb){
    try{
      const res = await fetch("/api/v1/gateway/emails/bulk-return", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({source_mailbox: mb})
      });
      if(res.ok){
        const d = await res.json();
        toast(`Returned ${d.returned} from ${mb}`, "ok");
        loadStatus();
        loadStagedEmails();
      }
    }catch(e){
      toast("Bulk return failed", "bad");
    }
  }

  function getVisibleStageIds(){
    const ids = [];
    document.querySelectorAll("#stagedList tr[data-id]").forEach(tr => ids.push(tr.getAttribute("data-id")));
    return ids;
  }
  async function _returnOne(stageId){
    try{ const res = await fetch(`/api/v1/gateway/emails/${stageId}/return`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({dry_run:false})}); return res.ok; }
    catch(e){ return false; }
  }
  function onSearch(v){
    gatewaySearch = v || "";
    const clr = document.getElementById("gwSearchClear");
    if(clr) clr.hidden = !gatewaySearch.trim();
    if(gatewaySearch.trim()) expandInbound();   // typing while collapsed reveals the rows
    applyGatewayFilter();
  }
  function clearSearch(){
    gatewaySearch = "";
    const inp = document.getElementById("gwSearch");
    if(inp) inp.value = "";
    const clr = document.getElementById("gwSearchClear");
    if(clr) clr.hidden = true;
    applyGatewayFilter();
  }
  function onIncQuar(checked){ gatewayIncludeQuar = !!checked; }
  function applyGatewayFilter(){
    const q = gatewaySearch.trim().toLowerCase();
    let list = gatewayItems;
    if(q){
      list = list.filter(it =>
        (it.stage_id||"").toLowerCase().includes(q) ||
        (it.sender||"").toLowerCase().includes(q) ||
        (it.subject||"").toLowerCase().includes(q) ||
        (it.source_mailbox||"").toLowerCase().includes(q) ||
        (it.status||"").toLowerCase().includes(q) ||
        (it.category||"").toLowerCase().includes(q)
      );
    }
    renderStagedList(list);
    _bulkLabel();
  }
  function startPolling(){
    if(!statusPollTimer){
      statusPollTimer = setInterval(() => {
        loadStatus();
        loadStagedEmails();
        loadChannelStatus();
      }, 5000);
    }
  }

  function stopPolling(){
    if(statusPollTimer){
      clearInterval(statusPollTimer);
      statusPollTimer = null;
    }
  }

  async function loadChannelStatus(){
    try{
      const res = await fetch("/api/v1/gateway/email-channel/status");
      if(!res.ok) return;
      const d = await res.json();
      const imapBadge = document.getElementById("imapChannelBadge");
      if(imapBadge){
        if(d.imap.configured){
          imapBadge.innerHTML = `<span class="badge-dot" style="background:#10b981;"></span> <b style="color:#10b981;">LIVE</b> (${d.imap.host})`;
        } else {
          imapBadge.innerHTML = `<span class="badge-dot" style="background:var(--muted-2);"></span> Simulated Sandbox`;
        }
      }
      const smtpBadge = document.getElementById("smtpChannelBadge");
      if(smtpBadge){
        if(d.smtp.configured){
          smtpBadge.innerHTML = `<span class="badge-dot" style="background:#10b981;"></span> <b style="color:#10b981;">LIVE</b> (${d.smtp.host})`;
        } else {
          smtpBadge.innerHTML = `<span class="badge-dot" style="background:var(--muted-2);"></span> Simulated Fallback`;
        }
      }
    }catch(e){}
  }

  async function pollImap(){
    toast("Connecting to IMAP inbox and checking for new unread mail...", "ok");
    try{
      const res = await fetch("/api/v1/gateway/imap/poll", {method: "POST"});
      const d = await res.json();
      if(d.status === "SUCCESS"){
        if(d.polled_count > 0){
          toast(`✓ Ingested ${d.polled_count} email(s) via IMAP!`, "ok");
        } else {
          toast("Connected to IMAP: No new unread emails.", "ok");
        }
        loadStatus();
        loadStagedEmails();
      } else if(d.status === "SKIPPED"){
        toast("IMAP not configured in .env. Using Simulated Sandbox.", "warn");
      } else {
        toast(`IMAP Error: ${d.error || d.message}`, "bad");
      }
    }catch(e){
      toast("Failed to poll IMAP", "bad");
    }
  }

  // Export Gateway API
  window.Gateway = {
    loadStatus,
    changePolicy,
    loadStagedEmails,
    approveEmail,
    openReturn,
    closeReturnModal,
    confirmReturn,
    openSimModal,
    closeSimModal,
    selectScenario,
    executeDrop,
    setSourcePolicy,
    reapplyPolicy,
    toggleMatrix,
    openMatrixModal,
    closeMatrixModal,
    openStageDetail,
    closeStageDetail,
    toggleInbound,
    onMatrixSearch,
    clearMatrixSearch,
    toggleAddSource,
    closeAddSource,
    addSource,
    removeSource,
    bulkApprove,
    bulkReturn,
    onSearch,
    clearSearch,
    onIncQuar,
    bulkApproveMailbox,
    bulkReturnMailbox,
    startPolling,
    stopPolling,
    loadChannelStatus,
    pollImap,
    init: function(){
      const s = document.getElementById("retSub");
      const b = document.getElementById("retBody");
      if(s) s.addEventListener("input", updateMailtoLink);
      if(b) b.addEventListener("input", updateMailtoLink);
      applyInboundCollapse();   // restore the operator's collapse choice before the first paint
      loadStatus();
      loadStagedEmails();
      loadChannelStatus();
      startPolling();
    }
  };
})();
