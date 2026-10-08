// One confirmation for System, source-edit hints, and Update & restart.
const restartSheet = bindSheet("#restart-sheet");
let restartOperation = "restart", restartStatus = null, restartBoot = null;
let restartBusy = false, restartDeadline = null, restartOpener = null;

function renderRestart(status) {
  restartStatus = status;
  const queued = ["waiting", "updating", "restarting"].includes(status.phase);
  if (queued && !restartBoot) restartBoot = status.boot_id;
  if (restartBoot && status.boot_id !== restartBoot) {
    location.reload();
    return;
  }
  if (["updating", "restarting"].includes(status.phase)) {
    // Allow dependency installation up to its existing five-minute timeout.
    restartDeadline ||= Date.now() + (status.phase === "updating" ? 360000 : 60000);
  } else if (status.phase !== "waiting" || status.mode !== "now") restartDeadline = null;
  const state = $("#restart-state");
  state.textContent = status.error || status.unavailable_reason || status.note ||
    (status.phase === "waiting" && status.mode === "now" ? "Restart is starting…" :
     status.phase === "waiting" ? "Restart queued. Vira will wait for all listed work to finish. Sessions waiting for your input need attention first. You can close this dialog; the server keeps waiting." :
     status.phase === "updating" ? "Installing updates. Vira will restart when installation succeeds…" :
     status.phase === "restarting" ? "Restarting Vira. This page will reconnect automatically…" :
     status.activities.length ? "Active work is listed below. Wait for it to finish, or restart now." :
     "No active sessions or tracked work.");
  if (restartDeadline && Date.now() > restartDeadline) {
    state.textContent = "Restart has not completed. Check Vira's startup service, then reload this page.";
  }
  const list = $("#restart-activities");
  list.replaceChildren();
  for (const item of status.activities) {
    const row = el("div", "restart-activity");
    row.append(el("strong", "", item.title), el("p", "hint", item.detail));
    list.appendChild(row);
  }
  $("#restart-wait").disabled = $("#restart-now").disabled =
    restartBusy || !status.available || queued;
  $("#restart-wait").hidden = status.activities.length === 0 && !queued;
  $("#restart-now").textContent = restartOperation === "update" ? "Update & restart now" : "Restart now";
  $("#restart-cancel-queued").hidden = status.phase !== "waiting";
  $("#restart-cancel-queued").disabled = restartBusy;
  $("#restart-close").textContent = queued ? "Close" : "Cancel";
  let banner = $("#restart-queue-banner");
  if (queued) {
    if (!banner) {
      banner = el("button", "hood-restart");
      banner.id = "restart-queue-banner";
      banner.onclick = () => openRestart(status.operation);
      document.body.appendChild(banner);
    }
    banner.textContent = status.phase === "waiting" ? "Restart queued — view work or cancel" : "Vira is restarting — reconnecting…";
  } else {
    banner?.remove();
    restartBoot = null;
  }
}

async function refreshRestart() {
  try {
    const status = await api("/api/restart");
    renderRestart(status);
  } catch (error) {
    if (restartBoot) {
      $("#restart-state").textContent = "Waiting for Vira to reconnect…";
      if (restartDeadline && Date.now() > restartDeadline) {
        $("#restart-state").textContent = "Vira has not reconnected. Check its startup service, then reload this page.";
        $("#restart-queue-banner")?.remove();
        restartBoot = null;
      }
    } else {
      $("#restart-state").textContent = "Could not check active work: " + errText(error);
      $("#restart-wait").disabled = $("#restart-now").disabled = true;
    }
  }
}

async function openRestart(operation = "restart") {
  restartOperation = operation;
  restartOpener = document.activeElement;
  $("#restart-explanation").textContent = operation === "update"
    ? "Update Vira and restart? Open connections will briefly disconnect. Detached sessions continue, flows resume, and in-app requests or background work may be interrupted. Unsaved form edits may be lost."
    : "Restart Vira? Open connections will briefly disconnect. Detached sessions continue, flows resume, and in-app requests or background work may be interrupted. Unsaved form edits may be lost.";
  $("#restart-wait").disabled = $("#restart-now").disabled = true;
  $("#restart-state").textContent = "Checking active work…";
  restartSheet.open();
  $("#restart-close").focus();
  await refreshRestart();
}

async function submitRestart(mode) {
  if (restartBusy || !restartStatus?.available) return;
  restartBusy = true;
  renderRestart(restartStatus);
  try {
    const status = await post("/api/restart", { mode, operation: restartOperation });
    // The first poll may miss the brief restarting phase. Start the
    // reconnect deadline at acceptance for an immediate request too.
    restartDeadline = mode === "now" ? Date.now() + (restartOperation === "update" ? 360000 : 60000) : null;
    renderRestart(status);
    if (mode === "now") restartDeadline = Date.now() + (restartOperation === "update" ? 360000 : 60000);
  } catch (error) {
    restartBusy = false;
    await refreshRestart();
    $("#restart-state").textContent = "Could not queue restart: " + errText(error);
    return;
  } finally {
    restartBusy = false;
  }
  if (restartStatus) renderRestart(restartStatus);
}

$("#restart-now").onclick = () => submitRestart("now");
$("#restart-wait").onclick = () => submitRestart("when_idle");
$("#restart-cancel-queued").onclick = async () => {
  restartBusy = true;
  try { renderRestart(await post("/api/restart/cancel", {})); }
  catch (error) { $("#restart-state").textContent = errText(error); }
  finally { restartBusy = false; await refreshRestart(); }
};
$("#restart-close").onclick = () => { restartSheet.close(); restartOpener?.focus(); };
// Keep keyboard focus within the confirmation. Escape uses bindSheet.
$("#restart-sheet").addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const buttons = [...$("#restart-sheet").querySelectorAll("button")]
    .filter((button) => !button.disabled && !button.hidden);
  const first = buttons[0], last = buttons[buttons.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
startPoll(refreshRestart, 2000);
refreshRestart();
