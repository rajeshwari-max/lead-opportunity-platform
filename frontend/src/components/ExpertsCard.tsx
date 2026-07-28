import { useCallback, useEffect, useState } from "react";
import { KeyRound, Loader2, RefreshCw, UserSearch } from "lucide-react";
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

export function ExpertsCard() {
  const [rows, setRows] = useState<ExpertRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        {!connected && (
          <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5">
            <p className="text-xs text-amber-500">
              Not connected to DevelopmentAid. Click below — a Chrome window opens,
              log in there yourself, then close it. Your session is saved for scraping.
            </p>
            <Button size="sm" variant="outline" disabled={connecting} onClick={connect}>
              {connecting
                ? <><Loader2 className="h-3.5 w-3.5 animate-spin" /> Waiting for you to log in…</>
                : <><KeyRound className="h-3.5 w-3.5" /> Connect account</>}
            </Button>
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
