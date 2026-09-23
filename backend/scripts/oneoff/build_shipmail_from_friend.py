"""Build ShipMail frontend directly from friend repo, removing only the simulated branch."""
from pathlib import Path
import re

FRIEND_PATH = Path(r"C:\Users\gayso\Desktop\Hackathon Averix\friend repo\backend\shipmail\frontend\index.html")
TARGET_PATH = Path(r"C:\Users\gayso\Desktop\Hackathon Averix\repo\backend\shipmail\frontend\index.html")

content = FRIEND_PATH.read_text(encoding="utf-8")

# 1. Remove demo badge in topbar
content = content.replace(
    '<span class="ltag" data-tip="Simulated mailbox" data-tip-desc="Everything on this page is a bundled demo bundle — no real mailbox is connected" data-tip-pos="bottom" data-page-node-id="Ds8aehZQxCqXoT3DVELWjE">demo</span>',
    ''
)

# 2. Update topbar conn-chip
content = content.replace(
    '<span class="conn-chip" id="connChip" data-tip="Mail source" data-tip-desc="Where this inbox comes from — the bundled demo bundle, or a real Gmail mailbox" data-tip-pos="bottom" data-page-node-id="YvearCCxlOXEmfsxOVL1e7"><i class="dot" data-page-node-id="L470NSEj7K4zPwLMbIc6Sm"></i><span id="connTxt" data-page-node-id="afMfaB10bSnwKy8hBZF0jI">Simulated</span></span>',
    '<span class="conn-chip" id="connChip" data-tip="Gmail Account" data-tip-desc="Connected to real Gmail via Google OAuth" data-tip-pos="bottom"><i class="dot"></i><span id="connTxt">Gmail Disconnected</span></span>'
)

# 3. Update You card in topbar
content = content.replace(
    '<div class="you-id" data-page-node-id="c1TLVUESwBlkvyYHCoTXTy"><b data-page-node-id="zlOrH6NTpX114uwwn3DRCE">You</b><span data-page-node-id="hXEhVUe9xA7wi5HMIAhmAv">Signed in as Operations — this demo has no real account behind it.</span></div>',
    '<div class="you-id" data-page-node-id="c1TLVUESwBlkvyYHCoTXTy"><b data-page-node-id="zlOrH6NTpX114uwwn3DRCE">Operations</b><span data-page-node-id="hXEhVUe9xA7wi5HMIAhmAv">Signed in to ShipMail</span></div>'
)

# 4. Update Mail source rail card to remove simRow
old_rail_card = """      <div class="panel-box rail-card" data-page-node-id="9aGaHPEAycFC7TOJ37B9My">
        <h4 data-page-node-id="z69hEVBb7WGnFHxfE9s4Dx">Mail source</h4>
        <div class="legend" data-page-node-id="APE7NbDnF7Zo5fdAWB8I37">
          <div id="simRow" role="button" tabindex="0" data-page-node-id="7Uu4CFzbTvjgJdChh5HEd2"><i style="background:var(--ok)" data-page-node-id="vBrSG1VjOqwkzNYAb83Oef"></i> Simulated inbox</div>
          <div id="gmailRow" role="button" tabindex="0" data-page-node-id="Db1MCSEY3ZZDil3HQTw5p5"><i style="background:var(--muted-2)" data-page-node-id="t0sgWIR3zEu7coy6etaBk8"></i> Real Gmail</div>
        </div>
        <div style="display:flex;gap:6px;margin-top:9px;flex-wrap:wrap" data-page-node-id="v9tn80Ka1qWSfBktPcHAKS">
          <button class="btn sm" id="gmailConnect" type="button" hidden data-page-node-id="1SvHx00mJVcL7m4UfAkzNn">Connect Gmail</button>
          <button class="btn sm" id="gmailSync" type="button" hidden data-page-node-id="CM1gXJLrt0uNvz5y6BeviJ">Sync now</button>
        </div>
        <div id="gmailNote" class="note-src" data-page-node-id="eWpQ6uZ7M7UMvjCh81mQhk"></div>
      </div>"""

