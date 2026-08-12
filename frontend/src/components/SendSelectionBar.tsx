import { useEffect, useState } from "react";
import { Loader2, Mail, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { TeamMember } from "@/lib/types";

interface Props {
  selectedIds: number[];
  onClear: () => void;
}

/** Bar that appears once rows are ticked, offering to email that exact set.
 *
 *  Recipients are team members, not free-text addresses. The approval buttons
 *  inside the email are signed per recipient, and accepting arbitrary addresses
 *  would make this an open relay for anyone with dashboard access.
 */
export function SendSelectionBar({ selectedIds, onClear }: Props) {
  const [open, setOpen] = useState(false);
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [picked, setPicked] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!open || team.length) return;
    api.team().then(setTeam).catch(() => setTeam([]));
  }, [open, team.length]);

  // Clearing the selection should also close the picker — leaving it open over
  // an empty selection offers a Send button that cannot do anything.
  useEffect(() => {
    if (selectedIds.length === 0) {
      setOpen(false);
      setResult(null);
    }
  }, [selectedIds.length]);

  if (selectedIds.length === 0) return null;

  const send = async () => {
    setBusy(true);
    setResult(null);
    try {
      const res = await api.sendSelection(selectedIds, picked);
      const sent = res.filter((r) => r.sent > 0);
      const failed = res.filter((r) => r.sent === 0);
      setResult({
        ok: failed.length === 0,
        text: failed.length === 0
          ? `Sent ${selectedIds.length} to ${sent.map((r) => r.member).join(", ")}.`
          : `Sent to ${sent.length}; failed for ${failed.map((r) => r.member).join(", ")}.`,
      });
      if (failed.length === 0) {
        setPicked([]);
        onClear();
      }
    } catch (e) {
      setResult({ ok: false, text: e instanceof Error ? e.message : "Send failed" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border bg-primary/[0.06] px-4 py-2">
      <span className="text-sm font-medium">
        {selectedIds.length} selected
      </span>
      <Button size="sm" variant="outline" className="h-8 text-xs" onClick={onClear}>
        <X className="mr-1 h-3.5 w-3.5" /> Clear
      </Button>
      <Button size="sm" className="h-8 text-xs" onClick={() => setOpen((v) => !v)}>
        <Mail className="mr-1 h-3.5 w-3.5" /> Email these
      </Button>

      {result && (
        <span className={`text-xs ${result.ok ? "text-emerald-500" : "text-red-500"}`}>
          {result.text}
        </span>
      )}

      {open && (
        <div className="w-full rounded-lg border border-border bg-background p-3">
          <p className="mb-2 text-xs font-medium">Send to</p>
          {team.length === 0 && (
            <p className="text-xs text-muted-foreground">
              No team members yet. Add them in the Team panel first.
            </p>
          )}
          <div className="flex flex-wrap gap-1.5">
            {team.map((m) => {
              const on = picked.includes(m.id);
              return (
                <button
                  key={m.id}
                  type="button"
                  onClick={() =>
                    setPicked((p) => (on ? p.filter((x) => x !== m.id) : [...p, m.id]))
                  }
                  title={m.email}
                  className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                    on
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border hover:bg-muted"
                  }`}
                >
                  {m.name}
                </button>
              );
            })}
          </div>
          <div className="mt-3 flex items-center gap-2">
            <Button size="sm" className="h-8 text-xs" disabled={busy || picked.length === 0}
                    onClick={send}>
              {busy
                ? <><Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> Sending…</>
                : <>Send {selectedIds.length} to {picked.length || "…"}</>}
            </Button>
            <span className="text-xs text-muted-foreground">
              These are sent as an extra copy — they are not marked as sent, so
              the scheduled digest still behaves normally.
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
