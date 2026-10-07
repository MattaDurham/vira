// OAuth codes stay in the fragment, never in request URLs or access logs.
const params = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, "", location.pathname);
const result = document.getElementById("result");
(async () => {
  try {
    const response = await fetch("/api/mail/graph/browser/complete", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({state: params.get("state") || "", code: params.get("code") || "",
                            error: params.get("error") || "", error_description: params.get("error_description") || ""})
    });
    for (const key of [...params.keys()]) params.delete(key);
    const status = await response.json();
    result.textContent = status.connected ? "Connected " + status.email + ". Return to Vira; mail and calendar are ready."
      : status.error || status.detail || "Sign-in did not finish. Return to Vira and try again.";
  } catch {
    result.textContent = "Could not finish sign-in. Return to Vira and try Connect Microsoft again.";
  }
})();
