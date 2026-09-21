/* jsdom smoke for the Gateway view after the trust-matrix modal rework.
 * Boots the real index.html, evals the inline block plus the five modules,
 * proxies window.fetch to the local backend, then checks:
 *   - distribution bar segments get flexGrow from /api/v1/gateway/status
 *   - kebab summary text updates
 *   - kebab opens the mxModal, matrix rows render, close hides it again
 *   - channel chips exist in the control bar
 *   - no unhandledrejection / window.onerror
 */
const fs = require("fs");
const path = require("path");
let JSDOM;
try{ JSDOM = require("jsdom").JSDOM; }
catch(e){ JSDOM = require("C:/Users/gayso/.workbuddy/binaries/node/workspace/node_modules/jsdom").JSDOM; }

const ROOT = path.join(__dirname, "..", "web");
const BASE = "http://127.0.0.1:8000";

const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const dom = new JSDOM(html, {
  url: BASE + "/ui/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;

let failures = 0;
const errors = [];
window.addEventListener("unhandledrejection", e => errors.push("unhandledrejection: " + (e.reason && e.reason.message || e.reason)));
window.onerror = (msg) => errors.push("onerror: " + msg);

function check(name, cond, extra){
  if(cond){ console.log("PASS " + name); }
  else { failures++; console.log("FAIL " + name + (extra ? " :: " + extra : "")); }
}

// jsdom (outside-only) has no fetch: bridge to Node's global fetch, prefixed to the backend
window.fetch = function(input, init){
  const url = typeof input === "string" ? input : (input && input.url) || "";
  const abs = url.startsWith("http") ? url : BASE + url;
  return fetch(abs, init);
};

// eval inline scripts in document order, then the module files
const inline = [...window.document.querySelectorAll("script:not([src])")];
for(const s of inline){ window.eval(s.textContent); }
for(const f of ["review.js", "gateway.js", "lifecycle.js", "outstream.js", "review_reply.js"]){
  window.eval(fs.readFileSync(path.join(ROOT, "js", f), "utf8"));
}

function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

(async () => {
  try{
    // sanity: backend must be reachable
    const probe = await window.fetch("/api/v1/gateway/status");
    check("backend reachable", probe.ok, "status " + probe.status);

    // switch to gateway like a user would
    window.switchView("gateway");
    await sleep(700);   // let loadStatus/loadStagedEmails settle

    // 1. KPI ids still exist (legend) and dist bar segments present
    const segIds = ["distSegSafe","distSegPending","distSegSpam","distSegBlocked"];
    const missingSegs = segIds.filter(id => !window.document.getElementById(id));
    check("dist bar segments present", missingSegs.length === 0, missingSegs.join(","));
    check("kpi legend ids present", ["kpiTotal","kpiSafe","kpiBlocked","kpiSpam","kpiPending"]
      .every(id => !!window.document.getElementById(id)));

    const st = await (await window.fetch("/api/v1/gateway/status")).json();
    const expect = {
      distSegSafe: st.statistics.safe_ingested,
      distSegPending: st.statistics.pending_approval,
      distSegSpam: st.statistics.quarantined_spam,
      distSegBlocked: st.statistics.blocked_malware,
    };
    let segOk = true, segDetail = [];
    for(const [id, v] of Object.entries(expect)){
      const el = window.document.getElementById(id);
      const got = el && el.style.flexGrow;
      if(String(v) !== String(got)){ segOk = false; segDetail.push(id + " want " + v + " got " + got); }
    }
    check("dist bar flexGrow matches status", segOk, segDetail.join(" | "));

    // 2. kebab summary updated by updateMatrixSummary
    const kebab = window.document.getElementById("mxKebabSum");
    check("kebab summary populated", !!kebab && /\d+ src/.test(kebab.textContent || ""), kebab && kebab.textContent);

    // 3. kebab opens modal, rows render inside, close hides
    const modal = window.document.getElementById("mxModal");
    check("mxModal exists and starts hidden", !!modal && modal.classList.contains("hidden"));
    window.Gateway.openMatrixModal();
    check("mxModal opens", modal && !modal.classList.contains("hidden"));
    await sleep(700);
    const rows = window.document.querySelectorAll("#matrixGrid tr");
    check("matrix rows render in modal", rows.length > 0, "rows=" + rows.length);
    check("mxSummary populated", /\d+/.test((window.document.getElementById("mxSummary")||{}).textContent || ""));
    window.Gateway.closeMatrixModal();
    check("mxModal closes", modal && modal.classList.contains("hidden"));

    // 4. channel chips live in the control bar, not a separate bar
    const imap = window.document.getElementById("imapChannelBadge");
    const smtp = window.document.getElementById("smtpChannelBadge");
    check("channel badges present", !!imap && !!smtp);
    check("old channelBar removed", !window.document.getElementById("channelBar"));

    // 5. legacy inline-matrix markup really gone from the live DOM
    check("no inline mxBody", !window.document.getElementById("mxBody"));

    // 6. inbound table still renders (main work area untouched)
    await sleep(500);
    const staged = window.document.querySelectorAll("#stagedList tr");
    check("stagedList renders", staged.length > 0, "rows=" + staged.length);

    // 7. Review view: KPI stack is now one distribution panel
    window.switchView("review");
    await sleep(900);
    const revPanel = window.document.querySelector("#kpis .dist-panel");
    check("review dist panel renders", !!revPanel);
    const revLegend = /Emails processed/.test((revPanel||{}).textContent||"");
    check("review legend present", revLegend);
    check("review updatedAt filled", /Updated/.test((window.document.getElementById("updatedAt")||{}).textContent||""));

    // 8. Reply (outstream) view: dist bar + kebab modal around disposition matrix
    window.switchView("outstream");
    await sleep(900);
    const outPanel = window.document.querySelector("#viewOutstream .dist-panel");
    check("outstream dist panel renders", !!outPanel);
    const segIds2 = ["outSegAwaiting","outSegSent","outSegReview","outSegOther"];
    check("outstream dist segments present", segIds2.every(id => !!window.document.getElementById(id)));
    const outKebab = window.document.getElementById("outKebabSum");
    check("outstream kebab summary populated", !!outKebab && /\d+ src/.test(outKebab.textContent || ""), outKebab && outKebab.textContent);
    const outModal = window.document.getElementById("outMatrixModal");
    check("outMatrixModal exists and starts hidden", !!outModal && outModal.classList.contains("hidden"));
    window.Outstream.openMatrixModal();
    check("outMatrixModal opens", outModal && !outModal.classList.contains("hidden"));
    const cards = window.document.querySelectorAll("#outMatrixGrid .mx-card");
    check("disposition cards render in modal", cards.length > 0, "cards=" + cards.length);
    window.Outstream.closeMatrixModal();
    check("outMatrixModal closes", outModal && outModal.classList.contains("hidden"));
    check("old details reply-settings removed", !window.document.querySelector("#viewOutstream details.matrix-panel"));

    check("no unhandled errors", errors.length === 0, errors.join(" ; "));
  }catch(e){
    failures++;
    console.log("FAIL exception :: " + (e && e.stack || e));
  }
  console.log(failures === 0 ? "SMOKE OK" : "SMOKE FAILED: " + failures);
  process.exit(failures === 0 ? 0 : 1);
})();
