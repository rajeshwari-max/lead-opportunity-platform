import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Clock, Loader2, Mail, Play } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { EmailSettings } from "@/lib/types";

/** Reminder offsets offered as toggles. Anything else can still be set through
 *  the API; these are the ones worth a click. */
const OFFSETS = [14, 10, 7, 3, 2, 1];

export function AutoEmailPanel({ readOnly = false }: { readOnly?: boolean }) {
  const [cfg, setCfg] = useState<EmailSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [emailConfigured, setEmailConfigured] = useState(true);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, status] = await Promise.all([api.emailSettings(), api.emailStatus()]);
      setCfg(s);
      setEmailConfigured(status.configured);
    } catch {
      /* backend down — leave the panel empty rather than showing wrong state */
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const patch = async (changes: Partial<EmailSettings>) => {
    setSaving(true);
    try {
      const next = await api.updateEmailSettings(changes);
      setCfg(next);
      setToast("Saved — takes effect immediately");
      setTimeout(() => setToast(null), 4000);
    } catch {
      setToast("Could not save");
      setTimeout(() => setToast(null), 4000);
    } finally {
      setSaving(false);
    }
  };

  const runNow = async () => {
    setRunning(true);
    try {
      const r = await api.runDigestNow();
      setToast(
        r.opportunities_sent > 0 || r.reminders_sent > 0
          ? `Sent ${r.opportunities_sent} to ${r.members_emailed} member(s), ${r.reminders_sent} reminder(s)`
          : "Nothing new to send right now"
      );
    } catch {
      setToast("Run failed — check SMTP settings");
    } finally {
      setRunning(false);
      setTimeout(() => setToast(null), 6000);
      load();
    }
  };

  if (!cfg) return null;

  const nextRun = cfg.next_run ? new Date(cfg.next_run) : null;
  const toggleOffset = (d: number) => {
    const set = new Set(cfg.reminder_days);
    set.has(d) ? set.delete(d) : set.add(d);
    patch({ reminder_days: [...set].sort((a, b) => b - a) });
  };

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Mail className="h-4 w-4" /> Automatic Emails
        </CardTitle>
        {!readOnly && (
          <Button size="sm" variant="outline" disabled={running || !emailConfigured}
                  title="Send the daily digest and any due reminders right now"
                  onClick={runNow}>
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            Run now
          </Button>
        )}
      </CardHeader>

      <CardContent className="space-y-4 text-sm">
        {!emailConfigured && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-400">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>SMTP isn't configured, so nothing will actually send. Add your
                  Gmail App Password to <b>backend/.env</b> and restart.</span>
          </div>
        )}

        {/* Master switch */}
        <label className="flex items-center justify-between gap-3">
          <span>
            <span className="font-medium">Send a daily email</span>
            <span className="block text-xs text-muted-foreground">
              New matches plus any deadline reminders due that day
            </span>
          </span>
          <input type="checkbox" checked={cfg.digest_enabled} disabled={readOnly || saving}
                 onChange={(e) => patch({ digest_enabled: e.target.checked })}
                 className="h-4 w-4 shrink-0 accent-primary" />
        </label>

        {cfg.digest_enabled && (
          <>
            <div className="flex items-center gap-2">
              <Clock className="h-3.5 w-3.5 text-muted-foreground" />
              <span className="text-xs text-muted-foreground">Every day at</span>
              <input
                type="time"
                disabled={readOnly || saving}
                value={`${String(cfg.digest_hour).padStart(2, "0")}:${String(cfg.digest_minute).padStart(2, "0")}`}
                onChange={(e) => {
                  const [h, m] = e.target.value.split(":").map(Number);
                  if (!Number.isNaN(h) && !Number.isNaN(m))
                    patch({ digest_hour: h, digest_minute: m });
                }}
                className="rounded-md border border-border bg-background px-2 py-1 text-xs outline-none focus:border-primary"
              />
              {nextRun && (
                <span className="text-xs text-muted-foreground">
                  next: {nextRun.toLocaleString()}
                </span>
              )}
            </div>

            <div>
              <p className="mb-1.5 text-xs font-semibold text-muted-foreground">
                Remind this many days before a deadline
              </p>
              <div className="flex flex-wrap gap-1.5">
                {OFFSETS.map((d) => {
                  const on = cfg.reminder_days.includes(d);
                  return (
                    <button key={d} disabled={readOnly || saving} onClick={() => toggleOffset(d)}
                            className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${
                              on ? "border-primary bg-primary/20 text-primary"
                                 : "border-border text-muted-foreground hover:bg-muted"}`}>
                      {d} days
                    </button>
                  );
                })}
              </div>
              {cfg.reminder_days.length === 0 && (
                <p className="mt-1 text-xs text-amber-400">
                  No reminders will be sent.
                </p>
              )}
            </div>
          </>
        )}

        <label className="flex items-center justify-between gap-3 border-t border-border pt-3">
          <span>
            <span className="font-medium">Send as soon as a scrape finishes</span>
            <span className="block text-xs text-muted-foreground">
              Newly found opportunities go out immediately instead of waiting
              for tomorrow's email
            </span>
          </span>
          <input type="checkbox" checked={cfg.send_on_scrape} disabled={readOnly || saving}
                 onChange={(e) => patch({ send_on_scrape: e.target.checked })}
                 className="h-4 w-4 shrink-0 accent-primary" />
        </label>

        <p className="text-xs text-muted-foreground">
          Emails go to every team member marked <b>Auto</b> below, each filtered
          to their own keywords and verticals.
        </p>

        {toast && (
          <div className="rounded-lg bg-emerald-500/15 px-3 py-2 text-xs font-medium text-emerald-400">
            {toast}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