new_rail_card = """      <div class="panel-box rail-card" data-page-node-id="9aGaHPEAycFC7TOJ37B9My">
        <h4 data-page-node-id="z69hEVBb7WGnFHxfE9s4Dx">Gmail Account</h4>
        <div class="legend" data-page-node-id="APE7NbDnF7Zo5fdAWB8I37">
          <div id="gmailRow" class="active" role="button" tabindex="0" data-page-node-id="Db1MCSEY3ZZDil3HQTw5p5"><i style="background:var(--muted-2)" data-page-node-id="t0sgWIR3zEu7coy6etaBk8"></i> Real Gmail</div>
        </div>
        <div style="display:flex;gap:6px;margin-top:9px;flex-wrap:wrap" data-page-node-id="v9tn80Ka1qWSfBktPcHAKS">
          <button class="btn sm" id="gmailConnect" type="button" data-page-node-id="1SvHx00mJVcL7m4UfAkzNn">Connect Gmail</button>
          <button class="btn sm" id="gmailSync" type="button" hidden data-page-node-id="CM1gXJLrt0uNvz5y6BeviJ">Sync now</button>
        </div>
        <div id="gmailNote" class="note-src" data-page-node-id="eWpQ6uZ7M7UMvjCh81mQhk"></div>
        <button class="btn ghost sm" id="gmailSetupLink" type="button" style="width:100%;justify-content:center;margin-top:7px;font-size:11px;padding:4px 8px;border-style:dashed;color:var(--muted-2);">
          ⚙️ OAuth Setup Guide &amp; Key
        </button>
      </div>"""

if old_rail_card in content:
    content = content.replace(old_rail_card, new_rail_card)
else:
    print("WARNING: old_rail_card not found exactly")

# 5. Add Gmail OAuth Setup Modal before the main <script> tag in body
modal_html = """<!-- ===================== GMAIL OAUTH SETUP MODAL ===================== -->
<div class="help" id="gmailSetupModal" hidden>
  <div class="help-card" style="width:min(580px, 95vw);max-height:88vh;" role="dialog" aria-label="Gmail OAuth Setup">
    <div class="help-head">
      <div class="ttl">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>
        </svg>
        Gmail OAuth Configuration Required
      </div>
      <button class="x" id="gmailSetupClose" type="button" aria-label="Close setup modal">&#10005;</button>
    </div>
    <div class="help-body" style="padding:16px 20px;display:flex;flex-direction:column;gap:14px;">
      <div style="font-size:12.8px;color:var(--text);line-height:1.6;">
        To connect your real Gmail mailbox, Google OAuth 2.0 requires an authorized Client ID credentials JSON file (<code style="font-family:var(--mono);font-size:11.5px;background:var(--surface-3);border:1px solid var(--border);border-radius:4px;padding:2px 6px;">google_oauth_client.json</code>).
      </div>

      <div style="background:var(--surface-3);border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-size:12px;line-height:1.6;">
        <div style="font-weight:650;color:var(--text);margin-bottom:6px;">Quick 4-Step Google Cloud Setup:</div>
        <ol style="margin:0;padding-left:18px;color:var(--muted);display:flex;flex-direction:column;gap:5px;">
          <li>Open <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener" style="color:var(--accent);">Google Cloud Console → Credentials</a> and ensure <b>Gmail API</b> is enabled.</li>
          <li>Create <b>OAuth Client ID</b> (Application type: <b>Web application</b>).</li>
          <li>Add Authorized Redirect URI:
            <div style="display:flex;align-items:center;gap:6px;margin-top:3px;">
              <code id="redirectUriCode" style="background:var(--bg-2);padding:3px 8px;border-radius:5px;border:1px solid var(--border-strong);font-family:var(--mono);color:var(--accent);font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;">http://127.0.0.1:8000/api/gmail/oauth-callback</code>
              <button class="btn sm ghost" id="copyRedirectBtn" type="button" style="padding:3px 8px;font-size:11px;">Copy</button>
            </div>
          </li>
          <li>Under <i>OAuth consent screen → Test users</i>, add your Gmail address, then <b>Download JSON</b>.</li>
        </ol>
      </div>

      <div>
        <div style="font-size:12.5px;font-weight:600;color:var(--text);margin-bottom:6px;">Upload or Paste Credentials JSON:</div>
        <div id="dropZone" style="border:2px dashed var(--border-strong);border-radius:10px;padding:16px 14px;text-align:center;background:var(--surface);cursor:pointer;transition:.18s;margin-bottom:10px;">
          <input type="file" id="clientFileInput" accept=".json,application/json" style="display:none;">
          <div style="font-size:12.5px;color:var(--text);font-weight:600;">📁 Click or Drag &amp; Drop <span style="color:var(--accent);">client_secret_*.json</span> here</div>
          <div style="font-size:11px;color:var(--muted-2);margin-top:3px;">Auto-detects and configures for ShipMail</div>
        </div>
        <textarea id="clientJsonText" placeholder="Or paste the client JSON content here directly..." style="width:100%;height:90px;box-sizing:border-box;border-radius:8px;background:var(--bg-2);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:11px;padding:8px 10px;outline:none;resize:vertical;"></textarea>
      </div>

      <div id="setupFeedback" style="display:none;font-size:12px;padding:8px 12px;border-radius:8px;"></div>
    </div>
    <div class="help-foot" style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
      <span style="font-size:11px;color:var(--muted-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">Target: <code style="font-family:var(--mono);font-size:10.5px;">backend/secrets/google_oauth_client.json</code></span>
      <button class="btn primary" id="saveCredentialsBtn" type="button">Save &amp; Connect</button>
    </div>
  </div>
</div>

<script>"""

