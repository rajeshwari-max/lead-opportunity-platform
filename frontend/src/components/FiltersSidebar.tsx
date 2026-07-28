import { FilterX } from "lucide-react";
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
  const toggle = (key: ListKey, value: string, checked: boolean) => {
    const current = new Set(filters[key]);
    checked ? current.add(value) : current.delete(value);
    onChange({ ...filters, [key]: [...current], page: 1 });
  };

  // Vertical sits right below Category — it's a primary filter across the app.
  const sections: { key: ListKey; title: string; options: string[] }[] = facets
    ? [
        { key: "categories", title: "Category", options: facets.categories },
        { key: "verticals", title: "Vertical", options: facets.verticals },
        { key: "sources", title: "Source Website", options: facets.sources },
        { key: "countries", title: "Country", options: facets.countries },
        { key: "regions", title: "Region", options: facets.regions },
      ]
    : [];

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

      {sections.map(
        (s) =>
          s.options.length > 0 && (
            <section key={s.key} className="space-y-1">
              <h3 className="mb-1 text-xs font-semibold text-muted-foreground">{s.title}</h3>
              <div className="max-h-44 space-y-0.5 overflow-y-auto pr-1">
                {s.options.map((opt) => (
                  <Checkbox key={opt} label={opt}
                            checked={filters[s.key].includes(opt)}
                            onChange={(c) => toggle(s.key, opt, c)} />
                ))}
              </div>
            </section>
          )
      )}
    </aside>
  );
}
