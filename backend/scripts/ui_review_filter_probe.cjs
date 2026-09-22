/* jsdom probe for the Review view after the filters moved client-side.
 * Boots the real index.html against the live backend and checks:
 *   - the free-text filter narrows the list without hitting the API again
 *   - the enum dropdown (status) filters locally
 *   - the new mailbox-source select is populated from real data
 *   - clearFilters restores the full list
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
  url: BASE + "/ui/#review",
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

// count every call to the emails endpoint so we can prove filtering is local
let emailCalls = 0;
window.fetch = function(input, init){
  const url = typeof input === "string" ? input : (input && input.url) || "";
  if(url.indexOf("/api/emails") !== -1) emailCalls++;
  const abs = url.startsWith("http") ? url : BASE + url;
  return fetch(abs, init);
};

const inline = [...window.document.querySelectorAll("script:not([src])")];
for(const s of inline){ window.eval(s.textContent); }
for(const f of ["review.js", "gateway.js", "lifecycle.js", "outstream.js", "review_reply.js"]){
  window.eval(fs.readFileSync(path.join(ROOT, "js", f), "utf8"));
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const rows = () => [...window.document.querySelectorAll("#list .row")];
const rowCount = () => rows().length;

async function waitFor(pred, timeout = 8000){
  const started = Date.now();
  while(Date.now() - started < timeout){
    if(pred()) return true;
    await sleep(120);
  }
  return false;
}

(async () => {
  try{
    const probe = await window.fetch("/api/emails?limit=1");
    check("backend reachable", probe.ok, "status " + probe.status);

    window.switchView("review");
    const loaded = await waitFor(() => rowCount() > 0);
    check("review list renders", loaded, "rows=" + rowCount());
    const total = rowCount();

    // 0b. classification-source badge renders on rows that have a report
    const apiData = await (await window.fetch("/api/emails?limit=2000")).json();
    const withSrc = (apiData.items || []).filter(r => r.classify_source).length;
    const badges = [...window.document.querySelectorAll("#list .row .badge")]
      .filter(b => /^(RULE|LLM|OCR)$/.test(b.textContent.trim())).length;
    check("classify-source badge rendered", withSrc > 0 && badges >= withSrc,
      `api=${withSrc} badges=${badges}`);

    const callsAfterLoad = emailCalls;

    // 1. source dropdown populated from real data
    const srcOpts = [...window.document.querySelectorAll("#f-src option")];
    check("mailbox source select populated", srcOpts.length > 1, "options=" + srcOpts.length);

    // 2. free-text filter narrows locally, no extra request
    const firstSubject = rows()[0].querySelector(".sbj").textContent.trim();
    const token = firstSubject.split(/\s+/).filter(w => w.length > 4)[0] || firstSubject.slice(0, 8);
    const fq = window.document.getElementById("f-q");
    fq.value = token.toLowerCase();
    window.applyReviewFilter();
    await sleep(200);
    check("free-text filter narrows list", rowCount() > 0 && rowCount() < total, `"${token}" ${total} -> ${rowCount()}`);
    check("free-text filter issues no request", emailCalls === callsAfterLoad, `calls ${callsAfterLoad} -> ${emailCalls}`);
    const clearBtn = window.document.getElementById("f-q-clear");
    check("clear button revealed while filtering", clearBtn && !clearBtn.hidden);

    // 3. enum dropdown (status = MISMATCH) filters locally
    window.clearFilters();
    await sleep(150);
    check("clearFilters restores full list", rowCount() === total, `${rowCount()} vs ${total}`);

    const statusSel = window.document.getElementById("f-status");
    statusSel.value = "MISMATCH";
    window.applyReviewFilter();
    await sleep(200);
    const mismatchRows = rows();
    const allMismatch = mismatchRows.every(r => {
      const b = r.querySelector(".badge, .pill, .st");
      return (r.textContent || "").indexOf("MISMATCH") !== -1;
    });
    check("status dropdown filters to MISMATCH", mismatchRows.length > 0 && allMismatch,
      "rows=" + mismatchRows.length + " allMismatch=" + allMismatch);
    check("dropdown filter issues no request", emailCalls === callsAfterLoad, `calls ${callsAfterLoad} -> ${emailCalls}`);

    // 4. source filter works
    statusSel.value = "";
    const src = window.document.getElementById("f-src");
    src.value = srcOpts[1] ? srcOpts[1].value : "";
    window.applyReviewFilter();
    await sleep(200);
    check("source filter narrows list", rowCount() > 0 && rowCount() <= total, `rows=${rowCount()} of ${total}`);

    // 5. needs-my-action strip reflects real counts and jumps
    const chips = ["needsTriage","needsReview","needsReply","needsFailed"];
    const missing = chips.filter(id => !window.document.getElementById(id));
    check("needs strip chips exist", missing.length === 0, "missing=" + missing.join(","));
    const readChip = id => {
      const b = window.document.querySelector("#" + id + " b");
      return b ? b.textContent.trim() : null;
    };
    const filled = await waitFor(() => chips.every(id => /^\d+$/.test(readChip(id) || "")), 10000);
    check("needs counts populated", filled, chips.map(id => id + "=" + readChip(id)).join(" "));

    window.jumpToNeeds("review");
    await sleep(400);
    const stAfter = window.document.getElementById("f-status");
    check("jump to defect review filters the list", stAfter && stAfter.value === "NEEDS_REVIEW",
      "status=" + (stAfter && stAfter.value));
    check("jump switched the view", window.document.querySelector("#viewReview").hidden === false);
    // reply / failed are queues inside the unified Review console, not hashes
    window.jumpToNeeds("failed");
    await sleep(400);
    const replySel = window.document.getElementById("f-reply");
    check("jump to failed sends selects the failed queue",
      replySel && replySel.value === "failed" && window.document.querySelector("#viewReview").hidden === false,
      "f-reply=" + (replySel && replySel.value));

    window.jumpToNeeds("reply");
    await sleep(400);
    check("jump to pending replies selects the awaiting queue",
      replySel && replySel.value === "awaiting", "f-reply=" + (replySel && replySel.value));

    window.clearFilters();
    await sleep(150);
    check("no unhandled errors", errors.length === 0, errors.join(" ; "));
  }catch(err){
    failures++;
    console.log("FAIL probe crashed :: " + (err && err.message));
  }

  console.log(failures === 0 ? "PROBE OK" : ("PROBE FAILED: " + failures));
  process.exit(failures === 0 ? 0 : 1);
})();
