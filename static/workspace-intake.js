/* Correspondence is evidence first. Rendering never files or runs a model. */
"use strict";
window.ViraIntake = (() => {
  const $ = (s) => document.querySelector(s);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag); n.className = cls || "";
    if (text != null) n.textContent = text;
    return n;
  };
  let snapshot = null, pending = null, liveCount = 0, filter = "all", configEditing = false;
  let dialog = null, returnFocus = null;
  const reviewStates = new Set(["review", "error"]);
  const savedStates = new Set(["saved", "processing", "processed"]);
  const needsReview = (item) => reviewStates.has(item.state) || ["review", "error", "disabled"].includes(item.task_status);
  const humanState = (item) => item.state === "processed" && !item.receipt
    ? ({recorded:"Reminder linked",disabled:"Assistant paused",review:"Reminder needs review",error:"Reminder handoff failed"}[item.task_status] || "Waiting for reminder extraction") : ({review:"Choose a destination",queued:"Waiting to classify",
    saving:"Saving",saved:"Saved to vault",processing:"Saved · processing",processed:"Saved and indexed",
    error:"Needs another look",ignored:"Dismissed"}[item.state] || item.state || "Waiting");
  function button(label, run, cls = "fchip") {
    const b = node("button", cls, label); b.type = "button"; b.addEventListener("click", run); return b;
  }
  async function request(path, body, method = "POST") {
    const resp = await fetch(path, body === undefined ? {} : {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const data = await resp.json();
    if (!resp.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.error || "Request failed");
    return data;
  }
  function error(parent, e) {
    parent.querySelector(".intake-error")?.remove();
    const p = node("p", "intake-error", e.message || String(e)); p.setAttribute("role", "alert"); parent.appendChild(p);
  }
  function selectField(parent, label, choices, selected) {
    const wrap = node("label", "intake-field", label), sel = node("select");
    choices.forEach(([value, title]) => { const o = node("option", "", title); o.value = value; sel.appendChild(o); });
    sel.value = selected || ""; wrap.appendChild(sel); parent.appendChild(wrap); return sel;
  }
  function textField(parent, label, value = "", placeholder = "") {
    const wrap = node("label", "intake-field", label), input = node("input"); input.value = value; input.placeholder = placeholder;
    wrap.appendChild(input); parent.appendChild(wrap); return input;
  }
  function destinations() {
    const values = snapshot?.destinations;
    const all = Array.isArray(values) ? values : values?.destinations || [];
    return [["", "Choose a vault"], ...all.filter((v) => v.write_enabled === true && v.connected !== false).map((v) => [v.id, v.name || v.label || v.id])];
  }
  function destinationName(id) {
    const values = snapshot?.destinations;
    const all = Array.isArray(values) ? values : values?.destinations || [];
    return all.find((v) => v.id === id)?.name || id;
  }
  function countBadge() {
    const n = (snapshot?.items || []).filter((x) => needsReview(x)).length + liveCount;
    const b = $("#attention-review-badge"); if (b) { b.textContent = n ? String(n) : ""; b.hidden = !n; }
  }
  function row(item) {
    const b = button("", () => open(item.id), "intake-row");
    const origin = node("span", "intake-origin", item.channel === "email" ? "Mail" : "Text");
    const copy = node("span", "intake-row-copy");
    copy.appendChild(node("strong", "", item.subject || item.title || item.preview || "Untitled message"));
    copy.appendChild(node("span", "intake-meta", [item.person_name || item.account || "Message", destinationName(item.destination),
      item.folder].filter(Boolean).join(" · ")));
    if (item.reason) copy.appendChild(node("span", "intake-reason", item.reason));
    b.append(origin, copy, node("span", "intake-state", humanState(item))); return b;
  }
  function list(parent, items, empty) {
    if (!items.length) parent.appendChild(node("p", "workspace-empty", empty));
    else items.forEach((item) => parent.appendChild(row(item)));
  }
  function renderReview() {
    const host = $("#correspondence-review"); if (!host || !snapshot) return;
    host.replaceChildren();
    const items = snapshot.items.filter((x) => needsReview(x));
    host.hidden = !items.length;
    host.appendChild(node("h3", "workspace-section-title", "Where should these go?"));
    list(host, items, "No filing decisions waiting."); countBadge();
  }
  function renderInbox() {
    const host = $("#correspondence-inbox"); if (!host || !snapshot || configEditing) return;
    const configOpen = host.querySelector(".intake-config")?.open;
    host.replaceChildren();
    const tools = node("div", "workspace-toolbar");
    tools.appendChild(node("span", "intake-status", snapshot.enabled ? "Filing enabled" : "Filing is paused"));
    tools.appendChild(button("Open incoming messages", () => openApp("feed")));
    tools.appendChild(button("Refresh", load)); host.appendChild(tools);
    if (snapshot.read_only) host.appendChild(node("p", "hint", "This sample-data environment does not process incoming messages or write to vaults."));
    if (snapshot.last_error) error(host, snapshot.last_error);
    const settings = node("details", "intake-config"); settings.open = !!configOpen;
    settings.appendChild(node("summary", "", "Filing preferences and destinations"));
    settings.addEventListener("toggle", () => { if (settings.open && !settings.querySelector("form")) configForm(settings); });
    host.appendChild(settings);
    if (configOpen) configForm(settings);
    const bar = node("div", "workspace-toolbar intake-filters");
    [["all","Recent messages"],["review","Needs review"],["saved","Saved"]].forEach(([key,label]) => {
      const b = button(label, () => { filter = key; renderInbox(); }); b.classList.toggle("on", filter === key);
      b.setAttribute("aria-pressed", String(filter === key)); bar.appendChild(b);
    }); host.appendChild(bar);
    const items = snapshot.items.filter((x) => filter === "all" || (filter === "review" ? needsReview(x) : savedStates.has(x.state) && x.receipt));
    list(host, items, filter === "review" ? "Nothing needs a filing decision." : filter === "saved" ? "Saved messages will appear here with a link to their source." : "No correspondence has been captured yet. Add a filing route, then enable intake to start with recent messages.");
    const coverage = node("details", "intake-coverage"); coverage.appendChild(node("summary", "", "Source coverage"));
    const values = snapshot.coverage;
    if (typeof values === "string") coverage.appendChild(node("p", "hint", values));
    else Object.entries(values || {}).forEach(([key,value]) => coverage.appendChild(node("p", "hint", key.replace(/_/g," ") + ": " + (typeof value === "object" ? JSON.stringify(value) : value))));
    host.appendChild(coverage);
  }
  async function configForm(host) {
    if (host.dataset.loading) return; host.dataset.loading = "1";
    try {
      const cfg = await request("/api/correspondence/config");
      if (!host.isConnected) return;
      const form = node("form", "intake-settings");
      form.addEventListener("input", () => { configEditing = true; });
      form.addEventListener("change", () => { configEditing = true; });
      const enabledWrap = node("label", "intake-check"), enabled = node("input"); enabled.type="checkbox"; enabled.checked=cfg.enabled;
      enabledWrap.append(enabled,node("span","","Read new messages and suggest filing")); form.appendChild(enabledWrap);
      const modelWrap=node("label","intake-check"), model=node("input");model.type="checkbox";model.checked=!!cfg.model_classification;
      modelWrap.append(model,node("span","","Allow connected AI to classify message bodies when rules do not match"));form.appendChild(modelWrap);
      form.appendChild(node("p","hint","AI classification sends source text to your configured model provider. With this off, rules stay local and uncertain messages wait for you."));
      form.appendChild(node("p", "hint", "Routes choose a vault and a category. Automatic filing applies only to an unambiguous match. Other messages wait in Review. Sources keep their own model permissions."));
      const routeHost=node("div","intake-routes"), edits=[]; form.appendChild(routeHost);
      function routeEditor(route={}) {
        const box=node("fieldset","intake-route"); box.appendChild(node("legend","","Filing route"));
        const dest=selectField(box,"Vault",destinations(),route.destination);
        const folder=textField(box,"Category",route.folder,"e.g. inbox/Finances");
        const sender=textField(box,"Sender",route.sender,"Exact sender, optional");
        const terms=textField(box,"All these words",(route.terms || []).join(", "),"budget, planning");
        const purpose=textField(box,"What belongs here",route.purpose,"Describe this route for classification");
        const account=textField(box,"Account",route.account,"Any account");
        const aw=node("label","intake-check"),automatic=node("input"); automatic.type="checkbox";automatic.checked=!!route.automatic;
        aw.append(automatic,node("span","","File clear matches automatically"));box.appendChild(aw);
        const edit={route,box,dest,folder,sender,terms,purpose,account,automatic};edits.push(edit);
        box.appendChild(button("Remove route",()=>{box.remove();configEditing=true;}));routeHost.appendChild(box);
      }
      (cfg.routes || []).forEach(routeEditor);
      form.appendChild(button("Manage vault connections",()=>{openApp("setup");if(typeof dashJump==="function")dashJump("brain");}));
      form.appendChild(button("Add a route",()=>{routeEditor();configEditing=true;}));
      const foot=node("div","workspace-toolbar"),save=node("button","btn small","Save preferences");save.type="submit";save.disabled=!!snapshot?.read_only;
      foot.append(save,button("Cancel",()=>{configEditing=false;renderInbox();}));form.appendChild(foot);
      form.addEventListener("submit",async(e)=>{
        e.preventDefault();save.disabled=true;
        try{
          const routes=edits.filter((x)=>x.box.isConnected).map((x,i)=>({...x.route,id:x.route.id || "route-"+Date.now()+"-"+i,
            destination:x.dest.value,folder:x.folder.value.trim(),sender:x.sender.value.trim(),purpose:x.purpose.value.trim(),account:x.account.value.trim(),
            terms:x.terms.value.split(",").map((t)=>t.trim()).filter(Boolean),automatic:x.automatic.checked}));
          await request("/api/correspondence/config",{...cfg,enabled:enabled.checked,model_classification:model.checked,routes},"PATCH");configEditing=false;await load();
        }catch(e){error(form,e);}finally{save.disabled=!!snapshot?.read_only;}
      });host.appendChild(form);
    }catch(e){error(host,e);}finally{delete host.dataset.loading;}
  }
  function close(){if(dialog){dialog.close();dialog.remove();dialog=null;returnFocus?.focus?.();}}
  async function open(id) {
    if (!snapshot) await load();
    close();returnFocus=document.activeElement;
    const current=node("dialog","workspace-inspector");dialog=current;
    const head=node("header","workspace-inspector-head");head.append(node("span","workspace-eyebrow","Message & destination"),button("Close",close));current.appendChild(head);
    const body=node("div","workspace-inspector-body");body.appendChild(node("p","hint","Reading source context…"));current.appendChild(body);
    current.addEventListener("cancel",(e)=>{e.preventDefault();close();});document.body.appendChild(current);current.showModal();
    try{
      const response=await request("/api/correspondence/"+encodeURIComponent(id)); const item=response.item || response;
      if(dialog!==current)return;body.replaceChildren();
      body.append(node("h2","",item.subject || item.title || "Message"),node("p","intake-meta",[item.person_name,item.channel,item.account,item.when].filter(Boolean).join(" · ")));
      body.appendChild(node("p","intake-state",humanState(item)));
      if(item.reason)body.appendChild(node("p","",item.reason));
      const source=node("details","intake-source");source.open=true;source.append(node("summary","","Original source"),node("pre","",item.text || item.preview || "No retained text available."));body.appendChild(source);
      if(item.limitations?.length){const limits=node("ul","intake-limitations");item.limitations.forEach((x)=>limits.appendChild(node("li","",typeof x==="string"?x:JSON.stringify(x))));body.appendChild(limits);}
      if(item.receipt?.path){body.appendChild(node("p","intake-receipt","Saved to "+item.receipt.path));
        (item.receipt.attachments || []).forEach((a)=>body.appendChild(node("p","hint","Attachment saved: "+a.name)));
        body.appendChild(node("p","hint",item.state==="processed"?"Saved and indexed for search. Further synthesis is separate.":"Saved source retained. Indexing or complete preservation is still pending."));}
      if(item.receipt?.path)body.appendChild(button("Read saved note",()=>{close();openNote(item.receipt.path,item.title);}));
      if(["error","saved","processing"].includes(item.state)) {
        const retry=button(item.limitations?.length?"Retry missing source material":"Retry",async()=>{retry.disabled=true;try{
          await request("/api/correspondence/"+encodeURIComponent(id)+"/retry",{});await load();await open(id);
        }catch(e){error(body,e);retry.disabled=!!snapshot?.read_only;}});retry.disabled=!!snapshot?.read_only;body.appendChild(retry);
      }
      if(item.task_status && item.task_status!=="none")body.appendChild(node("p","hint","Reminder: "+item.task_status));
      if(item.task_ids?.length)body.appendChild(button("Open reminder",()=>{close();openApp("brief");window.ViraAssistant?.reveal(item.task_ids[0]);}));
      if(item.error)error(body,item.error);
      if(item.task_error)error(body,item.task_error);
      if(!item.receipt && !["saved","processing","processed","ignored"].includes(item.state)) {
        const form=node("form","intake-decision");
        const disposition=selectField(form,"What should happen?",[["keep","Save as reference"],["task","Create or link a reminder"],["both","Save and create a reminder"],["ignore","Dismiss"]],item.disposition || "keep");
        const dest=selectField(form,"Vault",destinations(),item.destination),folder=textField(form,"Category",item.folder,"Optional category");
        const foot=node("div","workspace-toolbar"),approve=node("button","btn small","Confirm choice");approve.type="submit";approve.disabled=!!snapshot?.read_only;
        foot.appendChild(approve);form.appendChild(foot);
        form.addEventListener("submit",async(e)=>{e.preventDefault();approve.disabled=true;try{
          await request("/api/correspondence/"+encodeURIComponent(id)+"/review",{action:disposition.value==="ignore"?"dismiss":"approve",disposition:disposition.value,destination:dest.value,folder:folder.value.trim()});
          await load();await open(id);
        }catch(e){error(form,e);approve.disabled=!!snapshot?.read_only;}});body.appendChild(form);
      }
      if(item.person_id)body.appendChild(button("Open conversation",()=>{close();openPerson(item.person_id);}));
    }catch(e){body.replaceChildren();error(body,e);}
  }
  async function load(){
    if(pending)return pending;
    pending=(async()=>{try{snapshot=await request("/api/correspondence");snapshot.items ||= [];renderReview();renderInbox();}
      catch(e){for(const sel of ["#correspondence-inbox","#correspondence-review"]){const host=$(sel);if(host){host.hidden=false;error(host,e);}}}
      finally{pending=null;}})();return pending;
  }
  setInterval(()=>{if(!document.hidden && $("#view-attention")?.getClientRects().length)load();},30000);
  async function captureSource(sourceId) {
    try {
      const item = await request("/api/correspondence/capture", {source_id: sourceId});
      await load(); setAttentionTab("inbox", {defer:true}); openApp("attention"); await open(item.id);
    } catch (e) { toast(e.message); }
  }
  return {load,open,captureSource,setLiveCount(n){liveCount=n;countBadge();}};
})();
