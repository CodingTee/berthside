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
    seq: 0,
    policy: { disposition_mode: "manual", disposition_policies: {} },
    mailboxes: [],
  };

  let statusLoaded = false;
  let curEmail = null;

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

  // ---- filtering + render --------------------------------------------------
  function filteredRows(){
    const mb = ($("outSelMailbox") || {}).value || "ALL";
    const qry = (($("outSearch") || {}).value || "").trim().toLowerCase();
    return (OUT.rows || []).filter(r => {
      if(mb !== "ALL" && r.source_mailbox !== mb) return false;
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

    const tbody = $("outList");
    if(!tbody) return;
    if(!rows.length){
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px;">No classified emails match this source / search.</td></tr>`;
      renderMatrix();
      return;
    }
    tbody.innerHTML = rows.map(rowHtml).join("");
    renderMatrix();
  }

  function onSearch(){ render(); }

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
      mismatchCell = `<span style="color:var(--ok);font-size:12px;">✓ SI ↔ BL aligned</span>`;
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
      replyCell = `<span class="pill pill-clean" style="background:rgba(16,185,129,.12);color:#10b981;border:1px solid rgba(16,185,129,.3);">✅ Replied</span>`;
    } else if(it.reply_state === "awaiting"){
      if(eff === "auto"){
        replyCell = `<span class="pill" style="background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.3);">⚡ Auto-queued</span>`;
      } else {
        replyCell = `<span class="pill" style="background:rgba(251,191,36,.12);color:var(--warn);border:1px solid rgba(251,191,36,.3);">⏳ Awaiting human</span>`;
      }
    } else {
      replyCell = `<span style="color:var(--muted);font-size:12px;">—</span>`;
    }

    const actions =
      `<div style="display:flex;gap:6px;">` +
      `<button class="btn btn-sm" onclick="Outstream.openReturn('${q(id)}')">↩ Reply</button>` +
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
    if(!mbs.length){ grid.innerHTML = ""; return; }

    const pol = OUT.policy || {};
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
      if(res.ok){
        const d = await res.json();
        OUT.policy = {
          disposition_mode: d.disposition_mode,
          disposition_policies: d.disposition_policies || {},
        };
        toast(`Outstream response policy set to ${mode}`, "ok");
        render();
      } else {
        toast("Failed to update response policy", "bad");
      }
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
      if(res.ok){
        const d = await res.json();
        OUT.policy = {
          disposition_mode: d.disposition_mode,
          disposition_policies: d.disposition_policies || {},
        };
        toast(`Source ${mb} response set to ${mode}`, "ok");
        render();
      } else {
        toast("Failed to update source policy", "bad");
      }
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
      if(res.ok){
        const row = (OUT.rows || []).find(r => r.email_id === email_id);
        if(row){
          row.disposition_override = d.disposition_override;
          row.effective_disposition = d.effective_disposition;
        }
        toast(`Disposition for ${email_id} -> ${d.effective_disposition}`, "ok");
        render();
      } else {
        toast("Failed to set disposition", "bad");
      }
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

  async function openReturn(email_id){
    curEmail = email_id;
    const row = (OUT.rows || []).find(r => r.email_id === email_id);
    try{
      const res = await fetch(`/api/emails/${encodeURIComponent(email_id)}/return`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ dry_run: true }),
      });
      const d = await res.json();
      const via = $("outRetVia"); if(via) via.textContent = d.reply_via || "-";
      const to = $("outRetTo"); if(to) to.textContent = (row && row.from) ? row.from : (d.reply_via || "-");
      const sub = $("outRetSub"); if(sub) sub.value = d.subject || "";
      const body = $("outRetBody"); if(body) body.value = d.body || "";
      const del = $("outRetDelivery");
      if(del) del.textContent = d.decision === "REJECTED"
        ? "(reply: amendment request / clarification)"
        : "(reply: verification result)";
      updateMailto();
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

  async function confirmReturn(){
    if(!curEmail) return;
    const subject = ($("outRetSub") || {}).value || "";
    const body = ($("outRetBody") || {}).value || "";
    try{
      const res = await fetch(`/api/emails/${encodeURIComponent(curEmail)}/return`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ subject, body }),
      });
      const d = await res.json();
      toast(`Replied via ${d.reply_via} (${d.delivery})`, "ok");
      closeReturnModal();
      await reload();
    }catch(e){
      toast("Failed to dispatch reply", "bad");
    }
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
        if(res.ok) okN++; else failN++;
      }catch(_){ failN++; }
    }));
    toast(`Auto-replied ${okN} (failed ${failN})`, failN ? "warn" : "ok");
    await reload();
  }

  // ---- export --------------------------------------------------------------
  window.Outstream = {
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
    bulkReplyEligible,
  };
})();
