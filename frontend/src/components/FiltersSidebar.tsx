import { useState } from "react";
import { FilterX, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import type { Facets, FilterState } from "@/lib/types";
import { emptyFilters } from "@/lib/types";

interface Props {
  facets: Facets | null;
  filters: FilterState;
  onChange: (f: FilterState) => void;
}

type ListKey = "categories" | "verticals" | "countries" | "regions" | "sources";

export function FiltersSidebar({ facets, filters, onChange }: Props) {
  // One search box per long list. Source Website is ~90 entries and Country is
  // 220+, so finding one by scrolling a 44px-tall box is impractical. Category,
  // Vertical and Region are short enough to read at a glance and don't get one.
  const [queries, setQueries] = useState<Record<string, string>>({});

  const toggle = (key: ListKey, value: string, checked: boolean) => {
    const current = new Set(filters[key]);
    checked ? current.add(value) : current.delete(value);
    onChange({ ...filters, [key]: [...current], page: 1 });
  };

  // Vertical sits right below Category — it's a primary filter across the app.
  const sections: {
    key: ListKey; title: string; options: string[]; searchable?: boolean;
  }[] = facets
    ? [
        { key: "categories", title: "Category", options: facets.categories },
        { key: "verticals", title: "Vertical", options: facets.verticals },
        { key: "sources", title: "Source Website", options: facets.sources, searchable: true },
        { key: "countries", title: "Country", options: facets.countries, searchable: true },
        { key: "regions", title: "Region", options: facets.regions, searchable: true },
      ]
    : [];

  /** Filter the list, but never hide something already ticked — otherwise a
   *  search would appear to silently drop an active filter that is still
   *  narrowing the results. */
  const visible = (key: ListKey, options: string[]) => {
    const q = (queries[key] || "").trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) => o.toLowerCase().includes(q) || filters[key].includes(o)
    );
  };

  return (
    <aside className="flex w-full flex-col gap-5 lg:w-60 lg:shrink-0">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Filters
        </h2>
        <Button variant="ghost" size="sm" onClick={() => onChange({ ...emptyFilters })}>
          <FilterX className="h-3.5 w-3.5" /> Clear
        </Button>
      </div>

      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-muted-foreground">Deadline</h3>
        <div className="space-y-2">
          <Input type="date" value={filters.deadline_after}
                 onChange={(e) => onChange({ ...filters, deadline_after: e.target.value, page: 1 })} />
          <Input type="date" value={filters.deadline_before}
                 onChange={(e) => onChange({ ...filters, deadline_before: e.target.value, page: 1 })} />
        </div>
      </section>

      {/* Every section renders even when it currently has no options. Hiding
          them made the sidebar look broken: picking a source whose rows carry
          no country made the whole Country and Region sections vanish, which
          reads as the filters being lost rather than as "this source has no
          countries recorded". A stable sidebar that explains itself is worth
          more than a tidy one that rearranges under you. */}
      {sections.map(
        (s) => (
            <section key={s.key} className="space-y-1">
              <div className="mb-1 flex items-baseline justify-between gap-2">
                <h3 className="text-xs font-semibold text-muted-foreground">{s.title}</h3>
                {filters[s.key].length > 0 && (
                  <button
                    onClick={() => onChange({ ...filters, [s.key]: [], page: 1 })}
                    className="text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                  >
                    clear {filters[s.key].length}
                  </button>
                )}
              </div>
              {s.searchable && (
                <div className="relative mb-1.5">
                  <Search className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
                  <input
                    type="text"
                    value={queries[s.key] || ""}
                    placeholder={`Search ${s.title.toLowerCase()}…`}
                    onChange={(e) => setQueries((q) => ({ ...q, [s.key]: e.target.value }))}
                    className="h-7 w-full rounded-md border border-border bg-background pl-7 pr-6 text-xs outline-none placeholder:text-muted-foreground focus:border-primary"
                  />
                  {(queries[s.key] || "") && (
                    <button
                      onClick={() => setQueries((q) => ({ ...q, [s.key]: "" }))}
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </div>
              )}
              <div className="max-h-44 space-y-0.5 overflow-y-auto pr-1">
                {visible(s.key, s.options).map((opt) => (
                  <Checkbox key={opt} label={opt}
                            checked={filters[s.key].includes(opt)}
                            onChange={(c) => toggle(s.key, opt, c)} />
                ))}
                {visible(s.key, s.options).length === 0 && (
                  <p className="py-1 text-xs text-muted-foreground">
                    {s.options.length === 0
                      ? "None recorded for the current selection"
                      : "No match"}
                  </p>
                )}
              </div>
            </section>
          )
      )}
    </aside>
  );
}
