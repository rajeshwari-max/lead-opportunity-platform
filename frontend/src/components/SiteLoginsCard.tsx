import { useCallback, useEffect, useState } from "react";
import { Check, Download, KeyRound, Loader2, LogOut, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface SiteSession {
  source: string;
  display: string;
  login_url: string;
  connected: boolean;
  via: string | null;
}

/** Sites that show little or nothing to an anonymous visitor.
 *
 *  A person signs in once in a real browser and the session is reused. No
 *  password is stored or typed by the app — the same design as DevelopmentAid,
 *  for the same reasons: shared accounts, CAPTCHAs on the login forms, and a
 *  session that survives a password change.
 */
export function SiteLoginsCard({ isAdmin = true }: { isAdmin?: boolean }) {
  const [sites, setSites] = useState<SiteSession[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/sessions");
      if (r.ok) setSites(await r.json());
    } catch {
      setSites([]);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) load();
  }, [isAdmin, load]);

  if (!isAdmin || sites.length === 0) return null;

  const connect = async (s: SiteSession) => {
    setBusy(s.source);
    setMsg(null);
    try {
      const r = await fetch(`/api/sessions/${s.source}/connect`, { method: "POST" });
      const body = await r.json();
      setMsg(r.ok
        ? { ok: true, text: `${s.display} connected.` }
        : { ok: false, text: body.detail ?? "Could not open a login window" });
    } catch {
      setMsg({ ok: false, text: "Connection failed — is the backend running?" });
    } finally {
      setBusy(null);
      load();
    }
  };

  const download = async (s: SiteSession) => {
    setMsg(null);
    const r = await fetch(`/api/sessions/${s.source}/export`);
    if (!r.ok) {
      setMsg({ ok: false, text: (await r.json()).detail ?? "Export failed" });
      return;
    }
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `${s.source}_session.json`;
    a.click();
    URL.revokeObjectURL(url);
    setMsg({ ok: true, text: "Downloaded — upload this on the server." });
  };

  const upload = async (s: SiteSession, e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(s.source);
    setMsg(null);
    try {
      const r = await fetch(`/api/sessions/${s.source}/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: await file.text(),
      });
      const body = await r.json();
      setMsg(r.ok
        ? { ok: true, text: `${s.display}: ${body.cookies} cookies installed.` }
        : { ok: false, text: body.detail ?? "Upload failed" });
    } catch {
      setMsg({ ok: false, text: "That file could not be read as JSON." });
    } finally {
      setBusy(null);
      e.target.value = "";
      load();
    }
  };

  const disconnect = async (s: SiteSession) => {
    await fetch(`/api/sessions/${s.source}`, { method: "DELETE" });
    setMsg({ ok: true, text: `${s.display} disconnected.` });
    load();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="h-4 w-4" /> Site Logins
          <span className="text-xs font-normal text-muted-foreground">
            {sites.filter((s) => s.connected).length}/{sites.length}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-xs text-muted-foreground">
          These boards hide most listings from anonymous visitors. Sign in once in
          the window that opens — the app never sees or stores your password.
        </p>

        {sites.map((s) => (
          <div key={s.source} className="space-y-1.5 rounded-lg border border-border p-2.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">{s.display}</span>
              {s.connected ? (
                <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-500">
                  <Check className="h-3.5 w-3.5" /> connected
                </span>
              ) : (
                <span className="text-xs text-muted-foreground">not connected</span>
              )}
            </div>
            {s.via && <p className="text-[11px] text-muted-foreground">via {s.via}</p>}

            <div className="flex flex-wrap gap-1.5">
              <Button size="sm" variant="outline" className="h-7 text-xs"
                      disabled={busy === s.source} onClick={() => connect(s)}>
                {busy === s.source
                  ? <><Loader2 className="mr-1 h-3 w-3 animate-spin" /> waiting…</>
                  : <><KeyRound className="mr-1 h-3 w-3" /> {s.connected ? "Reconnect" : "Connect"}</>}
              </Button>
              {s.connected && (
                <>
                  <Button size="sm" variant="outline" className="h-7 text-xs"
                          onClick={() => download(s)}>
                    <Download className="mr-1 h-3 w-3" /> Session
                  </Button>
                  <Button size="sm" variant="outline" className="h-7 text-xs"
                          onClick={() => disconnect(s)}>
                    <LogOut className="mr-1 h-3 w-3" /> Forget
                  </Button>
                </>
              )}
              <label className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-muted">
                <Upload className="h-3 w-3" /> Upload
                <input type="file" accept="application/json,.json" className="hidden"
                       onChange={(e) => upload(s, e)} />
              </label>
            </div>
          </div>
        ))}

        {msg && (
          <p className={`text-xs ${msg.ok ? "text-emerald-500" : "text-red-500"}`}>
            {msg.text}
          </p>
        )}
        <p className="text-[11px] text-muted-foreground">
          A server has no screen, so Connect only works on your computer. Sign in
          there, download the session, then upload it on the server.
        </p>
      </CardContent>
    </Card>
  );
}
