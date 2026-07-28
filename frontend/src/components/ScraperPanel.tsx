import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { CalendarClock, CircleDot, Layers, Pause, Play, Square, Terminal } from "lucide-react";
import { api } from "@/lib/api";
import { formatDuration } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { ProgressBar } from "@/components/ui/progress";
import { VERTICALS, type Progress, type ScheduleStatus, type SourceInfo } from "@/lib/types";

interface Props {
  sources: SourceInfo[];
  progress: Progress | null;
}

const statusColor: Record<string, string> = {
  running: "text-emerald-500",
  queued: "text-muted-foreground",
  completed: "text-emerald-500",
  failed: "text-red-500",
  stopped: "text-amber-500",
  success: "text-emerald-500",
  skipped: "text-amber-500",
};

const fmtWhen = (iso: string | null | undefined): string =>
  iso ? new Date(iso.endsWith("Z") ? iso : iso + "Z").toLocaleString() : "—";

export function ScraperPanel({ sources, progress }: Props) {
  const [selected, setSelected] = useState<string[]>([]);
  const [scrapeVerticals, setScrapeVerticals] = useState<string[]>([]);
  const [schedule, setSchedule] = useState<ScheduleStatus | null>(null);
  const [cron, setCron] = useState("0 2 * * *");
  const active = progress != null && progress.state !== "idle";
  const allSelected = sources.length > 0 && selected.length === sources.length;

  // Default: all websites ticked once the source list loads
  useEffect(() => {
    setSelected(sources.map((s) => s.name));
  }, [sources]);

  // The schedule is persisted server-side — load it so a refresh keeps the state.
  useEffect(() => {
    api
      .getSchedule()
      .then((s) => {
        setSchedule(s);
        if (s.cron) setCron(s.cron);
      })
      .catch(console.error);
  }, []);

  // While a scheduled scrape may fire in the background, keep Next/Last Run fresh.
  useEffect(() => {
    if (!schedule || schedule.mode === "manual") return;
    const id = setInterval(() => {
      api.getSchedule().then(setSchedule).catch(() => undefined);
    }, 60_000);
    return () => clearInterval(id);
  }, [schedule?.mode]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (name: string, checked: boolean) =>
    setSelected((prev) => (checked ? [...prev, name] : prev.filter((n) => n !== name)));

  const toggleVertical = (s: string, checked: boolean) =>
    setScrapeVerticals((prev) => (checked ? [...prev, s] : prev.filter((x) => x !== s)));

  const applySchedule = async (mode: string) => {
    const result = await api.setSchedule(
      mode === "cron"
        ? { mode, cron }
        : { mode, hour: schedule?.hour ?? 2, minute: schedule?.minute ?? 0 }
    );
    if (result) setSchedule(result);
  };

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Terminal className="h-4 w-4" /> Scraper Control
        </CardTitle>
        <span className={`flex items-center gap-1.5 text-xs font-medium ${active ? "text-emerald-500" : "text-muted-foreground"}`}>
          <CircleDot className="h-3 w-3" />
          {progress?.state ?? "idle"}
        </span>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Website selection */}
        <div>
          <p className="mb-1 text-xs font-semibold text-muted-foreground">Websites</p>
          <Checkbox label="Select All" checked={allSelected}
                    onChange={(c) => setSelected(c ? sources.map((s) => s.name) : [])} />
          {sources.map((s) => (
            <Checkbox key={s.name} label={s.display_name}
                      checked={selected.includes(s.name)}
                      onChange={(c) => toggle(s.name, c)} />
          ))}
        </div>

        {/* Vertical-aware scraping: only keep opportunities in the ticked verticals */}
        <div>
          <p className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
            <Layers className="h-3.5 w-3.5" /> Verticals
            <span className="font-normal">(none = all)</span>
          </p>
          <div className="max-h-36 space-y-0.5 overflow-y-auto pr-1">
            {VERTICALS.map((s) => (
              <Checkbox key={s} label={s}
                        checked={scrapeVerticals.includes(s)}
                        onChange={(c) => toggleVertical(s, c)} />
            ))}
          </div>
        </div>

        {/* Controls */}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={active || selected.length === 0}
                  title={selected.length === 0 ? "Select at least one website" : "Start scraping"}
                  onClick={() => api.startScrape(allSelected ? [] : selected, scrapeVerticals)}>
            <Play className="h-3.5 w-3.5" /> Start
          </Button>
          <Button size="sm" variant="outline" disabled={progress?.state !== "running"}
                  onClick={() => api.pause()}>
            <Pause className="h-3.5 w-3.5" /> Pause
          </Button>
          <Button size="sm" variant="outline" disabled={progress?.state !== "paused"}
                  onClick={() => api.resume()}>
            <Play className="h-3.5 w-3.5" /> Resume
          </Button>
          <Button size="sm" variant="destructive" disabled={!active} onClick={() => api.stop()}>
            <Square className="h-3.5 w-3.5" /> Stop
          </Button>
        </div>

        {/* Progress */}
        <AnimatePresence>
          {active && progress && (
            <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}
                        exit={{ opacity: 0, height: 0 }} className="space-y-2">
              <ProgressBar value={progress.progress_percent} />
              <div className="flex justify-between text-xs text-muted-foreground">
                <span>{progress.progress_percent}%</span>
                <span>elapsed {formatDuration(progress.elapsed_seconds)} · ETA {formatDuration(progress.eta_seconds)}</span>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Per-source status */}
        {progress && Object.keys(progress.sources).length > 0 && (
          <div className="space-y-1 text-xs">
            {Object.entries(progress.sources).map(([name, s]) => (
              <div key={name} className="flex items-center justify-between rounded-md bg-muted/50 px-2 py-1.5">
                <span className="font-medium">{s.display_name}</span>
                <span className="text-muted-foreground">
                  p{s.pages} · found {s.found} · saved {s.saved} · expired {s.skipped_expired}
                  <span className={`ml-2 font-medium ${statusColor[s.status] ?? ""}`}>{s.status}</span>
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Live logs */}
        {progress && progress.logs.length > 0 && (
          <div className="max-h-40 space-y-0.5 overflow-y-auto rounded-lg bg-zinc-950/90 p-2.5 font-mono text-[11px] leading-relaxed text-emerald-300/90">
            {progress.logs.slice(-40).map((l, i) => (
              <div key={i}>{l}</div>
            ))}
          </div>
        )}

        {/* Automatic scheduler — persisted server-side, survives refresh/restart */}
        <div className="space-y-2">
          <p className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
            <CalendarClock className="h-3.5 w-3.5" /> Schedule
          </p>
          <div className="flex gap-2">
            <select value={schedule?.mode ?? "manual"} onChange={(e) => applySchedule(e.target.value)}
                    className="h-8 flex-1 rounded-lg border border-border bg-card px-2 text-xs">
              <option value="manual">Manual only</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
              <option value="yearly">Yearly</option>
              <option value="cron">Custom cron</option>
            </select>
            {schedule?.mode === "cron" && (
              <input value={cron} onChange={(e) => setCron(e.target.value)}
                     onBlur={() => applySchedule("cron")}
                     className="h-8 w-28 rounded-lg border border-border bg-card px-2 font-mono text-xs" />
            )}
          </div>

          {schedule && schedule.mode !== "manual" && (
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 rounded-lg border border-border bg-muted/30 p-2.5 text-[11px]">
              <span className="text-muted-foreground">Next Run</span>
              <span className="text-right font-medium">{fmtWhen(schedule.next_run)}</span>
              <span className="text-muted-foreground">Last Run</span>
              <span className="text-right font-medium">{fmtWhen(schedule.last_run)}</span>
              <span className="text-muted-foreground">Status</span>
              <span className={`text-right font-medium ${statusColor[schedule.last_status ?? ""] ?? ""}`}>
                {schedule.last_status ?? "not run yet"}
              </span>
              <span className="text-muted-foreground">Last Success</span>
              <span className="text-right font-medium">{fmtWhen(schedule.last_success)}</span>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
