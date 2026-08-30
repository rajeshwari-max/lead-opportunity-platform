import { useCallback, useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Loader2, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { UnclassifiedItem, UnclassifiedResponse } from "@/lib/types";

/** The Unclassified Opportunities section.
 *
 *  These rows passed the opportunity gate and are actionable, but no vertical
 *  could be derived — so they are excluded from the main table on purpose, and
 *  this is the only place they can be reached. A third of the database sits
 *  here, not because anyone judged it irrelevant but because the keyword rules
 *  had nothing to say about it.
 *
 *  Everything is server-side: search, filters, paging and select-all. Select-all
 *  has to mean "every row this filter matches", not "the 25 on screen" — a bulk
 *  action scoped to the visible page silently does a fraction of what was asked.
 *
 *  Searching never assigns anything. It narrows the list; assignment happens
 *  only when the reviewer presses a vertical.
 */
const EMPTY = {
  search: "", sources: [] as string[], countries: [] as string[],
  categories: [] as string[], page: 1,
};

export function UnclassifiedView({ onClose, readOnly = false }:
  { onClose: () => void; readOnly?: boolean }) {
  const [q, setQ] = useState(EMPTY);
  const [term, setTerm] = useState("");
  const [data, setData] = useState<UnclassifiedResponse | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await api.unclassified(q));
      setNote("");
    } catch {
      setNote("Could not load the section.");
    }
  }, [q]);

  useEffect(() => { void load(); }, [load]);

  const submitSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setSelected(new Set());
    setQ((p) => ({ ...p, search: term, page: 1 }));
  };

  const selectAllMatching = async () => {
    const r = await api.unclassifiedIds(q);
    setSelected(new Set(r.ids));
    // Said plainly rather than silently truncating: the write path refuses
    // more than the cap, so offering a larger selection would set up a failure.
    setNote(r.capped ? r.note : `${r.ids.length} selected across every page.`);
  };

  const apply = async (verticals: string[]) => {
    if (selected.size === 0) { setNote("Select at least one opportunity first."); return; }
    setBusy(true);
    try {
      await api.assignVerticals([...selected], verticals);
      setSelected(new Set());
      await load();
      setNote(verticals.length
        ? `Assigned ${verticals.join(" + ")}. Those rows now appear in the main table.`
        : "Marked as none of the six. They stay out of the working view and will not be re-tagged.");
    } catch {
      setNote("That assignment could not be saved.");
    } finally { setBusy(false); }
  };

  const items: UnclassifiedItem[] = data?.items ?? [];
  const sources = (data?.by_source ?? []).slice(0, 12);

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-background p-4 sm:p-6">
      <div className="mx-auto flex max-w-[1400px] flex-col gap-4">
        <header className="flex flex-wrap items-center gap-3 border-b pb-3">
          <h2 className="text-lg font-semibold">Unclassified Opportunities</h2>
          <span className="rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-semibold text-indigo-800">
            {(data?.total ?? 0).toLocaleString()} matching
            {data && data.total !== data.unfiltered_total &&
              ` of ${data.unfiltered_total.toLocaleString()}`}
          </span>
          <Button variant="ghost" size="sm" className="ml-auto" onClick={onClose}>
            <X className="h-4 w-4" /><span className="ml-1">Close</span>
          </Button>
        </header>

        <p className="text-sm text-muted-foreground">
          Actionable opportunities with no vertical. They are kept out of the main
          table until someone decides. Searching only narrows this list — nothing
          is assigned until you press a vertical below.
        </p>

        <form onSubmit={submitSearch} className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[16rem] flex-1">
            <Search className="absolute left-2 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
            <Input className="pl-7" placeholder="Search title, summary, organisation, country…"
                   value={term} onChange={(e) => setTerm(e.target.value)} />
          </div>
          <Button type="submit" size="sm">Search</Button>
          {q.search && (
            <Button type="button" size="sm" variant="ghost"
                    onClick={() => { setTerm(""); setQ({ ...EMPTY }); }}>
              Clear
            </Button>
          )}
        </form>

        <div className="flex flex-wrap gap-1.5">
          {sources.map((s) => {
            const on = q.sources.includes(s.source_website);
            return (
              <button key={s.source_website}
                      onClick={() => { setSelected(new Set()); setQ((p) => ({
                        ...p, page: 1,
                        sources: on ? p.sources.filter((x) => x !== s.source_website)
                                    : [...p.sources, s.source_website] })); }}
                      className={`rounded-full border px-2.5 py-0.5 text-xs ${
                        on ? "border-indigo-500 bg-indigo-50 text-indigo-800"
                           : "border-border text-muted-foreground hover:bg-muted"}`}>
                {s.source_website} <b>{s.count.toLocaleString()}</b>
              </button>
            );
          })}
        </div>

        {note && <p className="text-xs text-indigo-700">{note}</p>}

        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">{selected.size} selected</span>
          <Button size="sm" variant="outline" className="h-7 text-xs"
                  onClick={() => void selectAllMatching()}>
            Select all matching
          </Button>
          {selected.size > 0 && (
            <Button size="sm" variant="ghost" className="h-7 text-xs"
                    onClick={() => setSelected(new Set())}>Clear selection</Button>
          )}
        </div>

        <div className="overflow-x-auto rounded-md border">
          <table className="w-full min-w-[900px] text-sm">
            <thead className="bg-muted/50 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="w-8 p-2"></th>
                <th className="p-2">Opportunity</th>
                <th className="p-2">Source</th>
                <th className="p-2">Country</th>
                <th className="p-2">Deadline</th>
                <th className="p-2">Suggested</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className="border-t align-top">
                  <td className="p-2">
                    <input type="checkbox" className="mt-1 h-3.5 w-3.5"
                           checked={selected.has(item.id)} disabled={readOnly}
                           aria-label={`Select ${item.title}`}
                           onChange={() => setSelected((prev) => {
                             const n = new Set(prev);
                             n.has(item.id) ? n.delete(item.id) : n.add(item.id);
                             return n;
                           })} />
                  </td>
                  <td className="p-2">
                    <a href={item.opportunity_url || "#"} target="_blank" rel="noreferrer"
                       className="font-medium hover:underline">{item.title}</a>
                    <p className="text-xs text-muted-foreground">{item.organization}</p>
                  </td>
                  <td className="p-2 text-xs">{item.source_website}</td>
                  <td className="p-2 text-xs">{item.country || "—"}</td>
                  <td className="p-2 text-xs tabular-nums">{item.deadline || "—"}</td>
                  <td className="p-2">
                    {item.suggestions?.length ? item.suggestions.map((s) => (
                      <div key={s.vertical} className="text-xs">
                        <span className="font-medium">{s.vertical}</span>{" "}
                        <span className="tabular-nums text-muted-foreground">
                          {s.score.toFixed(2)}
                        </span>
                        {/* Why the model leaned that way. A bare number gives a
                            reviewer nothing to agree or disagree with. */}
                        {s.evidence?.length > 0 && (
                          <span className="ml-1 font-mono text-[10px] text-muted-foreground">
                            {s.evidence.slice(0, 2).join(" · ")}
                          </span>
                        )}
                      </div>
                    )) : <span className="text-xs text-muted-foreground">no signal</span>}
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan={6} className="p-6 text-center text-muted-foreground">
                  Nothing matches this filter.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="flex items-center gap-2 text-sm">
          <Button size="sm" variant="outline" className="h-7"
                  disabled={(data?.page ?? 1) <= 1}
                  onClick={() => setQ((p) => ({ ...p, page: p.page - 1 }))}>
            <ChevronLeft className="h-3 w-3" />
          </Button>
          <span className="text-xs text-muted-foreground">
            Page {data?.page ?? 1} of {data?.pages ?? 1}
          </span>
          <Button size="sm" variant="outline" className="h-7"
                  disabled={(data?.page ?? 1) >= (data?.pages ?? 1)}
                  onClick={() => setQ((p) => ({ ...p, page: p.page + 1 }))}>
            <ChevronRight className="h-3 w-3" />
          </Button>
        </div>

        {!readOnly && (
          <div className="sticky bottom-0 flex flex-wrap items-center gap-1.5 border-t bg-background py-3">
            <span className="mr-1 text-xs text-muted-foreground">Assign to:</span>
            {(data?.verticals ?? []).map((v) => (
              <Button key={v} size="sm" variant="outline" className="h-7 text-xs"
                      disabled={busy || selected.size === 0}
                      onClick={() => void apply([v])}>
                {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : v}
              </Button>
            ))}
            <Button size="sm" variant="ghost" className="h-7 text-xs"
                    disabled={busy || selected.size === 0}
                    onClick={() => void apply([])}>
              None of the six
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