main_script_target = '<div class="toast" id="toast" role="status" aria-live="polite" data-page-node-id="nOsevsHtdMZQtDzjC8zATw">\n  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" data-page-node-id="1Lcp5QSrCgzCvoEC3eatfk"><path d="M20 6L9 17l-5-5" data-page-node-id="t3fqdPi5MBixjn1IKpIlf4"/></svg>\n  <span id="toastMsg" data-page-node-id="ImVgbo238GGNLpRHbbATqo"></span>\n</div>\n\n<script>'

content = content.replace(main_script_target, main_script_target.replace('<script>', modal_html))

# 6. Change default source in state: source: "real"
content = content.replace('source: "sim"      /* "sim" (bundled demo) | "real" (live Gmail mailbox) */', 'source: "real"')

# 7. Update sourceRowHighlight
old_source_hl = """function sourceRowHighlight(){
  const sim = $("simRow"), g = $("gmailRow");
  if(sim) sim.classList.toggle("active", state.source === "sim");
  if(g)   g.classList.toggle("active", state.source === "real");
}"""
new_source_hl = """function sourceRowHighlight(){
  const g = $("gmailRow");
  if(g) g.classList.toggle("active", true);
}"""
content = content.replace(old_source_hl, new_source_hl)

# 8. Update refreshGmailStatus to automatically load real mailbox when connected and update chip
old_refresh = """async function refreshGmailStatus(){
  try{
    const st = await (await fetch(API + "/api/gmail/status")).json();
    const sim = $("simRow"), g = $("gmailRow");
    const gmailOn = st.token_configured && st.oauth_libraries_available;
    const chip = $("connChip"), txt = $("connTxt");
    if(chip) chip.classList.toggle("live", !!gmailOn);
    if(txt) txt.textContent = gmailOn ? "Gmail linked" : "Simulated";
    g.querySelector("i").style.background = gmailOn ? "var(--ok)" : (st.credentials_configured ? "var(--warn)" : "var(--muted-2)");
    sim.querySelector("i").style.background = "var(--accent)";
    g.querySelector("span") && 0;
    $("gmailSync").hidden = !gmailOn;
    $("gmailConnect").hidden = gmailOn;
    $("gmailNote").textContent = gmailOn
      ? "Real Gmail is linked — use Sync now to pull new mail."
      : st.credentials_configured
        ? "OAuth client configured — finish the connection."
        : "Add a Google OAuth client to enable real Gmail.";
  }catch(_){
    $("gmailNote").textContent = "Gmail status unavailable — running on the simulated inbox.";
    const chip = $("connChip"), txt = $("connTxt");
    if(chip) chip.classList.remove("live");
    if(txt) txt.textContent = "Simulated";
  }
}"""

