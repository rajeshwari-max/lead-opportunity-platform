/** Reserve the tab during the click so popup blockers don't hide the login. */
export async function connectAccount(source: string, localEndpoint: string): Promise<void> {
  const tab = source === "developmentaid" ? window.open("about:blank", "_blank") : null;
  if (tab) {
    tab.opener = null;
    tab.document.title = "Connect DevelopmentAid";
    tab.document.body.textContent = "Opening the login window…";
  }
  try {
    if (source === "developmentaid") {
      const config = await fetch("/api/remote-login/config");
      if (!config.ok) throw new Error("Could not check login availability. Sign in as an administrator.");
      if ((await config.json()).remote) {
        if (!tab) throw new Error("Allow popups for this platform, then click Connect again.");
        const res = await fetch("/api/remote-login/start", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail ?? "Could not start the login window.");
        tab.location.replace(data.viewer_url);
        // Poll the saved result so the original dashboard updates as well.
        for (;;) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          const status = await fetch(`/api/remote-login/${data.id}/status`, { cache: "no-store" });
          if (!status.ok) throw new Error("Login access ended. Sign in to the dashboard again.");
          const result = await status.json();
          if (result.state === "connected") return;
          if (["unverified", "cancelled", "expired", "error"].includes(result.state)) {
            throw new Error(result.message);
          }
          if (tab.closed && result.state !== "saving") {
            await fetch(`/api/remote-login/${data.id}/cancel`, { method: "POST" });
            throw new Error("Login window closed before saving.");
          }
        }
      }
    }
    tab?.close();
    const res = await fetch(localEndpoint, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail ?? "Could not open a login window.");
  } catch (e) {
    // Keep a real viewer open so its result remains readable.
    try { if (tab && tab.location.href === "about:blank") tab.close(); } catch { /* navigated */ }
    throw e;
  }
}
