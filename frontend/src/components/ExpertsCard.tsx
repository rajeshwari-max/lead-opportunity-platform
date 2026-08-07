import { useCallback, useEffect, useState } from "react";
import { Download, KeyRound, Loader2, RefreshCw, Upload, UserSearch } from "lucide-react";
import { VerticalBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface ExpertRow {
  vertical: string;
  count: number;
  search_url: string;
  /** Canonical vertical this Expert Pool vertical maps to (six-vertical system). */
  canonical_vertical?: string;
  updated_at: string | null;
}

export function ExpertsCard({ readOnly = false }: { readOnly?: boolean }) {
  const [rows, setRows] = useState<ExpertRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [sessionMsg, setSessionMsg] = useState<{ ok: boolean; text: string } | null>(null);

  /** Save the signed-in session to a file so it can be moved to a server. */
  const downloadSession = async () => {
    setSessionMsg(null);
    const res = await fetch("/api/devaid/session/export");
    if (!res.ok) {
      setSessionMsg({ ok: false, text: (await res.json()).detail ?? "Export failed" });
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "devaid_session.json";
    a.click();
    URL.revokeObjectURL(url);
    setSessionMsg({ ok: true, text: "Downloaded. Upload this file on the server." });
  };

  /** Install a session exported elsewhere. The server verifies it really is
   *  signed in before reporting success — an expired session is valid JSON. */
  const uploadSession = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setSessionMsg(null);
    try {
      const body = await file.text();
      const res = await fetch("/api/devaid/session/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      const data = await res.json();
      if (res.ok) {
        setSessionMsg({ ok: true, text: `Connected — ${data.cookies} cookies installed.` });
        setConnected(true);
      } else {
        setSessionMsg({ ok: false, text: data.detail ?? "Upload failed" });
      }
    } catch {
      setSessionMsg({ ok: false, text: "That file could not be read as JSON." });
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  };

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/experts");
      if (res.ok) setRows(await res.json());
      const st = await fetch("/api/devaid/status");
      if (st.ok) setConnected((await st.json()).connected);
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const connect = async () => {
    setConnecting(true);
    setError(null);
    try {
      const res = await fetch("/api/devaid/connect", { method: "POST" });
      if (res.ok) {
        setConnected(true);
      } else {
        // A 400 here means the window was closed before the login completed —
        // report it instead of optimistically flipping to "connected".
        setConnected(false);
        setError((await res.json()).detail ?? "Could not open login window");
      }
    } catch {
      setError("Connection failed — is the backend running?");
    } finally {
      setConnecting(false);
      load();
    }
  };

  const refresh = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/experts/refresh", { method: "POST" });
      const body = await res.json();
      if (res.ok) setRows(body);
      else setError(body.detail ?? "Refresh failed");
    } catch {
      setError("Refresh failed — is the backend running?");
    } finally {
      setBusy(false);
    }
  };

  const updated = rows[0]?.updated_at;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <UserSearch className="h-4 w-4" /> Expert Pool
          <span className="text-xs font-normal text-muted-foreground">(DevelopmentAid)</span>
        </CardTitle>
        <Button size="sm" variant="outline" disabled={busy} onClick={refresh}
                title="Re-count experts per vertical (English, 5+ yrs experience)">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
        </Button>
      </CardHeader>
      <CardContent className="space-y-2">
        {!connected && !readOnly && (
          <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5">
            <p className="text-xs text-amber-500">
              Not connected to DevelopmentAid. Click below — a Chrome window opens.
              Sign in fully (email, password and the reCAPTCHA), wait until you can
              see you're logged in, and only then close the window.
            </p>
            <Button size="sm" variant="outline" disabled={connecting} onClick={connect}>
              {connecting
                ? <><Loader2 className="h-3.5 w-3.5 animate-spin" /> Waiting for you to log in…</>
                : <><KeyRound className="h-3.5 w-3.5" /> Connect account</>}
            </Button>
          </div>
        )}
        {/* Sessions expire, so reconnecting must always be reachable — when the
            saved marker wrongly said "connected" this control was hidden, which
            left no way to fix the very problem that needed fixing. */}
        {connected && !readOnly && (
          <Button size="sm" variant="outline" className="w-full" disabled={connecting}
                  onClick={connect}
                  title="Sign in to DevelopmentAid again if scrapes only return one page">
            {connecting
              ? <><Loader2 className="h-3.5 w-3.5 animate-spin" /> Waiting for you to log in…</>
              : <><KeyRound className="h-3.5 w-3.5" /> Reconnect account</>}
          </Button>
        )}
        {/* Session transfer. On a server the Connect button above cannot work —
            there is no screen to open a login window on — so the session is
            carried across from a machine that has one. */}
        {!readOnly && (
          <div className="space-y-1.5 rounded-lg border border-border p-2.5">
            <p className="text-xs font-medium">Move this session to another machine</p>
            <p className="text-xs text-muted-foreground">
              A server can't show a login window. Log in here, download the
              session, then upload it on the server.
            </p>
            <div className="flex flex-wrap gap-1.5">
              <Button size="sm" variant="outline" onClick={downloadSession}>
                <Download className="h-3.5 w-3.5" /> Download session
              </Button>
              <label className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-muted">
                <Upload className="h-3.5 w-3.5" />
                {uploading ? "Checking…" : "Upload session"}
                <input type="file" accept="application/json,.json" className="hidden"
                       disabled={uploading} onChange={uploadSession} />
              </label>
            </div>
            {sessionMsg && (
              <p className={`text-xs ${sessionMsg.ok ? "text-emerald-500" : "text-red-500"}`}>
                {sessionMsg.text}
              </p>
            )}
          </div>
        )}
        {rows.length === 0 && !error && (
          <p className="text-sm text-muted-foreground">
            No counts yet — configure vertical URLs, then press refresh. Filters applied:
            English, 5+ years experience, one canonical vertical badge per row.
          </p>
        )}
        {rows.map((r) => (
          <a key={r.vertical} href={r.search_url} target="_blank" rel="noreferrer"
             className="flex items-center justify-between gap-2 rounded-lg border border-border px-3 py-2 text-sm transition-colors hover:bg-muted/50">
            <span className="min-w-0 flex-1">
              <span className="block truncate">{r.vertical}</span>
              {r.canonical_vertical && <VerticalBadge vertical={r.canonical_vertical} className="mt-1" />}
            </span>
            <span className="shrink-0 font-bold tabular-nums text-primary">
              {r.count.toLocaleString()}
            </span>
          </a>
        ))}
        {updated && (
          <p className="text-right text-[11px] text-muted-foreground">
            Updated {new Date(updated).toLocaleString()}
          </p>
        )}
        {error && (
          <p className="rounded-lg bg-red-500/15 px-3 py-2 text-xs text-red-400">{error}</p>
        )}
      </CardContent>
    </Card>
  );
}
