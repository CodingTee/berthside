/* jsdom probe for the reply-draft feature (Task 56).
 * Boots the real index.html against the live backend and checks:
 *   - editing the reply modal stores a draft in localStorage
 *   - the unsent draft survives closing and reopening the modal
 *   - the draft banner appears and "Discard draft" restores the template
 *   - a draft leaves a marker on the row's Reply button
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

window.fetch = function(input, init){
  const url = typeof input === "string" ? input : (input && input.url) || "";
  const abs = url.startsWith("http") ? url : BASE + url;
  return fetch(abs, init);
};

const inline = [...window.document.querySelectorAll("script:not([src])")];
for(const s of inline){ window.eval(s.textContent); }
for(const f of ["review.js", "gateway.js", "lifecycle.js", "outstream.js", "review_reply.js"]){
  window.eval(fs.readFileSync(path.join(ROOT, "js", f), "utf8"));
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const val = id => (window.document.getElementById(id) || {}).value || "";
const draftKey = id => "sdoc-reply-draft:" + id;

async function waitFor(pred, timeout = 8000){
  const started = Date.now();
  while(Date.now() - started < timeout){
    if(pred()) return true;
    await sleep(120);
  }
  return false;
}

/* jsdom does not run attribute handlers under outside-only, so the edit is
 * simulated by setting the value and calling the handler directly, exactly as
 * the oninput attribute would. The attributes themselves are asserted below. */
function type(el, text){
  el.value = text;
  window.Outstream.noteDraftEdit();
}

(async () => {
  try{
    const probe = await window.fetch("/api/emails?limit=1");
    check("backend reachable", probe.ok, "status " + probe.status);

    // pick the first email that the Outstream list actually renders
    await window.Outstream.reload();
    await sleep(300);
    const firstRow = window.document.querySelector("#outList tr");
    const meta = firstRow && firstRow.querySelectorAll("td > div")[1]
      ? firstRow.querySelectorAll("td > div")[1].textContent.trim() : "";
    const id = meta.split("·")[0].trim();
    check("email row available", !!id, "meta=" + meta);
    window.localStorage.removeItem(draftKey(id));

    // 1. open the reply modal -> pristine template
    window.Outstream.openReturn(id);
    const opened = await waitFor(() => val("outRetSub") !== "" || val("outRetBody") !== "");
    check("reply modal prepares a template", opened,
      "sub=" + JSON.stringify(val("outRetSub").slice(0, 40)));
    const templateBody = val("outRetBody");
    check("template body is non-empty", templateBody.length > 0, "len=" + templateBody.length);

    const note = window.document.getElementById("outRetDraftNote");
    check("draft note starts hidden", note && note.style.display === "none",
      "display=" + (note && note.style.display));
    // jsdom never fires attribute handlers; make sure the wiring is still there
    const subAttr = (window.document.getElementById("outRetSub") || {}).getAttribute
      ? window.document.getElementById("outRetSub").getAttribute("oninput") : null;
    const bodyAttr = (window.document.getElementById("outRetBody") || {}).getAttribute
      ? window.document.getElementById("outRetBody").getAttribute("oninput") : null;
    check("inputs wired to noteDraftEdit",
      subAttr === "Outstream.noteDraftEdit()" && bodyAttr === "Outstream.noteDraftEdit()",
      `subject=${subAttr} body=${bodyAttr}`);

    // 2. edit -> draft written to localStorage
    const drafted = "DRAFT PROBE LINE " + Date.now();
    type(window.document.getElementById("outRetBody"), templateBody + "\n" + drafted);
    await sleep(150);
    const stored = window.localStorage.getItem(draftKey(id));
    check("typing stores a draft", !!stored && stored.indexOf(drafted) !== -1,
      "raw=" + String(stored).slice(0, 60));
    check("draft note becomes visible", note && note.style.display === "flex",
      "display=" + (note && note.style.display));

    // 3. the row's Reply button is marked while a draft is pending
    await window.Outstream.reload();
    await sleep(250);
    const replyBtn = [...window.document.querySelectorAll("#outList button")]
      .find(b => (b.getAttribute("onclick") || "").indexOf("openReturn") !== -1 &&
                 (b.getAttribute("onclick") || "").indexOf(id) !== -1);
    check("row shows a draft marker", replyBtn && replyBtn.textContent.indexOf("✎") === 0,
      "label=" + (replyBtn ? JSON.stringify(replyBtn.textContent) : "button missing"));

    // 4. close and reopen -> draft wins over the template
    window.Outstream.closeReturnModal();
    await sleep(120);
    check("modal closed", window.document.getElementById("outReturnModal").classList.contains("hidden"));
    window.Outstream.openReturn(id);
    // wait for the reopen to finish (template first, draft override right after)
    await waitFor(() => val("outRetBody") !== "" && val("outRetBody").indexOf(drafted) !== -1, 10000);
    await sleep(500);
    const restored = val("outRetBody").indexOf(drafted) !== -1;
    check("draft survives closing the modal", restored,
      "contains draft line=" + (val("outRetBody").indexOf(drafted) !== -1));
    check("draft note visible on reopen", note && note.style.display === "flex",
      "display=" + (note && note.style.display));

    // 5. discard -> back to the pristine template, storage cleared
    window.Outstream.discardDraft();
    const reset = await waitFor(() => val("outRetBody") === templateBody, 10000);
    check("discard restores the template", reset,
      "same=" + (val("outRetBody") === templateBody) + " len=" + val("outRetBody").length);
    check("discard clears storage", window.localStorage.getItem(draftKey(id)) === null,
      "raw=" + String(window.localStorage.getItem(draftKey(id))).slice(0, 40));
    check("draft note hidden after discard", note && note.style.display === "none",
      "display=" + (note && note.style.display));

    window.Outstream.closeReturnModal();
    check("no unhandled errors", errors.length === 0, errors.join(" ; "));
  }catch(err){
    failures++;
    console.log("FAIL probe crashed :: " + (err && err.message));
  }

  console.log(failures === 0 ? "PROBE OK" : ("PROBE FAILED: " + failures));
  process.exit(failures === 0 ? 0 : 1);
})();
