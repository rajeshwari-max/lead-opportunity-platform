import { useCallback, useEffect, useState } from "react";
import { CalendarClock, Check, Infinity as InfinityIcon, Loader2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { ReviewQueueItem, ReviewQueueResponse } from "@/lib/types";

/** Rows whose closing date could not be determined.
 *
 *  These are stored Active but are not actionable, so they appear in neither
 *  the live table nor the archive. Until this panel existed, "held for review"
 *  and "silently lost" looked identical from the dashboard.
 *
 *  The panel shows `deadline_raw` — the source's own words — prominently,
 *  because nine times in ten the date is right there in a format the parser did
 *  not recognise, and a person can read it in a second. That is the whole
 *  reason a human queue beats another parsing heuristic here.
 */
export function ReviewQueueCard({ readOnly = false }: { readOnly?: boolean }) {
  const [data, setData] = useState<ReviewQueueResponse | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [dateFor, setDateFor] = useState<Record<number, string>>({});
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.reviewQueue({ limit: expanded ? 50 : 5 }));
      setError("");
    } catch {
      setError("Could not load the review queue.");
    }
  }, [expanded]);

  useEffect(() => {
    void load();
  }, [load]);

  const decide = async (
    item: ReviewQueueItem,
    decision: "dated" | "rolling" | "closed",
  ) => {
    const deadline = dateFor[item.id];
    if (decision === "dated" && !deadline) {
      setError("Pick a date first, or use Still open / Closed.");
      return;
    }
    setBusyId(item.id);
    setError("");
    try {
      await api.decideReviewItem(item.id, decision, deadline);
      await load();
    } catch {
      setError("That decision could not be saved. Try again.");
    } finally {
      setBusyId(null);
    }
  };

  // An empty queue is the healthy state and does not deserve a permanent
  // card. Hidden rather than shown as a reassuring zero.
  if (!data || data.total === 0) return null;

  const items = data.items;
  const topSource = data.by_source[0];
  // A backlog concentrated in one source is a parser bug for that source, not
  // a review job — clearing it by hand would be the wrong response, so say so
  // rather than presenting 900 rows as work for a person.
  const concentrated =
    topSource && data.total >= 20 && topSource.count / data.total >= 0.6;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <CalendarClock className="h-4 w-4 text-amber-600" />
          Needs a closing date
          <span className="ml-auto rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800">
            {data.total}
          </span>
        </CardTitle>
      </CardHeader>

      <CardContent className="flex flex-col gap-3 text-sm">
        <p className="text-muted-foreground">
          The source did not give a closing date we could read. These are held
          here — they are not shown as live and not archived.
        </p>

        {concentrated && (
          <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-900">
            <strong>{topSource.count}</strong> of these come from{" "}
            <strong>{topSource.source_website}</strong>. That looks like a date
            format its scraper cannot read, which is worth fixing at the source
            rather than clearing by hand.
          </p>
        )}

        {error && <p className="text-xs text-red-600">{error}</p>}

        <ul className="flex flex-col gap-3">
          {items.map((item) => (
            <li key={item.id} className="rounded-md border p-2">
              <a
                href={item.opportunity_url || "#"}
                target="_blank"
                rel="noreferrer"
                className="line-clamp-2 font-medium hover:underline"
              >
                {item.title}
              </a>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {item.source_website}
              </p>

              {/* The evidence a decision is actually made from. */}
              {item.deadline_raw && (
                <p className="mt-1 rounded bg-muted px-2 py-1 text-xs">
                  Source says: <span className="font-mono">{item.deadline_raw}</span>
                </p>
              )}

              {!readOnly && (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <Input
                    type="date"
                    className="h-7 w-[9.5rem] text-xs"
                    value={dateFor[item.id] ?? ""}
                    onChange={(e) =>
                      setDateFor((d) => ({ ...d, [item.id]: e.target.value }))
                    }
                  />
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 px-2 text-xs"
                    disabled={busyId === item.id}
                    onClick={() => void decide(item, "dated")}
                  >
                    {busyId === item.id ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : (
                      <Check className="h-3 w-3" />
                    )}
                    <span className="ml-1">Set date</span>
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 px-2 text-xs"
                    disabled={busyId === item.id}
                    onClick={() => void decide(item, "rolling")}
                  >
                    <InfinityIcon className="h-3 w-3" />
                    <span className="ml-1">Still open</span>
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 px-2 text-xs"
                    disabled={busyId === item.id}
                    onClick={() => void decide(item, "closed")}
                  >
                    <XCircle className="h-3 w-3" />
                    {/* "Closed", not "Delete" — the row is archived and kept. */}
                    <span className="ml-1">Closed</span>
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>

        {data.total > items.length && (
          <Button
            variant="ghost"
            size="sm"
            className="text-xs"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? "Show fewer" : `Show more (${data.total - items.length} more)`}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
