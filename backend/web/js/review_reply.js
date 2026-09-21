/* Shared review, reply and immutable delivery history panel. */
(function(){
  let current = null, ticket = 0;
  function label(delivery){
    if(["SENT", "SENT_SMTP"].includes(delivery)) return "Sent · SMTP accepted (receipt unconfirmed)";
    if(delivery === "SIMULATED") return "Simulated · no email sent";
    if(["FAILED", "ERROR"].includes(delivery)) return "Send failed";
    return "Delivery unconfirmed";
  }
  async function attach(id, expand=false){
    current = id;
    const seq = ++ticket, detail = document.getElementById("detail");
    if(!detail) return;
    document.getElementById("reviewReplyActions")?.remove();
    const panel = document.createElement("section");
    panel.id = "reviewReplyActions"; panel.className = "rr-actions";
    const title = document.createElement("strong"); title.textContent = "Reply & delivery history";
    const summary = document.createElement("p"); summary.textContent = "Loading delivery history…";
    const reply = document.createElement("button"); reply.className = "btn primary"; reply.textContent = "Prepare reply";
    reply.onclick = () => window.Outstream.openReturn(id);
    const history = document.createElement("details");history.className = "rr-history";history.open = expand;
    const heading = document.createElement("summary");heading.textContent = "Sending history";history.append(heading);
    const policyLabel=document.createElement("label");policyLabel.textContent=" Reply policy for this email: ";
    const policy=document.createElement("select");policy.setAttribute("aria-label","Reply policy for this email");
    [["INHERIT","Use source / global policy"],["MANUAL","Manual · approval required"],["AUTO","Auto"]].forEach(([value,text])=>{const opt=document.createElement("option");opt.value=value;opt.textContent=text;policy.append(opt);});
    policy.disabled=true;policyLabel.append(policy);
    panel.append(title, summary, policyLabel, reply, history); detail.append(panel);
    fetch("/api/emails?limit=2000").then(r=>r.json()).then(data=>{
      if(seq!==ticket) return;
      const row=(data.items||[]).find(x=>x.email_id===id);
      if(row){policy.value=row.disposition_override||"INHERIT";policy.disabled=false;}
    }).catch(()=>{});
    policy.onchange=async()=>{
      policy.disabled=true;
      await window.Outstream.setItemDisposition(id,policy.value);
      policy.disabled=false;
    };
    const approvals=document.createElement("details");approvals.className="rr-history";
    const approvalHeading=document.createElement("summary");approvalHeading.textContent="Reply approval history";approvals.append(approvalHeading);panel.append(approvals);
    fetch(`/api/emails/${encodeURIComponent(id)}/reply-approvals`).then(async r=>{
      if(!r.ok) throw new Error();return r.json();
    }).then(rows=>{
      if(seq!==ticket)return;
      approvalHeading.textContent=`Reply approval history (${rows.length})`;
      rows.forEach(row=>{
        const item=document.createElement("article"),meta=document.createElement("p"),body=document.createElement("pre");
        meta.textContent=`${row.status} · ${row.reviewer} · ${row.created_at} · To: ${row.recipient}`;
        body.textContent=row.subject+"\n\n"+row.body;
        item.append(meta,body);approvals.append(item);
      });
    }).catch(()=>{approvalHeading.textContent="Approval history unavailable · backend update may require restart";});
    try{
      const response = await fetch(`/api/emails/${encodeURIComponent(id)}/dispatch-history`);
      if(!response.ok) throw new Error("Unable to load history");
      const rows = await response.json();
      if(seq !== ticket || current !== id) return;
      summary.textContent = rows.length ? label(rows[0].delivery) + " · Sending a reply does not close the shipment." : "No reply has been sent. Review the evidence before preparing a response.";
      heading.textContent = `Sending history (${rows.length})`;
      if(rows.length) reply.textContent = ["FAILED", "ERROR"].includes(rows[0].delivery) ? "Review and retry" : "Prepare another reply";
      rows.forEach(row => {
        const item = document.createElement("article");
        const h = document.createElement("strong");h.textContent = label(row.delivery);
        const meta = document.createElement("p");meta.textContent = `${row.created_at || "Time unavailable"} · To: ${row.recipient || "Unknown"} · Via: ${row.channel || "Unknown"}`;
        const subject = document.createElement("p");subject.textContent = row.subject || "(no subject)";
        const body = document.createElement("pre");body.textContent = row.body || "No stored body available.";
        item.append(h,meta,subject,body);
        if(row.error){const err=document.createElement("p");err.style.color="var(--bad)";err.textContent="Failure reason: "+row.error;item.append(err);}
        if(row.message_id){const msg=document.createElement("small");msg.textContent="Message ID: "+row.message_id;item.append(msg);}
        history.append(item);
      });
    }catch(e){if(seq === ticket) summary.textContent = "Delivery history is unavailable. Refresh before sending to avoid duplicate replies.";}
  }
  async function inspect(id, history=false){
    switchView("review");
    await openEmail(id);
    await new Promise(resolve => setTimeout(resolve, 0));
    await attach(id,history);
  }
  window.ReviewReply={attach,inspect,refresh:()=>current && attach(current)};
})();
