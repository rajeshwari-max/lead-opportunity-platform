import { motion } from "framer-motion";
import { Activity, Award, FileText, Gavel, Sparkles } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { FilterState, Stats } from "@/lib/types";

// Each card carries its own accent — icon tint, a soft icon plate, a top rule
// and the ring shown when it's the active filter. Previously only the icon was
// coloured, which left five near-identical grey cards and no visual anchor.
const cards = [
  { key: "total", label: "Active Opportunities", icon: Activity,
    color: "text-primary", plate: "bg-primary/10", rule: "bg-primary",
    ring: "border-primary/60 ring-primary/25" },
  { key: "Grant", label: "Grants", icon: Award,
    color: "text-emerald-500", plate: "bg-emerald-500/10", rule: "bg-emerald-500",
    ring: "border-emerald-500/60 ring-emerald-500/25" },
  { key: "RFP", label: "RFPs", icon: FileText,
    color: "text-indigo-500", plate: "bg-indigo-500/10", rule: "bg-indigo-500",
    ring: "border-indigo-500/60 ring-indigo-500/25" },
  { key: "Tender", label: "Tenders", icon: Gavel,
    color: "text-amber-500", plate: "bg-amber-500/10", rule: "bg-amber-500",
    ring: "border-amber-500/60 ring-amber-500/25" },
  { key: "today", label: "New Today", icon: Sparkles,
    color: "text-fuchsia-500", plate: "bg-fuchsia-500/10", rule: "bg-fuchsia-500",
    ring: "border-fuchsia-500/60 ring-fuchsia-500/25" },
] as const;

interface Props {
  stats: Stats | null;
  loading?: boolean;
  filters: FilterState;
  onChange: (f: FilterState) => void;
}

/** Scrolls the user down to the opportunities table (same page — no navigation). */
export function scrollToOpportunities() {
  document.getElementById("opportunities-table")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/**
 * Dashboard stat cards.
 * Responsive grid: 1/row (mobile) → 2/row (tablet) → 3+2 (laptop) → 5/row (desktop).
 * Clicking a card applies the matching filter and navigates to the table.
 */
export function StatCards({ stats, loading, filters, onChange }: Props) {
  const value = (key: string): number => {
    if (!stats) return 0;
    if (key === "total") return stats.total_active;
    if (key === "today") return stats.todays_new;
    return stats.by_category[key] ?? 0;
  };

  const navigate = (key: string) => {
    if (key === "total") {
      onChange({ ...filters, categories: [], new_today: false, page: 1 });
    } else if (key === "today") {
      // Clicking a card that reads "240" must show those 240. This used to only
      // change the sort order, so the table looked unchanged and the card
      // appeared broken. Clicking again clears it.
      onChange({
        ...filters,
        new_today: !filters.new_today,
        sort_by: "date_scraped",
        sort_dir: "desc",
        page: 1,
      });
    } else {
      onChange({
        ...filters,
        categories: filters.categories[0] === key ? [] : [key],
        new_today: false,
        page: 1,
      });
    }
    scrollToOpportunities();
  };

  const isActive = (key: string) => {
    if (key === "today") return filters.new_today;
    if (key === "total") return filters.categories.length === 0 && !filters.new_today;
    return filters.categories.length === 1 && filters.categories[0] === key;
  };

  if (loading && !stats) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-5">
        {cards.map((c) => (
          <Skeleton key={c.key} className="h-[86px]" />
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-5">
      {cards.map((c, i) => (
        <motion.div
          key={c.key}
          className="h-full"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: i * 0.06 }}
        >
          <Card
            role="button"
            tabIndex={0}
            aria-label={`Show ${c.label} in the opportunities table`}
            onClick={() => navigate(c.key)}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && navigate(c.key)}
            className={`group relative h-full cursor-pointer overflow-hidden transition-all duration-200 hover:-translate-y-0.5 hover:shadow-lg ${
              isActive(c.key) ? `${c.ring} ring-1 shadow-sm` : "hover:border-border/80"
            }`}
          >
            {/* Accent rule along the top — fills in when the card is the active
                filter, so the current selection is obvious at a glance. */}
            <span
              className={`absolute inset-x-0 top-0 h-0.5 ${c.rule} transition-opacity duration-200 ${
                isActive(c.key) ? "opacity-100" : "opacity-0 group-hover:opacity-60"
              }`}
            />
            <CardContent className="flex h-full items-center justify-between p-5">
              <div className="min-w-0">
                <p className="truncate text-xs font-medium text-muted-foreground">{c.label}</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">
                  {value(c.key).toLocaleString()}
                </p>
                {isActive(c.key) && (
                  <p className="mt-0.5 text-[10px] font-medium text-muted-foreground">
                    filtering · click to clear
                  </p>
                )}
              </div>
              <span
                className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl ${c.plate} transition-transform duration-200 group-hover:scale-105`}
              >
                <c.icon className={`h-5 w-5 ${c.color}`} strokeWidth={1.9} />
              </span>
            </CardContent>
          </Card>
        </motion.div>
      ))}
    </div>
  );
}
