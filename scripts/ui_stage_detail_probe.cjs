/* Focused jsdom probe: gateway inbound stream compact rows + click-to-open detail modal.
 * Only exercises the gateway view so concurrent edits to other views do not interfere.
 */
const fs = require("fs");
const path = require("path");
let JSDOM;
try{ JSDOM = require("jsdom").JSDOM; }
catch(e){ JSDOM = require("C:/Users/gayso/.workbuddy/binaries/node/workspace/node_modules/jsdom").JSDOM; }

const ROOT = path.join(__dirname, "..", "web");
const BASE = "http://127.0.0.1:8000";

const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const dom = new JSDOM(html, { url: BASE + "/ui/", runScripts: "outside-only", pretendToBeVisual: true });
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
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

(async () => {
  try{
    window.switchView("gateway");
    // poll until staged rows render (async fetch)
    let rows = [];
    for(let i = 0; i < 40; i++){
      rows = [...window.document.querySelectorAll("#stagedList tr.stg-row")];
      if(rows.length) break;
      await sleep(150);
    }
    check("compact staged rows render", rows.length > 0, "rows=" + rows.length);

    const first = rows[0];
    check("row has 4 cells", first.querySelectorAll("td").length === 4, "cells=" + first.querySelectorAll("td").length);
    check("subject cell has one-line subject + meta",
      !!first.querySelector(".stg-subj") && !!first.querySelector(".stg-meta"));
    check("verdict cell has security + category + status chips",
      !!first.querySelector(".stg-verdict .pill") && !!first.querySelector(".stg-verdict .stg-status"));
    check("old 7-col attachments cell gone",
      !/📎/.test(first.textContent), first.textContent.slice(0, 80));

    const modal = window.document.getElementById("stageDetailModal");
    check("detail modal exists and starts hidden", !!modal && modal.classList.contains("hidden"));

    const stageId = first.getAttribute("data-id");
    window.Gateway.openStageDetail(stageId);
    check("detail modal opens on row click api", modal && !modal.classList.contains("hidden"));
    check("modal subject filled", /\S/.test((window.document.getElementById("sdSubject")||{}).textContent || ""));
    check("modal stage id filled", (window.document.getElementById("sdStage")||{}).textContent === stageId,
      (window.document.getElementById("sdStage")||{}).textContent);
    check("modal actions rendered", window.document.querySelectorAll("#sdActions .btn, #sdActions span").length > 0);
    window.Gateway.closeStageDetail();
    check("detail modal closes", modal && modal.classList.contains("hidden"));

    check("no unhandled errors", errors.length === 0, errors.join(" ; "));
  }catch(e){
    failures++;
    console.log("FAIL exception :: " + (e && e.stack || e));
  }
  console.log(failures === 0 ? "PROBE OK" : "PROBE FAILED: " + failures);
  process.exit(failures === 0 ? 0 : 1);
})();
