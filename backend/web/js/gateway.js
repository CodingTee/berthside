/* ==========================================================================
   ShipSync Enterprise Hub · Gateway & Quarantine Sandbox Module
   ========================================================================== */

(function(){
  let currentScenario = "clean_bl";
  let currentReturnStage = null;
  let statusPollTimer = null;

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
      renderStagedList(data);
    }catch(e){
      console.error(e);
    }
  }

  function renderStagedList(items){
    const tbody = document.getElementById("stagedList");
    const badge = document.getElementById("stagedCountBadge");
    if(badge) badge.textContent = `(${items ? items.length : 0} records)`;
    if(!tbody) return;

    if(!items || items.length === 0){
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px;">No staged records found for this mailbox channel</td></tr>`;
      return;
    }
    let html = "";
    items.forEach(it => {
      const isBlocked = it.security_status === "BLOCKED";
      const secPill = isBlocked
        ? `<span class="pill pill-blocked">⛔ Malware Intercepted</span>`
        : `<span class="pill pill-clean">🛡️ Verified Clean</span>`;

      const catPill = it.category === "SPAM"
        ? `<span class="pill pill-warn">SPAM / Phishing</span>`
        : `<span class="pill" style="background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.3);">${it.category}</span>`;

      let actionBtns = "";
      if(it.status === "STAGED"){
        actionBtns = `
          <div style="display:flex;gap:6px;align-items:center;">
            <button class="btn btn-sm btn-primary" onclick="Gateway.approveEmail('${it.stage_id}')">✅ Approve & Ingest</button>
            <button class="btn btn-sm btn-danger" onclick="Gateway.openReturn('${it.stage_id}', true)">📤 Reject & Clarify</button>
          </div>
        `;
      } else if(it.status === "QUARANTINED"){
        if(isBlocked){
          actionBtns = `<span style="color:var(--bad);font-weight:600;font-size:12px;">🔒 Malware Blocked</span>`;
        } else {
          actionBtns = `
            <div style="display:flex;gap:6px;align-items:center;">
              <button class="btn btn-sm" style="border-color:var(--warn);color:var(--warn);background:rgba(251,191,36,.08);" onclick="Gateway.approveEmail('${it.stage_id}')">🔓 Release & Ingest</button>
              <button class="btn btn-sm" onclick="Gateway.openReturn('${it.stage_id}', true)">↩️ Return to Sender</button>
            </div>
          `;
        }
      } else if(it.status === "APPROVED" || it.status === "AUTO_INGESTED"){
        actionBtns = `
          <div style="display:flex;gap:6px;align-items:center;">
            <span style="color:var(--ok);font-weight:600;font-size:12px;">✓ Ingested</span>
            <button class="btn btn-sm" onclick="Gateway.openReturn('${it.stage_id}', false)">↩️ Return to Sender</button>
          </div>
        `;
      } else if(it.status === "RETURNED"){
        actionBtns = `
          <div style="display:flex;gap:6px;align-items:center;">
            <span style="color:var(--accent);font-weight:600;font-size:12px;">↩️ Returned via ${it.source_mailbox}</span>
            <button class="btn btn-sm" onclick="Gateway.openReturn('${it.stage_id}', false)">Return Again</button>
          </div>
        `;
      } else {
        actionBtns = `
          <div style="display:flex;gap:6px;align-items:center;">
            <span style="color:var(--muted);font-size:12px;">Rejected</span>
            <button class="btn btn-sm" onclick="Gateway.openReturn('${it.stage_id}', false)">↩️ Return to Sender</button>
          </div>
        `;
      }

      html += `
        <tr>
          <td style="font-family:var(--mono);font-size:12px;">${it.stage_id}</td>
          <td><span class="pill pill-src">${it.source_mailbox}</span><span class="eff-tag ${effectiveMode(it.source_mailbox) === 'auto' ? 'auto' : 'manual'}">${effectiveMode(it.source_mailbox) === 'auto' ? 'AUTO' : 'MANUAL'}</span></td>
          <td>
            <div style="font-weight:600;color:var(--text);">${it.subject}</div>
            <div style="font-size:11.5px;color:var(--muted);">Sender: ${it.sender || "-"}</div>
          </td>
          <td>
            ${secPill}
            ${it.attachments && it.attachments.length > 0 ? `<div style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-top:4px;">📎 ${it.attachments.join(", ")}</div>` : ""}
          </td>
          <td>${catPill}</td>
          <td><span style="font-size:12px;font-family:var(--mono);">${it.status}</span></td>
          <td>${actionBtns}</td>
        </tr>
      `;
    });
    tbody.innerHTML = html;
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

  function renderMatrix(policies, counts, mailboxes){
    const grid = document.getElementById("matrixGrid");
    if(!grid) return;
    if(!mailboxes || mailboxes.length === 0){
      grid.innerHTML = "";
      return;
    }
    let html = "";
    mailboxes.forEach(mb => {
      const forced = policies[mb];
      const mode = forced || effectiveMode(mb);
      const inherit = !forced;
      const staged = (counts && counts[mb]) || 0;
      const shortMb = mb.length > 30 ? mb.slice(0, 28) + "…" : mb;
      html += `
        <div class="mx-card">
          <div class="mx-top">
            <div>
              <div class="mx-mb" title="${mb}">${shortMb}</div>
              <div class="mx-role">${SRC_ROLES[mb] || "Mailbox source"}</div>
            </div>
            ${inherit
              ? `<span class="mx-pill inherit">Inherits global</span>`
              : `<span class="mx-pill forced">Overridden</span>`}
          </div>
          <div class="mx-bottom">
            <div class="seg" data-mb="${mb}">
              <button class="${inherit ? "active inherit" : "inherit"}" onclick="Gateway.setSourcePolicy('${mb.replace(/'/g, "\\'")}', 'inherit')">Inherit</button>
              <button class="${mode === 'auto' && !inherit ? "active auto" : "auto"}" onclick="Gateway.setSourcePolicy('${mb.replace(/'/g, "\\'")}', 'auto')">Auto</button>
              <button class="${mode === 'manual' && !inherit ? "active manual" : "manual"}" onclick="Gateway.setSourcePolicy('${mb.replace(/'/g, "\\'")}', 'manual')">Manual</button>
            </div>
            <div class="mx-actions">
              <button class="btn btn-sm btn-primary" onclick="Gateway.bulkApproveMailbox('${mb.replace(/'/g, "\\'")}')">Approve</button>
              <button class="btn btn-sm" onclick="Gateway.bulkReturnMailbox('${mb.replace(/'/g, "\\'")}')">Return</button>
            </div>
          </div>
          <div class="mx-count">Awaiting triage: <b>${staged}</b> &nbsp;·&nbsp; Effective: <b style="color:${mode==='auto'?'var(--ok)':'var(--warn)'}">${mode.toUpperCase()}</b></div>
        </div>
      `;
    });
    grid.innerHTML = html;
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
    const el = document.getElementById("bulkSrcLabel");
    if(el) el.textContent = mb === "ALL" ? "(all sources)" : "(" + mb + ")";
  }

  async function bulkApprove(scope){
    _bulkLabel();
    const mb = _currentFilter();
    const include_quarantined = (scope === "visible");
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

  async function bulkReturn(scope){
    _bulkLabel();
    const mb = _currentFilter();
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
    bulkApprove,
    bulkReturn,
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
      loadStatus();
      loadStagedEmails();
      loadChannelStatus();
      startPolling();
    }
  };
})();