new_refresh = """async function refreshGmailStatus(){
  try{
    const st = await (await fetch(API + "/api/gmail/status")).json();
    const g = $("gmailRow");
    const gmailOn = st.token_configured && st.oauth_libraries_available;
    const chip = $("connChip"), txt = $("connTxt");
    if(chip) chip.classList.toggle("live", !!gmailOn);
    if(txt) txt.textContent = gmailOn ? (st.account || "Gmail linked") : "Gmail Disconnected";
    if(g && g.querySelector("i")) g.querySelector("i").style.background = gmailOn ? "var(--ok)" : (st.credentials_configured ? "var(--warn)" : "var(--muted-2)");
    $("gmailSync").hidden = !gmailOn;
    $("gmailConnect").hidden = gmailOn;
    $("gmailNote").textContent = gmailOn
      ? "Real Gmail is linked (" + (st.account || "") + ") — use Sync now to pull new mail."
      : st.credentials_configured
        ? "OAuth client configured — finish the connection."
        : "Add a Google OAuth client (google_oauth_client.json) to enable real Gmail.";
    if(gmailOn){
      await loadRealMailbox();
      renderKPIs(); renderRail(); renderList();
      const h = decodeURIComponent(location.hash.slice(1));
      if(h && allItems().some(i => i.id === h)) openMailById(h);
      enrich();
    }
  }catch(_){
    $("gmailNote").textContent = "Gmail status unavailable.";
    const chip = $("connChip"), txt = $("connTxt");
    if(chip) chip.classList.remove("live");
    if(txt) txt.textContent = "Gmail Disconnected";
  }
}

function openGmailSetupModal(){
  const m = $("gmailSetupModal");
  if(!m) return;
  m.hidden = false;
  const fb = $("setupFeedback");
  if(fb){ fb.style.display = "none"; fb.textContent = ""; }
}
function closeGmailSetupModal(){
  const m = $("gmailSetupModal");
  if(m) m.hidden = true;
}
function initGmailSetupHandlers(){
  const setupLink = $("gmailSetupLink");
  if(setupLink) setupLink.addEventListener("click", openGmailSetupModal);
  const setupClose = $("gmailSetupClose");
  if(setupClose) setupClose.addEventListener("click", closeGmailSetupModal);
  const setupModal = $("gmailSetupModal");
  if(setupModal) setupModal.addEventListener("click", e => { if(e.target.id === "gmailSetupModal") closeGmailSetupModal(); });

  const copyBtn = $("copyRedirectBtn");
  if(copyBtn){
    copyBtn.addEventListener("click", () => {
      const uri = "http://127.0.0.1:8000/api/gmail/oauth-callback";
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(uri).then(() => toast("Redirect URI copied!")).catch(() => toast(uri));
      } else {
        toast(uri);
      }
    });
  }

  const dropZone = $("dropZone");
  const fileInput = $("clientFileInput");
  const jsonText = $("clientJsonText");
  const saveBtn = $("saveCredentialsBtn");
  const fb = $("setupFeedback");

  if(dropZone && fileInput){
    dropZone.addEventListener("click", () => fileInput.click());
    dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.style.borderColor = "var(--accent)"; });
    dropZone.addEventListener("dragleave", () => { dropZone.style.borderColor = "var(--border-strong)"; });
    dropZone.addEventListener("drop", e => {
      e.preventDefault();
      dropZone.style.borderColor = "var(--border-strong)";
      if(e.dataTransfer.files && e.dataTransfer.files[0]){
        const file = e.dataTransfer.files[0];
        const reader = new FileReader();
        reader.onload = ev => { if(jsonText) jsonText.value = ev.target.result; };
        reader.readAsText(file);
      }
    });
    fileInput.addEventListener("change", e => {
      if(e.target.files && e.target.files[0]){
        const file = e.target.files[0];
        const reader = new FileReader();
        reader.onload = ev => { if(jsonText) jsonText.value = ev.target.result; };
        reader.readAsText(file);
      }
    });
  }

  if(saveBtn){
    saveBtn.addEventListener("click", async () => {
      const raw = (jsonText ? jsonText.value : "").trim();
      if(!raw){
        if(fb){
          fb.style.display = "block";
          fb.style.background = "var(--bad-bg)";
          fb.style.color = "var(--bad)";
          fb.textContent = "Please select or paste your Google OAuth client JSON.";
        }
        return;
      }
      let parsed;
      try{
        parsed = JSON.parse(raw);
      }catch(e){
        if(fb){
          fb.style.display = "block";
          fb.style.background = "var(--bad-bg)";
          fb.style.color = "var(--bad)";
          fb.textContent = "Invalid JSON syntax. Please check the file contents.";
        }
        return;
      }
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving…";
      try{
        const r = await fetch(API + "/api/gmail/credentials", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(parsed)
        });
        if(!r.ok){
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail || "Failed to save credentials");
        }
        toast("Google OAuth credentials configured!");
        closeGmailSetupModal();
        await refreshGmailStatus();
        await connectGmail();
      }catch(err){
        if(fb){
          fb.style.display = "block";
          fb.style.background = "var(--bad-bg)";
          fb.style.color = "var(--bad)";
          fb.textContent = err.message || "Failed to save credentials";
        }
      }finally{
        saveBtn.disabled = false;
        saveBtn.textContent = "Save & Connect";
      }
    });
  }
}"""

