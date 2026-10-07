// The single-use local bridge is never sent in URLs to Microsoft or access logs.
let ticket = new URLSearchParams(location.hash.slice(1)).get("ticket") || "";
history.replaceState(null, "", location.pathname);
(async () => {
  const result = document.getElementById("result");
  try {
    const response = await fetch("/api/mail/graph/browser/launch", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ticket})
    });
    ticket = "";
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Could not open Microsoft sign-in.");
    location.replace(body.authorize_url);
  } catch (error) {
    ticket = "";
    result.textContent = error.message || "Return to Vira and click Connect Microsoft again.";
  }
})();
