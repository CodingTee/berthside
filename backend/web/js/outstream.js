/* ==========================================================================
   ShipSync Master Console · Outstream Buffer Module
   --------------------------------------------------------------------------
   Post-classification disposition buffer. Mirrors the Enterprise Hub's
   instream trust matrix (global + per-source auto/manual) but governs the
   *response* after classification: which mismatched / escalated emails get an
   automatic SIMULATED reply vs which stay parked for a human. Single-item
   overrides win over per-source policy win over the global default.

   Content (SI <-> BL mismatch column) follows review.js; controls + matrix +
   return modal follow gateway.js. Self-contained: no shared-chrome binding,
   only window.Outstream is exported, so it cannot collide with the other
   console modules' top-level identifiers.
   ========================================================================== */

(function(){
  const OUT = {
    rows: [],
    queue: "reply",
    seq: 0,
    policy: { disposition_mode: "manual", disposition_policies: {} },
    mailboxes: [],
  };

  let statusLoaded = false;
  let curEmail = null, approvalId = null, sendBusy = false, editVersion = 0;

  const SRC_ROLES = {
    "sdoc-hackathon-bundle@averis.com": "Benchmark EDI / API bundle",
    "operations@shipsync.demo": "Direct mail influx (live ops)",
    "docs.export@averis.com": "Export documentation",
    "booking@averis.com": "Booking confirmations",
    "april.shipping@averis.com": "APRIL pulp & paper BU",
    "finance@averis.com": "Freight & finance desk",
    "transpacific@averis.com": "Transpacific trade lane",
  };

  const FIELD_LABEL = {
    shipper: "Shipper", consignee: "Consignee", notify_party: "Notify Party",
    port_of_loading: "POL", port_of_discharge: "POD",
    container_count: "Containers", gross_weight_kg: "Gross Weight",
  };

  // ---- small helpers -------------------------------------------------------
  function $(id){ return document.getElementById(id); }

  function setText(id, text){
    const el = $(id);
    if(el) el.textContent = text;
  }

  function esc(s){
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Escape a value for embedding inside a single-quoted JS string in an
  // onclick="" attribute (the attribute itself is double-quoted).
  function q(s){ return String(s).replace(/'/g, "\\'"); }

  function toast(msg, type="ok"){
    if(window.showToast){ window.showToast(msg, type); return; }
    const t = $("toast");
    if(!t) return;
    t.textContent = msg;
    t.className = "toast show " + type;
    setTimeout(() => { t.className = "toast"; }, 3200);
  }

  function opt(v, cur){
    const label = v.charAt(0) + v.slice(1).toLowerCase();
    return `<option value="${v}" ${v === cur ? "selected" : ""}>${label}</option>`;
  }

  function catBadge(c){
    if(!c) return `<span class="badge cat" style="--c:var(--muted-2)">-</span>`;
    return `<span class="badge cat" style="--c:var(--cat-${c})">${esc(c.replace(/_/g, " "))}</span>`;
  }

  function stBadge(s){
    if(!s) return `<span class="badge st" style="--c:var(--muted-2)">-</span>`;
    return `<span class="badge st ${s}">${esc(s.replace(/_/g, " "))}</span>`;
  }

  function effectiveDisposition(mb){
    const pol = OUT.policy || {};
    const dp = pol.disposition_policies || {};
    if(mb && dp[mb]) return dp[mb];
    return pol.disposition_mode || "manual";
  }

  // ---- data load -----------------------------------------------------------
  async function ensureStatus(){
    if(statusLoaded) return;
    try{
      const res = await fetch("/api/v1/gateway/status");
      const data = await res.json();
      OUT.policy = {
        disposition_mode: data.policy.disposition_mode || "manual",
        disposition_policies: data.policy.disposition_policies || {},
      };
      OUT.mailboxes = data.available_mailboxes || [];
      const selMode = $("outSelMode");
      if(selMode && OUT.policy.disposition_mode) selMode.value = OUT.policy.disposition_mode;
      statusLoaded = true;
    }catch(e){
      console.warn("outstream: could not load gateway status", e);
    }
  }

  async function reload(){
    const my = ++OUT.seq;
    try{
      const res = await fetch("/api/emails?limit=2000");
      const data = await res.json();
      if(my !== OUT.seq) return;  // a newer reload landed; drop stale payload
      OUT.rows = data.items || [];
      render();
    }catch(e){
      console.error("outstream: failed to load emails", e);
    }
  }

  function init(){
    ensureStatus().then(reload);
  }

  document.addEventListener("DOMContentLoaded",()=>ensureStatus().then(reload));

  // ---- filtering + render --------------------------------------------------
  function filteredRows(){
    const mb = ($("outSelMailbox") || {}).value || "ALL";
    const qry = (($("outSearch") || {}).value || "").trim().toLowerCase();
    return (OUT.rows || []).filter(r => {
      if(mb !== "ALL" && r.source_mailbox !== mb) return false;
      if(OUT.queue === "reply" && r.reply_state !== "awaiting") return false;
      if(OUT.queue === "failed" && r.reply_state !== "failed") return false;
      if(OUT.queue === "history" && !r.has_dispatch) return false;
      if(qry){
        const hay = [
          r.email_id, r.from, r.subject, r.source_mailbox,
          r.category, r.status, (r.defect_fields || []).join(" "),
        ].join(" ").toLowerCase();
        if(hay.indexOf(qry) === -1) return false;
      }
      return true;
    });
  }

  function setQueue(queue){
    OUT.queue = queue;
    setText("replyQueueTitle", {reply:"Awaiting reply",failed:"Failed deliveries",history:"Delivery history"}[queue] || "Replies");
    const bulk = document.getElementById("replyBulkSend");
    if(bulk) bulk.hidden = queue !== "reply";
    render();
  }
  function render(){
    const rows = filteredRows();
    const total = rows.length;
    const awaiting = rows.filter(r => r.reply_state === "awaiting").length;
    const sent = rows.filter(r => r.reply_state === "sent").length;
    const review = rows.filter(r =>
      (r.status === "MISMATCH" || r.status === "NEEDS_REVIEW") &&
      r.effective_disposition === "manual").length;

    setText("outKpiTotal", total);
    setText("outKpiAwaiting", awaiting);
    setText("outKpiSent", sent);
    setText("outKpiReview", review);
    setText("outReplyCounter", `↩ Reply needed: ${awaiting}`);
    setText("outCountBadge", `(${total} email${total === 1 ? "" : "s"})`);
    renderDist({ awaiting, sent, review, other: Math.max(0, total - awaiting - sent - review) });

    const tbody = $("outList");
    if(!tbody) return;
    if(!rows.length){
      const message = OUT.queue === "failed" ? "No failed deliveries match this source / search." : OUT.queue === "history" ? "No delivery history matches this source / search." : "No replies are waiting for this source / search.";
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px;">${message}</td></tr>`;
      renderMatrix();
      return;
    }
    tbody.innerHTML = rows.map(rowHtml).join("");
    renderMatrix();
  }

  function onSearch(){ render(); }

  function renderDist(parts){
    const segs = [["outSegAwaiting", parts.awaiting], ["outSegSent", parts.sent],
      ["outSegReview", parts.review], ["outSegOther", parts.other]];
    const total = segs.reduce((a, s) => a + (Number(s[1]) || 0), 0);
    segs.forEach(([id, v]) => {
      const el = $(id);
      if(!el) return;
      el.style.flexGrow = total > 0 ? String(Number(v) || 0) : "1";
    });
  }

  // ---- row rendering -------------------------------------------------------
  function rowHtml(it){
    const id = it.email_id;
    const eff = it.effective_disposition || "manual";
    const ov = it.disposition_override || "INHERIT";
    const mismatch = (it.status === "MISMATCH" || it.status === "NEEDS_REVIEW");

    let mismatchCell;
    if(mismatch && it.defect_fields && it.defect_fields.length){
      mismatchCell = it.defect_fields
        .map(f => `<span class="mx-pill forced" style="margin:2px;">${esc(FIELD_LABEL[f] || f)}</span>`)
        .join(" ");
    } else if(mismatch){
      mismatchCell = `<span style="color:var(--warn);font-size:12px;">${esc(it.review_reason || "escalated")}</span>`;
    } else {
      mismatchCell = `<span style="color:var(--ok);font-size:12px;">${it.status === "OK" ? "✓ SI ↔ BL aligned" : "No SI / BL comparison result"}</span>`;
    }

    const shortMb = it.source_mailbox && it.source_mailbox.length > 30
      ? it.source_mailbox.slice(0, 28) + "…" : it.source_mailbox;
    const srcCell = `<span class="pill pill-src" title="${esc(it.source_mailbox || "")}">${esc(shortMb || "-")}</span>`;

    const dispCell =
      `<span class="eff-tag ${eff === "auto" ? "auto" : "manual"}">${eff.toUpperCase()}</span>` +
      `<select class="select-ctl" style="margin-top:4px;min-width:110px;" onchange="Outstream.setItemDisposition('${q(id)}', this.value)">` +
      opt("INHERIT", ov) + opt("AUTO", ov) + opt("MANUAL", ov) +
      `</select>`;

    let replyCell;
    if(it.reply_state === "sent"){
      replyCell = `<span class="pill pill-clean" style="background:rgba(16,185,129,.12);color:#10b981;border:1px solid rgba(16,185,129,.3);">Sent · SMTP accepted</span>`;
    } else if(it.reply_state === "failed"){
      const why = it.last_error ? String(it.last_error) : "";
      replyCell = `<span style="color:var(--bad)" ${why ? `title="${esc(why)}"` : ""}>Send failed${why ? ": " + esc(why.length > 80 ? why.slice(0, 80) + "..." : why) : ""}</span>`;
    } else if(it.reply_state === "simulated"){
      replyCell = `<span style="color:var(--warn)">Simulated · not sent</span>`;
    } else if(it.reply_state === "unknown"){
      replyCell = `<span>Delivery unconfirmed</span>`;
    } else if(it.reply_state === "awaiting"){
      if(eff === "auto"){
        replyCell = `<span class="pill" style="background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.3);">⚡ Auto-queued</span>`;
      } else {
        replyCell = `<span class="pill" style="background:rgba(251,191,36,.12);color:var(--warn);border:1px solid rgba(251,191,36,.3);">⏳ Awaiting human</span>`;
      }
    } else {
      replyCell = `<span style="color:var(--muted);font-size:12px;">—</span>`;
    }

    const replyLabel = it.reply_state === "sent" ? "New reply"
      : it.reply_state === "failed" ? "Retry" : "Reply";
    const actions =
      `<div style="display:flex;gap:6px;">` +
      `<button class="btn btn-sm" onclick="ReviewReply.inspect('${q(id)}')">Review</button>` +
      `<button class="btn btn-sm" onclick="ReviewReply.inspect('${q(id)}', true)">History</button>` +
      `<button class="btn btn-sm" onclick="Outstream.openReturn('${q(id)}')">${readDraft(id) ? "✎ " : ""}${replyLabel}</button>` +
      `</div>`;

    return `<tr>
      <td><div style="font-weight:600;color:var(--text);">${esc(it.subject || "(no subject)")}</div>
          <div style="font-size:11px;color:var(--muted);font-family:var(--mono);">${esc(id)} · ${esc(it.from || "-")}</div></td>
      <td>${catBadge(it.category)} ${stBadge(it.status)}</td>
      <td>${mismatchCell}</td>
      <td>${srcCell}</td>
      <td>${dispCell}</td>
      <td>${replyCell}</td>
      <td>${actions}</td>
    </tr>`;
  }

  // ---- disposition matrix (mirrors gateway's source trust matrix) ----------
  function renderMatrix(){
    const grid = $("outMatrixGrid");
    if(!grid) return;
    const mbs = OUT.mailboxes || [];
    const pol = OUT.policy || {};

    // Keep the kebab summary live even when the current queue is empty,
    // otherwise it stays on the "sources" placeholder forever.
    const total = mbs.length;
    const auto = mbs.filter(mb => {
      const forced = (pol.disposition_policies || {})[mb];
      return (forced || pol.disposition_mode || "manual") === "auto";
    }).length;
    const kebab = $("outKebabSum");
    if(kebab) kebab.textContent = total + " src · " + auto + " auto / " + (total - auto) + " manual";

    if(!mbs.length){ grid.innerHTML = ""; return; }

    const dp = pol.disposition_policies || {};

    let html = "";
    mbs.forEach(mb => {
      const forced = dp[mb];
      const mode = forced || (pol.disposition_mode || "manual");
      const inherit = !forced;
      const rows = (OUT.rows || []).filter(r => r.source_mailbox === mb);
      const awaiting = rows.filter(r => r.reply_state === "awaiting").length;
      const eligible = rows.filter(r =>
        r.reply_state === "awaiting" && r.effective_disposition === "auto").length;
      const shortMb = mb.length > 30 ? mb.slice(0, 28) + "…" : mb;

      html += `
        <div class="mx-card">
          <div class="mx-top">
            <div>
              <div class="mx-mb" title="${esc(mb)}">${esc(shortMb)}</div>
              <div class="mx-role">${esc(SRC_ROLES[mb] || "Mailbox source")}</div>
            </div>
            ${inherit
              ? `<span class="mx-pill inherit">Inherits global</span>`
              : `<span class="mx-pill forced">Overridden</span>`}
          </div>
          <div class="mx-bottom">
            <div class="seg" data-mb="${esc(mb)}">
              <button class="${inherit ? "active inherit" : "inherit"}" onclick="Outstream.setSourceDisposition('${q(mb)}', 'inherit')">Inherit</button>
              <button class="${mode === "auto" && !inherit ? "active auto" : "auto"}" onclick="Outstream.setSourceDisposition('${q(mb)}', 'auto')">Auto</button>
              <button class="${mode === "manual" && !inherit ? "active manual" : "manual"}" onclick="Outstream.setSourceDisposition('${q(mb)}', 'manual')">Manual</button>
            </div>
          </div>
          <div class="mx-count">Awaiting reply: <b>${awaiting}</b> · Auto-eligible: <b>${eligible}</b> · Effective: <b style="color:${mode === "auto" ? "var(--ok)" : "var(--warn)"}">${mode.toUpperCase()}</b></div>
        </div>`;
    });
    grid.innerHTML = html;
  }

  function openMatrixModal(){
    const m = $("outMatrixModal");
    if(m) m.classList.remove("hidden");
    renderMatrix();
  }
  function closeMatrixModal(){
    const m = $("outMatrixModal");
    if(m) m.classList.add("hidden");
  }

  // ---- policy writes -------------------------------------------------------
  async function setGlobalDisposition(){
    const sel = $("outSelMode");
    if(!sel) return;
    const mode = sel.value;
    try{
      const res = await fetch("/api/v1/gateway/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ disposition_mode: mode }),
      });
      const d = await res.json();
      if(!res.ok) throw new Error(d.detail || "Request failed");
      OUT.policy = {
        disposition_mode: d.disposition_mode,
        disposition_policies: d.disposition_policies || {},
      };
      toast(`Outstream response policy set to ${mode}`, "ok");
      render();
    }catch(e){
      toast("Failed to update response policy", "bad");
    }
  }

  async function setSourceDisposition(mb, mode){
    const dp = Object.assign({}, (OUT.policy && OUT.policy.disposition_policies) || {});
    if(mode === "inherit") delete dp[mb];
    else dp[mb] = mode;
    try{
      const res = await fetch("/api/v1/gateway/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ disposition_policies: dp }),
      });
      const d = await res.json();
      if(!res.ok) throw new Error(d.detail || "Request failed");
      OUT.policy = {
        disposition_mode: d.disposition_mode,
        disposition_policies: d.disposition_policies || {},
      };
      toast(`Source ${mb} response set to ${mode}`, "ok");
      render();
    }catch(e){
      toast("Failed to update source policy", "bad");
    }
  }

  async function setItemDisposition(email_id, override){
    try{
      const res = await fetch(`/api/emails/${encodeURIComponent(email_id)}/disposition`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ override }),
      });
      const d = await res.json();
      if(!res.ok) throw new Error(d.detail || "Request failed");
      if(!OUT.rows.length) await reload();
      const row = (OUT.rows || []).find(r => r.email_id === email_id);
      if(row){
        row.disposition_override = d.disposition_override;
        row.effective_disposition = d.effective_disposition;
      }
      toast(`Disposition for ${email_id} -> ${d.effective_disposition}`, "ok");
      render();
    }catch(e){
      toast("Failed to set disposition", "bad");
    }
  }

  // ---- reply / return modal ------------------------------------------------
  function updateMailto(){
    const btn = $("outRetMailtoBtn");
    if(!btn) return;
    const to = (($("outRetTo") || {}).textContent || "").trim();
    const sub = ($("outRetSub") || {}).value || "";
    const body = ($("outRetBody") || {}).value || "";
    if(to && to !== "-"){
      btn.href = `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(sub)}&body=${encodeURIComponent(body)}`;
    } else {
      btn.href = "#";
    }
  }

  // ---- reply drafts (localStorage: survives closing the modal) --------------
  const draftKey = id => "sdoc-reply-draft:" + id;
  function readDraft(id){
    try{ const raw = localStorage.getItem(draftKey(id)); return raw ? JSON.parse(raw) : null; }
    catch(e){ return null; }
  }
  function writeDraft(){
    if(!curEmail) return;
    try{
      localStorage.setItem(draftKey(curEmail), JSON.stringify({
        subject: ($("outRetSub") || {}).value || "",
        body: ($("outRetBody") || {}).value || "",
        ts: Date.now(),
      }));
    }catch(e){}
    setDraftNote(true);
  }
  function dropDraft(){
    if(curEmail){ try{ localStorage.removeItem(draftKey(curEmail)); }catch(e){} }
    setDraftNote(false);
  }
  function setDraftNote(on){
    const n = $("outRetDraftNote");
    if(n) n.style.display = on ? "flex" : "none";
  }
  /* Input handler: every edit both stores a draft and voids the approval. */
  function noteDraftEdit(){
    invalidateApproval();
    writeDraft();
  }
  /* Drop the draft and pull the pristine template again. */
  function discardDraft(){
    if(!curEmail) return;
    dropDraft();
    toast("Draft discarded", "ok");
    openReturn(curEmail);
  }

  async function openReturn(email_id){
    curEmail = email_id;
    invalidateApproval();
    setDraftNote(false);
    if(!OUT.rows.length) await reload();
    const row = (OUT.rows || []).find(r => r.email_id === email_id);
    try{
      const res = await fetch(`/api/emails/${encodeURIComponent(email_id)}/return`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ dry_run: true }),
      });
      const d = await res.json();
      if(!res.ok) throw new Error(d.detail || "Request failed");
      const via = $("outRetVia"); if(via) via.textContent = d.reply_via || "-";
      const to = $("outRetTo"); if(to) to.textContent = d.recipient || ((row && row.from) ? row.from : "-");
      const sub = $("outRetSub"); if(sub) sub.value = d.subject || "";
      const body = $("outRetBody"); if(body) body.value = d.body || "";
      // an unsent draft from an earlier visit wins over the fresh template
      const draft = readDraft(email_id);
      if(draft && (draft.subject || draft.body)){
        if(sub && draft.subject !== undefined) sub.value = draft.subject;
        if(body && draft.body !== undefined) body.value = draft.body;
        setDraftNote(true);
      }
      const del = $("outRetDelivery");
      if(del) del.textContent = d.decision === "REJECTED"
        ? "(reply: amendment request / clarification)"
        : "(reply: verification result)";
      invalidateApproval();
      const m = $("outReturnModal");
      if(m) m.classList.remove("hidden");
    }catch(e){
      toast("Failed to prepare reply", "bad");
    }
  }

  function closeReturnModal(){
    const m = $("outReturnModal");
    if(m) m.classList.add("hidden");
  }

  function invalidateApproval(){
    approvalId=null; editVersion++;
    if($("outSendBtn")) $("outSendBtn").disabled=true;
    if($("outApprovalStatus")) $("outApprovalStatus").textContent="Review recipient, subject and body, then approve. Editing requires approval again.";
  }
  async function approveReply(){
    if(sendBusy || !curEmail) return;
    const version=editVersion, id=curEmail;
    $("outApproveBtn").disabled=true;
    try{
      const res=await fetch(`/api/emails/${encodeURIComponent(id)}/approve-reply`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({subject:$("outRetSub").value,body:$("outRetBody").value})});
      const d=await res.json();if(!res.ok) throw new Error(d.detail||"Approval failed");
      if(id!==curEmail || version!==editVersion) return;
      approvalId=d.approval_id;$("outSendBtn").disabled=false;
      $("outApprovalStatus").textContent="Approved by "+d.reviewer+". Ready to send this exact reply.";
    }catch(e){toast(e.message,"bad");}finally{$("outApproveBtn").disabled=false;}
  }
  async function confirmReturn(){
    if(!curEmail || !approvalId || sendBusy) return;
    sendBusy=true;$("outSendBtn").disabled=true;$("outApproveBtn").disabled=true;
    $("outRetSub").disabled=true;$("outRetBody").disabled=true;
    try{
      const res=await fetch(`/api/emails/${encodeURIComponent(curEmail)}/return`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({subject:$("outRetSub").value,body:$("outRetBody").value,approval_id:approvalId})});
      const d=await res.json();if(!res.ok) throw new Error(d.detail||"Send failed");
      // a failed dispatch keeps its draft so the retry starts from the same text
      if(d.delivery !== "FAILED") dropDraft();
      toast(d.delivery === "FAILED" ? "Sending failed. A new review is required before retrying." : d.delivery === "SIMULATED" ? "Simulation recorded. No email was sent." : "SMTP accepted the reply; receipt unconfirmed.",d.delivery === "FAILED"?"bad":"ok");
      closeReturnModal();await reload();if(window.loadList) window.loadList();
      if(window.ReviewReply) window.ReviewReply.refresh();
    }catch(e){toast(e.message,"bad");}
    finally{sendBusy=false;invalidateApproval();$("outApproveBtn").disabled=false;$("outRetSub").disabled=false;$("outRetBody").disabled=false;}
  }

  async function bulkReplyEligible(){
    const eligible = (OUT.rows || []).filter(
      r => r.reply_state === "awaiting" && r.effective_disposition === "auto");
    if(!eligible.length){
      toast("No auto-eligible replies pending", "ok");
      return;
    }
    toast(`Auto-replying ${eligible.length} eligible email(s)...`, "ok");
    let okN = 0, failN = 0;
    await Promise.all(eligible.map(async r => {
      try{
        const res = await fetch(`/api/emails/${encodeURIComponent(r.email_id)}/return`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({}),
        });
        const outcome = await res.json();
        if(res.ok && !["FAILED", "ERROR"].includes(outcome.delivery)) okN++; else failN++;
      }catch(_){ failN++; }
    }));
    toast(`Auto-replied ${okN} (failed ${failN})`, failN ? "warn" : "ok");
    await reload();
  }

  // ---- export --------------------------------------------------------------
  window.Outstream = {
    setQueue,
    init,
    reload,
    render,
    onSearch,
    setGlobalDisposition,
    setSourceDisposition,
    setItemDisposition,
    openReturn,
    closeReturnModal,
    confirmReturn,
    approveReply,
    noteDraftEdit,
    discardDraft,
    invalidateApproval,
    bulkReplyEligible,
    openMatrixModal,
    closeMatrixModal,
  };
})();
