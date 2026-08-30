import { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { ScraperHealth, SourceHealth } from "@/lib/types";

/** Which sources are broken, and for how long.
 *
 *  792 of 916 runs recorded "completed", including all 127 attempts by the 16
 *  sources that never fetched a page. This reads the evidence runs now capture
 *  — outcome, error code, HTTP status — instead of that status word.
 *
 *  Staleness is measured from the last row SAVED, not the last run attempted,
 *  because a source that runs nightly and has saved nothing since July is
 *  broken and a "last run: today" reading would hide exactly that.
 */
const STATE_STYLE: Record<string, { label: string; cls: string }> = {
  failing: { label: "Failing", cls: "bg-red-100 text-red-800" },
  never_produced: { label: "Never produced", cls: "bg-orange-100 text-orange-900" },
  stale: { label: "Stale", cls: "bg-amber-100 text-amber-900" },
  unknown: { label: "No data yet", cls: "bg-slate-100 text-slate-700" },
  ok: { label: "OK", cls: "bg-emerald-100 text-emerald-800" },
};

export function ScraperHealthCard({ isAdmin = true }: { isAdmin?: boolean }) {
  const [data, setData] = useState<ScraperHealth | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setData(await api.scraperHealth());
    } catch {
      setData(null);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) void load();
  }, [isAdmin, load]);

  if (!isAdmin || !data) return null;

  const needing = data.sources.filter((s) => s.state !== "ok");
  const shown: SourceHealth[] = showAll ? data.sources : needing.slice(0, 8);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Activity className="h-4 w-4" />
          Source health
          <span className="ml-auto flex items-center gap-2">
            {data.summary.needs_attention > 0 && (
              <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-800">
                {data.summary.needs_attention} need attention
              </span>
            )}
            <Button variant="ghost" size="sm" className="h-6 px-1"
                    disabled={busy} onClick={() => void load()}>
              <RefreshCw className={`h-3 w-3 ${busy ? "animate-spin" : ""}`} />
            </Button>
          </span>
        </CardTitle>
      </CardHeader>

      <CardContent className="flex flex-col gap-3 text-sm">
        {data.alerting.length > 0 && (
          <p className="flex items-start gap-2 rounded-md bg-red-50 p-2 text-xs text-red-900">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              <strong>{data.alerting.length}</strong> source
              {data.alerting.length === 1 ? " has" : "s have"} failed{" "}
              {data.thresholds.failure_streak} runs in a row. One bad run is a
              site being down; {data.thresholds.failure_streak} is a pattern.
            </span>
          </p>
        )}

        {/* The decision the brief asks for, named rather than implied. A
            source nobody has scoped is not broken — but it is scraping without
            anyone having said what it should yield, and that is a question for
            an administrator, not a fault for an engineer. */}
        {(() => {
          const unscoped = data.sources.filter((s) => !s.scope_confirmed);
          if (unscoped.length === 0) return null;
          return (
            <details className="rounded-md bg-amber-50 p-2 text-xs text-amber-900">
              <summary className="cursor-pointer font-medium">
                {unscoped.length} source{unscoped.length === 1 ? "" : "s"} need a
                scope decision
              </summary>
              <p className="mt-1.5">
                Nobody has stated what these should collect, so they are judged
                only by the generic opportunity gate — which reads titles and
                URLs, not the source&rsquo;s own type and status fields. They are
                still scraping. For each one, the decision is: which record types
                should it yield, and from which official URL?
              </p>
              <ul className="mt-2 flex flex-col gap-0.5">
                {unscoped.slice(0, 12).map((s) => (
                  <li key={s.source_key} className="flex gap-2">
                    <span className="font-medium">{s.display_name}</span>
                    <span className="text-amber-800/70">
                      {s.implementation} · {s.fetch_mode}
                      {s.requires_login ? " · login" : ""}
                    </span>
                  </li>
                ))}
              </ul>
              {unscoped.length > 12 && (
                <p className="mt-1.5 text-amber-800/70">
                  and {unscoped.length - 12} more.
                </p>
              )}
            </details>
          );
        })()}

        {needing.length === 0 && (
          <p className="text-muted-foreground">
            Every source has produced rows recently.
          </p>
        )}

        <ul className="flex flex-col gap-2">
          {shown.map((s) => {
            const style = STATE_STYLE[s.state] ?? STATE_STYLE.unknown;
            return (
              <li key={s.source_key} className="rounded-md border p-2">
                <div className="flex items-start justify-between gap-2">
                  <span className="font-medium">{s.display_name}</span>
                  <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] font-semibold ${style.cls}`}>
                    {style.label}
                  </span>
                </div>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {s.total_rows.toLocaleString()} rows
                  {s.days_since_saved !== null
                    ? ` · last saved ${s.days_since_saved}d ago`
                    : " · never saved"}
                  {` · ${s.implementation}/${s.fetch_mode}`}
                  {s.last_outcome ? ` · ${s.last_outcome}` : ""}
                  {s.last_http_status ? ` · HTTP ${s.last_http_status}` : ""}
                </p>
                {s.note && <p className="mt-1 text-xs">{s.note}</p>}
                {s.last_error_message && (
                  <p className="mt-1 font-mono text-[11px] text-red-700">
                    {s.last_error_message}
                  </p>
                )}
              </li>
            );
          })}
        </ul>

        <Button variant="ghost" size="sm" className="text-xs"
                onClick={() => setShowAll((v) => !v)}>
          {showAll
            ? "Show only what needs attention"
            : `Show all ${data.summary.total} sources`}
        </Button>
      </CardContent>
    </Card>
  );
}
