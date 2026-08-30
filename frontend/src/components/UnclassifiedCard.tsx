import { useCallback, useEffect, useState } from "react";
import { Loader2, Tags } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { UnclassifiedView } from "@/components/UnclassifiedView";
import type { UnclassifiedItem, UnclassifiedResponse } from "@/lib/types";

/** Rows the keyword classifier could not place in any vertical.
 *
 *  34% of actionable rows carry no vertical, and the dashboard's has_vertical
 *  filter defaults to ON — so a third of the database is invisible in the
 *  working view, not because anyone judged it irrelevant but because the
 *  keyword rules had nothing to say about it. These are the rows a person can
 *  label in seconds and the rules cannot label at all.
 *
 *  A label here is permanent against the classifier: the startup backfill
 *  skips human-labelled rows, so a correction is not undone at the next
 *  restart. That is also why "None of these" is a button rather than an
 *  absence — recording "it belongs to none of our six" is a decision, and
 *  leaving the row blank would let the backfill re-tag it.
 */
export function UnclassifiedCard({ readOnly = false }: { readOnly?: boolean }) {
  // The card is a count and a doorway. Everything the brief specifies —
  // search, six filter dimensions, paging, select-all across the filter —
  // lives in the full section, because a sidebar cannot host it honestly.
  const [openFull, setOpenFull] = useState(false);
  const [data, setData] = useState<UnclassifiedResponse | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await api.unclassified({ page_size: 12 }));
      setSelected(new Set());
      setError("");
    } catch {
      setError("Could not load unclassified opportunities.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const apply = async (verticals: string[]) => {
    if (selected.size === 0) {
      setError("Select at least one opportunity first.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.assignVerticals([...selected], verticals);
      await load();
    } catch {
      setError("That assignment could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  if (!data || data.total === 0) return null;
  if (openFull) {
    return <UnclassifiedView readOnly={readOnly}
                             onClose={() => { setOpenFull(false); void load(); }} />;
  }

  const items: UnclassifiedItem[] = data.items;
  const top = data.by_source[0];
  // A backlog concentrated in one source is a keyword gap for that source's
  // vocabulary — fixed once in the rules, not a thousand times by hand.
  const concentrated = top && data.total >= 50 && top.count / data.total >= 0.5;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Tags className="h-4 w-4 text-indigo-600" />
          Unclassified
          <button onClick={() => setOpenFull(true)}
                  className="ml-auto rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-semibold text-indigo-800 hover:bg-indigo-200">
            {data.total.toLocaleString()}
          </button>
        </CardTitle>
      </CardHeader>

      <CardContent className="flex flex-col gap-3 text-sm">
        <p className="text-muted-foreground">
          No vertical could be derived from these, so they are hidden from the
          main table. A label set here is kept — the classifier will not
          overwrite it.
        </p>

        {concentrated && (
          <p className="rounded-md bg-indigo-50 p-2 text-xs text-indigo-900">
            <strong>{top.count.toLocaleString()}</strong> of these come from{" "}
            <strong>{top.source_website}</strong>. That is a vocabulary gap for
            one source, which is worth fixing in the keyword rules rather than
            by hand.
          </p>
        )}

        {error && <p className="text-xs text-red-600">{error}</p>}

        <ul className="flex max-h-80 flex-col gap-1.5 overflow-y-auto">
          {items.map((item) => (
            <li key={item.id} className="flex items-start gap-2 rounded-md border p-2">
              <input
                type="checkbox"
                className="mt-1 h-3.5 w-3.5 shrink-0"
                checked={selected.has(item.id)}
                onChange={() => toggle(item.id)}
                disabled={readOnly}
                aria-label={`Select ${item.title}`}
              />
              <div className="min-w-0">
                <a href={item.opportunity_url || "#"} target="_blank" rel="noreferrer"
                   className="line-clamp-2 font-medium hover:underline">
                  {item.title}
                </a>
                <p className="text-xs text-muted-foreground">
                  {item.source_website}
                  {item.country ? ` · ${item.country}` : ""}
                </p>
              </div>
            </li>
          ))}
        </ul>

        {!readOnly && (
          <div className="flex flex-col gap-2 border-t pt-2">
            <p className="text-xs text-muted-foreground">
              {selected.size} selected — assign to:
            </p>
            <div className="flex flex-wrap gap-1.5">
              {data.verticals.map((v) => (
                <Button key={v} size="sm" variant="outline"
                        className="h-7 px-2 text-xs"
                        disabled={busy || selected.size === 0}
                        onClick={() => void apply([v])}>
                  {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : v}
                </Button>
              ))}
              <Button size="sm" variant="ghost" className="h-7 px-2 text-xs"
                      disabled={busy || selected.size === 0}
                      onClick={() => void apply([])}>
                None of these
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
