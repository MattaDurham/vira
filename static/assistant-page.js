/* Focused assistant entry point. No desktop modules or source readers boot. */
"use strict";

async function assistantRequest(path, data) {
  const options = data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  };
  const response = await fetch(path, { ...options, credentials: "same-origin" });
  if (!response.ok) throw new Error(await response.text() || "Request failed");
  return response.json();
}
function api(path) { return assistantRequest(path); }
function post(path, data) { return assistantRequest(path, data); }
function openPerson(id) { location.assign("/#person/" + encodeURIComponent(id)); }
function openApp(id) { location.assign("/#" + (id === "setup" ? "setup/notifications" : encodeURIComponent(id))); }

document.addEventListener("DOMContentLoaded", () => {
  window.ViraAssistant.load();
});