content = content.replace(old_refresh, new_refresh)

# 9. Update connectGmail to open setup modal on 409
old_connect = """async function connectGmail(){
  try{
    const r = await fetch(API + "/api/gmail/connect");
    if(!r.ok){ toast("Gmail is not configured on the server."); return; }
    const { authorization_url } = await r.json();
    window.open(authorization_url, "shipmail-gmail", "width=480,height=640");
    toast("Complete Google sign-in, then use Sync now.");
  }catch(_){ toast("Could not start the Gmail connection."); }
}"""

new_connect = """async function connectGmail(){
  try{
    const r = await fetch(API + "/api/gmail/connect");
    if(!r.ok){
      const err = await r.json().catch(() => ({}));
      if(r.status === 409 || (err.detail && err.detail.toLowerCase().includes("not configured"))){
        openGmailSetupModal();
        return;
      }
      toast(err.detail || "Gmail is not configured on the server.");
      return;
    }
    const { authorization_url } = await r.json();
    window.open(authorization_url, "shipmail-gmail", "width=480,height=640");
    toast("Complete Google sign-in, then use Sync now.");
  }catch(_){ toast("Could not start the Gmail connection."); }
}"""

content = content.replace(old_connect, new_connect)

# 10. Update syncGmail to call loadRealMailbox and enrich
old_sync = """async function syncGmail(){
  toast("Syncing Gmail…");
  try{
    const r = await fetch(API + "/api/gmail/poll", {method:"POST"});
    const d = await r.json();
    toast(`Gmail sync: ${d.processed} processed, ${d.skipped} skipped.`);
    renderKPIs(); renderRail(); renderList();
  }catch(_){ toast("Gmail sync failed."); }
}"""

