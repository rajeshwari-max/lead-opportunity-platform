import { motion } from "framer-motion";
import { Activity, Award, FileText, Gavel, Sparkles } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { FilterState, Stats } from "@/lib/types";

const cards = [
  { key: "total", label: "Active Opportunities", icon: Activity, color: "text-primary" },
  { key: "Grant", label: "Grants", icon: Award, color: "text-emerald-500" },
  { key: "RFP", label: "RFPs", icon: FileText, color: "text-indigo-500" },
  { key: "Tender", label: "Tenders", icon: Gavel, color: "text-amber-500" },
  { key: "today", label: "New Today", icon: Sparkles, color: "text-fuchsia-500" },
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
      onChange({ ...filters, categories: [], page: 1 });
    } else if (key === "today") {
      // "New today" = latest scraped first
      onChange({ ...filters, sort_by: "date_scraped", sort_dir: "desc", page: 1 });
    } else {
      onChange({ ...filters, categories: [key], page: 1 });
    }
    scrollToOpportunities();
  };

  const isActive = (key: string) =>
    key !== "total" && key !== "today" && filters.categories.length === 1 && filters.categories[0] === key;

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
            className={`h-full cursor-pointer transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-md ${
              isActive(c.key) ? "border-primary/60 ring-1 ring-primary/30" : ""
            }`}
          >
            <CardContent className="flex h-full items-center justify-between p-5">
              <div>
                <p className="text-xs text-muted-foreground">{c.label}</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{value(c.key)}</p>
              </div>
              <c.icon className={`h-8 w-8 shrink-0 ${c.color}`} strokeWidth={1.6} />
            </CardContent>
          </Card>
        </motion.div>
      ))}
    </div>
  );
}
