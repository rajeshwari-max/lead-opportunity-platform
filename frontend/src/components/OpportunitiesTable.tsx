import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { ArrowUpDown, ChevronLeft, ChevronRight, ExternalLink } from "lucide-react";
import { Badge, VerticalBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { daysLeft, formatDate } from "@/lib/utils";
import type { FilterState, Opportunity, Paginated } from "@/lib/types";

const col = createColumnHelper<Opportunity>();

interface Props {
  data: Paginated | null;
  loading: boolean;
  filters: FilterState;
  onChange: (f: FilterState) => void;
}

export function OpportunitiesTable({ data, loading, filters, onChange }: Props) {
  const columns = [
    col.accessor("title", {
      header: "Title",
      cell: (info) => (
        <a href={info.row.original.opportunity_url} target="_blank" rel="noreferrer"
           className="group flex max-w-md items-start gap-1 font-medium hover:text-primary">
          <span className="line-clamp-2">{info.getValue()}</span>
          <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-100" />
        </a>
      ),
    }),
    col.accessor("organization", {
      header: "Organization",
      cell: (info) => <span className="line-clamp-2 max-w-48 text-muted-foreground">{info.getValue() || "—"}</span>,
    }),
    col.accessor("category", {
      header: "Category",
      cell: (info) => <Badge category={info.getValue()}>{info.getValue()}</Badge>,
    }),
    col.accessor("verticals", {
      header: "Vertical",
      cell: (info) => {
        const tags = (info.getValue() || "").split(",").map((s) => s.trim()).filter(Boolean);
        if (tags.length === 0) return <span className="text-xs text-muted-foreground">—</span>;
        return (
          <div className="flex max-w-44 flex-wrap gap-1">
            {tags.map((t) => (
              <VerticalBadge key={t} vertical={t} />
            ))}
          </div>
        );
      },
    }),
    col.accessor("deadline", {
      header: "Deadline",
      cell: (info) => {
        if (!info.getValue())
          return <span className="text-emerald-400">Ongoing</span>;
        const left = daysLeft(info.getValue());
        return (
          <div className="whitespace-nowrap">
            <div>{formatDate(info.getValue())}</div>
            {left != null && (
              <div className={`text-xs ${left <= 5 ? "text-red-400" : "text-muted-foreground"}`}>
                {left}d left
              </div>
            )}
          </div>
        );
      },
    }),
    col.accessor("location", {
      header: "Location",
      cell: (info) => (
        <span className="line-clamp-2 max-w-40 text-muted-foreground">
          {info.getValue() || info.row.original.country || "—"}
        </span>
      ),
    }),
    col.accessor("funding_amount", {
      header: "Amount",
      cell: (info) => <span className="whitespace-nowrap">{info.getValue() || "—"}</span>,
    }),
    col.accessor("source_website", {
      header: "Source",
      cell: (info) => <span className="whitespace-nowrap text-xs text-muted-foreground">{info.getValue()}</span>,
    }),
  ];

  const table = useReactTable({
    data: data?.items ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    manualPagination: true,
    manualSorting: true,
  });

  const sortBy = (id: string) => {
    const key = id === "source_website" ? "source_website" : id;
    onChange({
      ...filters,
      sort_by: key,
      sort_dir: filters.sort_by === key && filters.sort_dir === "asc" ? "desc" : "asc",
    });
  };

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>
          Latest Opportunities{" "}
          <span className="font-normal text-muted-foreground">
            {data ? `(${data.total} active)` : ""}
          </span>
        </CardTitle>
        {loading && <span className="text-xs text-muted-foreground">Loading…</span>}
      </CardHeader>
      <CardContent className="overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead>
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b border-border text-left">
                {hg.headers.map((h) => (
                  <th key={h.id} className="px-4 py-3 font-medium text-muted-foreground">
                    <button className="inline-flex items-center gap-1 hover:text-foreground"
                            onClick={() => sortBy(h.column.id)}>
                      {flexRender(h.column.columnDef.header, h.getContext())}
                      <ArrowUpDown className="h-3 w-3" />
                    </button>
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {loading && !data && (
              <>
                {Array.from({ length: 6 }).map((_, i) => (
                  <tr key={`sk-${i}`} className="border-b border-border/50">
                    {Array.from({ length: 8 }).map((__, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))}
              </>
            )}
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="border-b border-border/50 transition-colors hover:bg-muted/40">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-3 align-top">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-10 text-center text-muted-foreground">
                  {(() => {
                    const active: string[] = [
                      ...filters.categories,
                      ...filters.sources,
                      ...filters.verticals,
                      ...filters.countries,
                      ...filters.regions,
                    ];
                    if (filters.search) active.push(`search: "${filters.search}"`);
                    if (filters.deadline_after || filters.deadline_before) active.push("deadline range");
                    // Only category filter(s) applied -> friendly category-specific message
                    const onlyCategories =
                      filters.categories.length > 0 &&
                      active.length === filters.categories.length;
                    if (onlyCategories) {
                      const label = filters.categories.join(" / ");
                      return (
                        <span>
                          No available {label} opportunities for now — none currently have an
                          ongoing deadline.
                          <br />
                          They will appear here automatically as soon as a scrape finds current ones.
                        </span>
                      );
                    }
                    return active.length > 0 ? (
                      <span>
                        No opportunities match the combination:{" "}
                        <span className="font-medium text-foreground">{active.join(" + ")}</span>.
                        <br />
                        Filters narrow results together (AND) — untick some, or use{" "}
                        <span className="font-medium text-foreground">Clear</span> in the sidebar.
                      </span>
                    ) : (
                      <span>No opportunities in the database yet — click Start in Scraper Control.</span>
                    );
                  })()}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </CardContent>
      <div className="flex items-center justify-between border-t border-border px-4 py-3 text-sm">
        <span className="text-muted-foreground">
          Page {data?.page ?? 1} of {data?.pages ?? 1}
        </span>
        <div className="flex items-center gap-2">
          <select
            className="h-8 rounded-lg border border-border bg-card px-2 text-xs"
            value={filters.page_size}
            onChange={(e) => onChange({ ...filters, page_size: Number(e.target.value), page: 1 })}
          >
            {[10, 25, 50, 100].map((n) => (
              <option key={n} value={n}>{n} / page</option>
            ))}
          </select>
          <Button variant="outline" size="sm" disabled={(data?.page ?? 1) <= 1}
                  onClick={() => onChange({ ...filters, page: filters.page - 1 })}>
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button variant="outline" size="sm" disabled={!data || data.page >= data.pages}
                  onClick={() => onChange({ ...filters, page: filters.page + 1 })}>
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </Card>
  );
}