new_sync = """async function syncGmail(){
  toast("Syncing Gmail…");
  try{
    const r = await fetch(API + "/api/gmail/poll", {method:"POST"});
    if(!r.ok){
      const err = await r.json().catch(() => ({}));
      toast(err.detail || "Gmail sync failed (HTTP " + r.status + ").");
      return;
    }
    const d = await r.json();
    toast(`Gmail sync: ${d.processed} processed, ${d.skipped} skipped.`);
    await loadRealMailbox();
    renderKPIs(); renderRail(); renderList();
    enrich();
  }catch(_){ toast("Gmail sync failed."); }
}"""

content = content.replace(old_sync, new_sync)

# 11. Update boot() to NOT load emails.json / shipments.json (which is the simulated branch!)
old_boot = """async function boot(refresh){
  try{
    const [mail, ships] = await Promise.all([
      fetch(DATA + "emails.json", {cache:refresh ? "reload" : "default"}).then(r => r.json()),
      fetch(DATA + "shipments.json", {cache:refresh ? "reload" : "default"}).then(r => r.json()).catch(() => ({shipments:[]}))
    ]);
    state.emails = (mail.emails || []).map(e => Object.assign(e, {
      starred: e.starred || starOf(e.id),
      unread: !isRead(e.id)
    }));
    state.ships = Object.fromEntries((ships.shipments || []).map(s => [s.shipment_id, s]));
  }catch(_){
    state.emails = []; state.ships = {};
  }
  renderKPIs(); renderRail(); renderList();
  const h = decodeURIComponent(location.hash.slice(1));
  if(h && allItems().some(i => i.id === h)) openMailById(h);
  else { state.activeId = null; renderEmptyDetail(); renderList(); }
  enrich();
  refreshGmailStatus();
  sourceRowHighlight();
}"""

new_boot = """async function boot(refresh){
  state.emails = [];
  state.ships = {};
  renderKPIs(); renderRail(); renderList();
  const h = decodeURIComponent(location.hash.slice(1));
  if(h && allItems().some(i => i.id === h)) openMailById(h);
  else { state.activeId = null; renderEmptyDetail(); renderList(); }
  await refreshGmailStatus();
  sourceRowHighlight();
}"""

content = content.replace(old_boot, new_boot)

# 12. Remove simRow event listeners
old_listeners = """$("gmailConnect").addEventListener("click", connectGmail);
$("gmailSync").addEventListener("click", syncGmail);
$("simRow").addEventListener("click", () => switchSource("sim"));
$("gmailRow").addEventListener("click", () => switchSource("real"));
$("simRow").addEventListener("keydown", e => { if(e.key === "Enter") switchSource("sim"); });
$("gmailRow").addEventListener("keydown", e => { if(e.key === "Enter") switchSource("real"); });"""

new_listeners = """$("gmailConnect").addEventListener("click", connectGmail);
$("gmailSync").addEventListener("click", syncGmail);
$("gmailRow").addEventListener("click", () => refreshGmailStatus());"""

content = content.replace(old_listeners, new_listeners)

# 13. Call initGmailSetupHandlers in self-executing function
old_init = """(function(){
  applyTheme(localStorage.getItem("sdoc-theme") || localStorage.getItem("shipmail-theme") || "dark");
  setRail(localStorage.getItem("sdoc-rail") === "1");
  setFocus(localStorage.getItem("sdoc-focus") === "1");
  renderFocusIcons(); renderShipToggle();
  boot();
})();"""

new_init = """(function(){
  applyTheme(localStorage.getItem("sdoc-theme") || localStorage.getItem("shipmail-theme") || "dark");
  setRail(localStorage.getItem("sdoc-rail") === "1");
  setFocus(localStorage.getItem("sdoc-focus") === "1");
  renderFocusIcons(); renderShipToggle();
  initGmailSetupHandlers();
  boot();
})();"""

content = content.replace(old_init, new_init)

TARGET_PATH.write_text(content, encoding="utf-8")
print(f"Successfully wrote adapted index.html to {TARGET_PATH} (Length: {len(content)})")
