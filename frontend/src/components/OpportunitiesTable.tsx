import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { Fragment, useEffect, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  Check,
  ExternalLink,
  Search,
  Undo2,
} from "lucide-react";
import { Badge, VerticalBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MultiSelect } from "@/components/ui/multi-select";
import { SendSelectionBar } from "@/components/SendSelectionBar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { daysLeft, formatDate } from "@/lib/utils";
import { RATES_AS_OF, toInr } from "@/lib/money";
import type { Facets, FilterState, Opportunity, Paginated } from "@/lib/types";

const col = createColumnHelper<Opportunity>();

/** Table layout preferences live in the browser, not the server: how wide the
 *  Title column should be is a personal reading choice, and storing it centrally
 *  would have two people overwriting each other's view. */
function loadPref<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}
function savePref(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private browsing / quota — layout simply won't persist */
  }
}

interface Props {
  data: Paginated | null;
  loading: boolean;
  filters: FilterState;
  onChange: (f: FilterState) => void;
  /** Distinct values powering the Source and Type dropdowns in the toolbar. */
  facets: Facets | null;
  /** The public mirror can be read but not changed — approving is disabled there. */
  readOnly?: boolean;
}

export function OpportunitiesTable({ data, loading, filters, onChange, facets, readOnly = false }: Props) {
  // Approvals are tracked locally so the button responds on click rather than
  // after a refetch of the whole page. Keyed by id and merged over the server
  // value, so it survives re-renders but never masks a fresh fetch of a row
  // someone else approved.
  const [pendingApproval, setPendingApproval] = useState<Record<number, boolean>>({});
  const [failedApproval, setFailedApproval] = useState<number | null>(null);

  // Which rows are open. A Set of ids rather than a flag on the row, because
  // the row objects are replaced on every refetch and a flag would be lost.
  const [showInr, setShowInr] = useState<boolean>(() => loadPref("lop-show-inr", false));
  useEffect(() => savePref("lop-show-inr", showInr), [showInr]);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const toggleSelected = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggleExpanded = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const toggleApproval = async (o: Opportunity) => {
    const next = !(pendingApproval[o.id] ?? o.approved);
    setPendingApproval((m) => ({ ...m, [o.id]: next }));
    setFailedApproval(null);
    try {
      await api.approve(o.id, next);
    } catch {
      // Put the row back where it was; a button that stays "Approved" after a
      // failed write is worse than no button at all.
      setPendingApproval((m) => {
        const { [o.id]: _dropped, ...rest } = m;
        return rest;
      });
      setFailedApproval(o.id);
    }
  };

  const pageIds = (data?.items ?? []).map((o) => o.id);
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  const columns = [
    // Selection lives in its own narrow column rather than on the title cell,
    // so ticking a row never risks following the title link by accident.
    col.display({
      id: "select",
      size: 44,
      enableResizing: false,
      header: () => (
        <input
          type="checkbox"
          checked={allOnPageSelected}
          title={allOnPageSelected ? "Deselect this page" : "Select every row on this page"}
          onChange={(e) =>
            setSelected((prev) => {
              const next = new Set(prev);
              // Only this page. Selecting across pages silently would let
              // someone email thousands of rows believing they picked twenty.
              pageIds.forEach((id) => (e.target.checked ? next.add(id) : next.delete(id)));
              return next;
            })
          }
          className="h-4 w-4 rounded accent-[hsl(var(--primary))]"
        />
      ),
      cell: (info) => (
        <input
          type="checkbox"
          checked={selected.has(info.row.original.id)}
          onChange={() => toggleSelected(info.row.original.id)}
          className="h-4 w-4 rounded accent-[hsl(var(--primary))]"
        />
      ),
    }),
    col.accessor("title", {
      header: "Title",
      size: 300,
      // title={} gives the full text on hover — titles are routinely longer
      // than two lines and were previously unreadable once clamped.
      cell: (info) => {
        const { website, source_website } = info.row.original;
        // Some sources put every listing behind their own login. The link is
        // correct, but an unauthenticated visitor lands on a sign-in page and
        // reasonably concludes the link is broken. Saying so first costs a
        // second and stops that conclusion.
        const needsLogin = /developmentaid|devex|globaltenders/i.test(source_website || "");
        const warnThenOpen = (e: React.MouseEvent, url: string) => {
          if (!needsLogin) return;
          e.preventDefault();
          const go = window.confirm(
            `${source_website} shows this opportunity only to signed-in members.\n\n` +
            "If you aren't logged in there, you'll land on their sign-in page — " +
            "log in, then this link will open the opportunity.\n\nOpen it now?"
          );
          if (go) window.open(url, "_blank", "noopener");
        };
        // Some sources never publish a per-call URL, and some older rows had a
        // Every row is clickable. Where we have no link to the listing itself,
        // the backend supplies a search on the source site that will find it
        // (link_kind === "search"). The label says which it is — a search
        // presented as the listing would cost more trust than the dead end it
        // replaces, but a dead end costs the reader the manual lookup this
        // tool exists to remove.
        const { link, link_kind } = info.row.original;
        const isSearch = link_kind === "search";
        // "none" — we have nothing to open for this row. The backend no longer
        // invents a web search when a row has no real URL (that is what made
        // entries open a search engine instead of the call), and it no longer
        // stores such rows at all. Existing ones are removed by
        // scripts/clean_dashboard.py, so this should be transient — but while
        // any remain, saying "no link" is the honest label. Without this branch
        // they fall through to isDirect and are presented as a direct link to
        // the call while actually opening the funder's homepage.
        const isNone = link_kind === "none" || !link;
        // "listing" is the case that made links feel wrong: the URL is real and
        // belongs to the funder, but it is a section index ("/funding",
        // "/apply-for-a-grant") rather than this call's own page. Labelled
        // "Open the original listing" it read as a link to the wrong
        // opportunity. Saying which kind it is costs nothing and is true.
        const isListing = link_kind === "listing";
        const isDirect = !isSearch && !isListing && !isNone;
        return (
          <div>
            <a href={link || website} target="_blank" rel="noreferrer"
               title={isNone
                 ? `${source_website} published no link for this one — nothing to open`
                 : isSearch
                 ? `No direct link published for this one — opens a search on ${source_website}`
                 : isListing
                   ? `${source_website} publishes no page for this call on its own — opens the funder's listing page, where you'll need to find the row`
                   : info.getValue()}
               onClick={(e) => warnThenOpen(e, link || website)}
               className="group flex items-start gap-1 font-medium underline-offset-2 hover:text-primary hover:underline">
              <span className="line-clamp-2">{info.getValue()}</span>
              {isSearch
                ? <Search className="mt-0.5 h-3 w-3 shrink-0 opacity-60" />
                : <ExternalLink className={`mt-0.5 h-3 w-3 shrink-0 transition-opacity ${
                    isListing ? "opacity-60" : "opacity-0 group-hover:opacity-100"}`} />}
            </a>
            {isSearch && (
              <span className="text-[11px] text-muted-foreground">
                opens a search on {source_website}
              </span>
            )}
            {isNone && (
              <span className="text-[11px] text-muted-foreground">
                no link published — nothing to open
              </span>
            )}
            {isListing && (
              <span className="text-[11px] text-muted-foreground">
                opens the funder's listing page — find the row there
              </span>
            )}
            {isDirect && needsLogin && (
              <span className="text-[11px] text-muted-foreground">sign-in required</span>
            )}
          </div>
        );
      },
    }),
    col.accessor("organization", {
      header: "Organization",
      size: 150,
      cell: (info) => (
        <span title={info.getValue() || undefined}
              className="line-clamp-2 text-muted-foreground">
          {info.getValue() || "—"}
        </span>
      ),
    }),
    // Directly after Organization: the two are read together — "who is funding
    // this, and which board did we find it on" — and having Source at the far
    // right meant scrolling away from the funder to answer the second half.
    col.accessor("source_website", {
      header: "Source",
      size: 130,
      cell: (info) => (
        <span title={info.getValue() || undefined}
              className="block truncate text-xs text-muted-foreground">
          {info.getValue() || "—"}
        </span>
      ),
    }),
    // One column, stacked: Research/Implementation sits on top of Grant/RFP.
    // A separate Work Type column cost horizontal space the table didn't have
    // and pushed the Approve button off-screen. The routing decision still
    // reads first because it is physically above the category.
    col.accessor("category", {
      header: "Type",
      size: 130,
      cell: (info) => {
        const { work_type: wt, study_type: study } = info.row.original;
        const tone =
          wt === "Research" ? "bg-violet-500/15 text-violet-400 ring-violet-500/30"
          : "bg-sky-500/15 text-sky-400 ring-sky-500/30";
        return (
          <div className="space-y-1 whitespace-nowrap">
            {wt && (
              <div>
                <span className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ${tone}`}>
                  {wt}
                </span>
              </div>
            )}
            <div>
              <Badge category={info.getValue()}>{info.getValue()}</Badge>
            </div>
            {/* Only ever present on research work, so it belongs here rather
                than in a column of its own that would be empty most rows. */}
            {study && <div className="text-[11px] text-muted-foreground">{study}</div>}
          </div>
        );
      },
    }),
    col.accessor("verticals", {
      header: "Vertical",
      size: 160,
      cell: (info) => {
        const tags = (info.getValue() || "").split(",").map((s) => s.trim()).filter(Boolean);
        if (tags.length === 0) return <span className="text-xs text-muted-foreground">—</span>;
        return (
          <div className="flex flex-wrap gap-1">
            {tags.map((t) => (
              <VerticalBadge key={t} vertical={t} />
            ))}
          </div>
        );
      },
    }),
    col.accessor("deadline", {
      header: "Deadline",
      size: 120,
      cell: (info) => {
        if (!info.getValue())
          return <span className="text-emerald-400">Ongoing</span>;
        const left = daysLeft(info.getValue());
        // Graded urgency rather than a single red-at-5-days rule: a fortnight's
        // notice on a proposal is already tight, and that deserves a signal.
        const tone =
          left == null ? "text-muted-foreground"
          : left < 0 ? "text-muted-foreground"
          : left <= 3 ? "text-red-500 font-semibold"
          : left <= 7 ? "text-orange-500 font-medium"
          : left <= 14 ? "text-amber-500"
          : "text-muted-foreground";
        return (
          <div className="whitespace-nowrap">
            <div>{formatDate(info.getValue())}</div>
            {left != null && (
              <div className={`text-xs ${tone}`}>
                {left < 0 ? "closed" : left === 0 ? "closes today" : `${left}d left`}
              </div>
            )}
          </div>
        );
      },
    }),
    // Shows the normalised country, not the raw `location` string. Those two
    // disagree constantly — the listing publishes "UK", "USA" or a 30-country
    // list, while the Country filter offers "United Kingdom". Rendering the raw
    // string made the column look wrong and unmatchable against the filter, so
    // the canonical country leads and the full published text is on hover.
    col.accessor("country", {
      header: "Country",
      size: 150,
      cell: (info) => {
        const country = info.getValue();
        const { location, region } = info.row.original;
        if (!country && !location) return <span className="text-muted-foreground">—</span>;
        const multi = country && location && /[,;]/.test(location);
        // whitespace-nowrap: "United States" over "North America" was wrapping
        // into five stacked words in a narrow column, which read as broken
        // rather than as two fields. The table already scrolls horizontally.
        return (
          <div className="min-w-0" title={location || country || undefined}>
            <div className="truncate">
              {country || location}
              {multi && <span className="ml-1 text-xs text-muted-foreground">+ others</span>}
            </div>
            {region && <div className="truncate text-xs text-muted-foreground">{region}</div>}
          </div>
        );
      },
    }),
    col.accessor("funding_amount", {
      header: "Amount",
      size: 150,
      // No whitespace-nowrap. Under table-fixed a long amount like
      // "$905,664 – $1,188,684" cannot shrink, so it ran straight over the
      // Approve button in the next column. Wrapping keeps it inside its cell;
      // break-words handles a single token wider than the column.
      cell: (info) => {
        const raw = info.getValue();
        const inr = showInr ? toInr(raw) : "";
        return (
          <div className="min-w-0">
            <span className="block break-words leading-snug">{raw || "—"}</span>
            {inr && (
              <span
                className="mt-0.5 block break-words text-[11px] leading-snug text-muted-foreground"
                title={`Indicative only, converted at ${RATES_AS_OF} rates`}
              >
                {inr}
              </span>
            )}
          </div>
        );
      },
    }),
    col.display({
      id: "approve",
      header: "Approve",
      size: 110,
      cell: (info) => {
        const o = info.row.original;
        const approved = pendingApproval[o.id] ?? o.approved;
        if (readOnly) {
          return approved ? (
            <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs font-medium text-emerald-500">
              <Check className="h-3.5 w-3.5" /> Approved
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">—</span>
          );
        }
        return (
          <div className="whitespace-nowrap">
            <Button
              size="sm"
              variant={approved ? "default" : "outline"}
              onClick={() => toggleApproval(o)}
              title={
                approved
                  ? `Approved${o.approved_by ? ` by ${o.approved_by}` : ""}${
                      o.approved_at ? ` on ${formatDate(o.approved_at)}` : ""
                    }`
                  : "Approve this opportunity"
              }
              className={approved ? "h-7 bg-emerald-600 px-2 text-xs hover:bg-emerald-700" : "h-7 px-2 text-xs"}
            >
              {approved ? (
                <>
                  <Check className="mr-1 h-3.5 w-3.5" /> Approved
                </>
              ) : (
                "Approve"
              )}
            </Button>
            {/* Undo was previously only discoverable by guessing that the green
                button toggles. A mis-click needs a way out that is visible
                without hovering. */}
            {approved && (
              <button
                type="button"
                onClick={() => toggleApproval(o)}
                className="mt-1 flex items-center gap-1 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
              >
                <Undo2 className="h-3 w-3" /> Undo
              </button>
            )}
            {failedApproval === o.id && (
              <div className="mt-1 text-xs text-red-500">Couldn't save</div>
            )}
          </div>
        );
      },
    }),
  ];

  // --- adjustable table -----------------------------------------------------
  // Column widths, which columns are shown and how tall the rows are, all kept
  // in localStorage. This is a per-person reading preference: someone scanning
  // titles wants a wide Title column and no Source; someone reconciling funders
  // wants the opposite. A single fixed layout cannot serve both.
  const [sizing, setSizing] = useState<Record<string, number>>(
    () => loadPref("lop-col-sizing", {} as Record<string, number>)
  );
  useEffect(() => savePref("lop-col-sizing", sizing), [sizing]);

  const table = useReactTable({
    data: data?.items ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    columnResizeMode: "onChange",
    // 70px floor: a column dragged to zero is unrecoverable without
    // clearing localStorage, since there is no edge left to grab.
    defaultColumn: { minSize: 70 },
    enableColumnResizing: true,
    state: { columnSizing: sizing },
    onColumnSizingChange: (updater) =>
      setSizing((cur) => (typeof updater === "function" ? updater(cur) : cur)),
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
      <CardHeader className="flex-row items-center justify-between gap-3">
        <CardTitle className="flex items-baseline gap-2">
          Latest Opportunities
          <span className="text-sm font-normal text-muted-foreground">
            {data ? `${data.total.toLocaleString()} active` : ""}
          </span>
          {loading && (
            <span className="text-xs font-normal text-muted-foreground">updating…</span>
          )}
        </CardTitle>
        <div className="flex flex-wrap items-center gap-1.5">
          {/* Source and Type filter right above the table, where the eye already
              is. The sidebar carries the same two filters and stays in sync —
              both write the same FilterState, so picking a source here ticks
              its sidebar checkbox and vice versa. */}
          <MultiSelect
            label="Source"
            options={facets?.sources ?? []}
            selected={filters.sources}
            onChange={(sources) => onChange({ ...filters, sources, page: 1 })}
          />
          <MultiSelect
            label="Type"
            options={facets?.categories ?? []}
            selected={filters.categories}
            onChange={(categories) => onChange({ ...filters, categories, page: 1 })}
          />
          <Button
            size="sm"
            variant={showInr ? "default" : "outline"}
            onClick={() => setShowInr((v) => !v)}
            title={`Show an approximate INR equivalent under each amount (${RATES_AS_OF} rates, indicative only)`}
            className={showInr ? "h-8 bg-amber-600 text-xs hover:bg-amber-700" : "h-8 text-xs"}
          >
            ₹ INR
          </Button>
          {/* Approving is only useful if the approved set can be read back — this
              is how you review what the team has signed off. */}
          <Button
            size="sm"
            variant={filters.approved ? "default" : "outline"}
            onClick={() => onChange({ ...filters, approved: !filters.approved, page: 1 })}
            className={filters.approved ? "h-8 bg-emerald-600 text-xs hover:bg-emerald-700" : "h-8 text-xs"}
          >
            <Check className="mr-1 h-3.5 w-3.5" />
            {filters.approved ? "Showing approved" : "Approved only"}
          </Button>
        </div>
      </CardHeader>
      <SendSelectionBar
        selectedIds={[...selected]}
        onClear={() => setSelected(new Set())}
      />
      <CardContent className="overflow-x-auto p-0">
        {/* table-fixed makes the browser honour the widths set on each
                header instead of auto-sizing columns to their content —
                without it, dragging a column changes nothing visible. */}
        <table className="w-full table-fixed text-sm" style={{ width: table.getTotalSize() }}>
          {/* Sticky header: the table runs to 100 rows a page, and the column
              labels used to scroll out of sight. */}
          <thead className="sticky top-0 z-10">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b border-border text-left">
                {hg.headers.map((h) => {
                  // Show WHICH column is sorted and in which direction. Every
                  // header previously rendered the same neutral icon, so the
                  // current sort was invisible.
                  const isSorted = filters.sort_by === h.column.id;
                  const Icon = !isSorted
                    ? ArrowUpDown
                    : filters.sort_dir === "asc" ? ArrowUp : ArrowDown;
                  return (
                    <th key={h.id}
                        style={{ width: h.getSize() }}
                        className="group relative bg-card px-4 py-3 font-medium text-muted-foreground">
                      <button
                        className={`inline-flex items-center gap-1 transition-colors hover:text-foreground ${
                          isSorted ? "text-foreground" : ""}`}
                        title={`Sort by ${String(h.column.columnDef.header)}`}
                        onClick={() => sortBy(h.column.id)}>
                        {flexRender(h.column.columnDef.header, h.getContext())}
                        <Icon className={`h-3 w-3 ${isSorted ? "text-primary" : "opacity-50"}`} />
                      </button>
                      {/* Drag to resize. The handlers come from the table
                          instance so the width tracks the pointer rather than
                          snapping on release. */}
                      {h.column.getCanResize() && (
                        <span
                          onMouseDown={h.getResizeHandler()}
                          onTouchStart={h.getResizeHandler()}
                          onDoubleClick={() => h.column.resetSize()}
                          title="Drag to resize \u00b7 double-click to reset"
                          className={`absolute right-0 top-0 h-full w-1.5 cursor-col-resize touch-none select-none rounded transition-colors hover:bg-primary/40 ${
                            h.column.getIsResizing() ? "bg-primary" : "bg-transparent"}`}
                        />
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {loading && !data && (
              <>
                {Array.from({ length: 6 }).map((_, i) => (
                  <tr key={`sk-${i}`} className="border-b border-border/50">
                    {Array.from({ length: table.getVisibleFlatColumns().length }).map((__, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))}
              </>
            )}
            {table.getRowModel().rows.map((row) => {
              const o = row.original;
              const isOpen = expanded.has(o.id);
              return (
                <Fragment key={row.id}>
                  <tr
                    // A left accent bar on hover makes it obvious which row the
                    // cursor is on across eight columns of dense text.
                    className={`group cursor-pointer border-b border-border/50 transition-colors hover:bg-primary/[0.04] ${
                      isOpen ? "bg-primary/[0.06]" : ""
                    }`}
                    // Clicking anywhere on the row expands it, except on a link
                    // or a button — the title link and Approve must keep doing
                    // their own job rather than opening a panel.
                    onClick={(e) => {
                      const el = e.target as HTMLElement;
                      if (el.closest("a,button,input,label")) return;
                      toggleExpanded(o.id);
                    }}
                  >
                    {row.getVisibleCells().map((cell, ci) => (
                      <td
                        key={cell.id}
                        className={`px-4 align-top py-3 ${
                          ci === 0
                            ? "relative before:absolute before:inset-y-0 before:left-0 before:w-0.5 before:bg-primary before:opacity-0 before:transition-opacity group-hover:before:opacity-100"
                            : ""
                        }`}
                      >
                        {ci === 1 ? (
                          <div className="flex items-start gap-1.5">
                            <ChevronRight
                              className={`mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform ${
                                isOpen ? "rotate-90" : ""
                              }`}
                            />
                            <div className="min-w-0 flex-1">
                              {flexRender(cell.column.columnDef.cell, cell.getContext())}
                            </div>
                          </div>
                        ) : (
                          flexRender(cell.column.columnDef.cell, cell.getContext())
                        )}
                      </td>
                    ))}
                  </tr>
                  {isOpen && (
                    <tr className="border-b border-border/50 bg-muted/30">
                      <td colSpan={row.getVisibleCells().length} className="px-4 py-4">
                        <div className="space-y-3 text-sm">
                          {/* The full title, unclamped. In the row it is capped
                              at two lines, which cuts most of these in half. */}
                          <div>
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                              Title
                            </p>
                            <p className="mt-0.5 font-medium leading-snug">{o.title}</p>
                          </div>

                          {o.summary && (
                            <div>
                              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                                Summary
                              </p>
                              <p className="mt-0.5 whitespace-pre-line leading-relaxed text-muted-foreground">
                                {o.summary}
                              </p>
                            </div>
                          )}

                          {o.eligibility && (
                            <div>
                              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                                Eligibility
                              </p>
                              <p className="mt-0.5 whitespace-pre-line leading-relaxed text-muted-foreground">
                                {o.eligibility}
                              </p>
                            </div>
                          )}

                          <div className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-muted-foreground">
                            {o.organization && <span><b className="font-semibold text-foreground">Organisation:</b> {o.organization}</span>}
                            {o.source_website && <span><b className="font-semibold text-foreground">Source:</b> {o.source_website}</span>}
                            {o.location && <span><b className="font-semibold text-foreground">Location:</b> {o.location}</span>}
                            {o.country && <span><b className="font-semibold text-foreground">Country:</b> {o.country}</span>}
                            {o.region && <span><b className="font-semibold text-foreground">Region:</b> {o.region}</span>}
                            {o.funding_amount && (
                              <span>
                                <b className="font-semibold text-foreground">Amount:</b> {o.funding_amount}
                                {toInr(o.funding_amount) && (
                                  <span className="ml-1 opacity-80">({toInr(o.funding_amount)})</span>
                                )}
                              </span>
                            )}
                            {o.work_type && <span><b className="font-semibold text-foreground">Work type:</b> {o.work_type}</span>}
                            {o.study_type && <span><b className="font-semibold text-foreground">Study:</b> {o.study_type}</span>}
                            <span>
                              <b className="font-semibold text-foreground">Deadline:</b>{" "}
                              {o.deadline ? formatDate(o.deadline) : "Ongoing"}
                            </span>
                          </div>

                          {o.link && (
                            <a
                              href={o.link}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                            >
                              {o.link_kind === "search"
                                ? `Search ${o.source_website} for this`
                                : o.link_kind === "listing"
                                  ? "Open the funder's listing page"
                                  : "Open the original listing"} <ExternalLink className="h-3 w-3" />
                            </a>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={table.getVisibleFlatColumns().length}
                    className="px-4 py-10 text-center text-muted-foreground">
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
        {/* "Showing 26–50 of 9,416" is far more useful than a bare page number
            when the result set runs to thousands of rows. */}
        <span className="text-muted-foreground">
          {data && data.total > 0 ? (
            <>
              Showing{" "}
              <span className="font-medium text-foreground">
                {((data.page - 1) * filters.page_size + 1).toLocaleString()}–
                {Math.min(data.page * filters.page_size, data.total).toLocaleString()}
              </span>{" "}
              of {data.total.toLocaleString()}
            </>
          ) : (
            `Page ${data?.page ?? 1} of ${data?.pages ?? 1}`
          )}
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
