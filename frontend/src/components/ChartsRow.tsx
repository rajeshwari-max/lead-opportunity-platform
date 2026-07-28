import { useMemo } from "react";
import {
  Bar,
  BarChart,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate } from "@/lib/utils";
import { scrollToOpportunities } from "@/components/StatCards";
import type { FilterState, Stats } from "@/lib/types";

const COLORS = ["#818cf8", "#34d399", "#fbbf24", "#f472b6", "#38bdf8", "#fb923c", "#a78bfa", "#f87171"];

function toSeries(record: Record<string, number>) {
  return Object.entries(record).map(([name, value]) => ({ name, value }));
}

// Theme-aware tooltip: CSS variables resolve to light or dark automatically.
const tooltipStyle = {
  backgroundColor: "hsl(var(--card))",
  border: "1px solid hsl(var(--border))",
  color: "hsl(var(--foreground))",
  borderRadius: 8,
  fontSize: 12,
};

interface Props {
  stats: Stats | null;
  loading?: boolean;
  filters: FilterState;
  onChange: (f: FilterState) => void;
}

/**
 * Dashboard charts. Fully responsive: stacked on mobile, 2-up on tablet,
 * 4-up grid on desktop. Every chart reflects the active filters, and clicking
 * a slice/bar applies that value as a filter (click again to clear).
 */
export function ChartsRow({ stats, loading, filters, onChange }: Props) {
  const categorySeries = useMemo(() => toSeries(stats?.by_category ?? {}), [stats]);
  const regionSeries = useMemo(() => toSeries(stats?.by_region ?? {}), [stats]);
  const verticalSeries = useMemo(() => toSeries(stats?.by_vertical ?? {}), [stats]);

  if (!stats) {
    if (!loading) return null;
    return (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-[280px]" />
        ))}
      </div>
    );
  }

  const toggleSingle = (key: "categories" | "verticals" | "regions", name: string) => {
    const current = filters[key];
    const next = current.length === 1 && current[0] === name ? [] : [name];
    onChange({ ...filters, [key]: next, page: 1 });
    scrollToOpportunities();
  };

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
      <ChartCard title="By Category">
        <ResponsiveContainer width="100%" height={230}>
          <PieChart margin={{ top: 4, right: 4, bottom: 4, left: 4 }}>
            <Pie
              data={categorySeries}
              dataKey="value"
              nameKey="name"
              innerRadius="48%"
              outerRadius="78%"
              paddingAngle={3}
              strokeOpacity={0}
              className="cursor-pointer outline-none"
              onClick={(entry: { name?: string }) => entry?.name && toggleSingle("categories", entry.name)}
            >
              {categorySeries.map((_, i) => (
                <Cell key={i} fill={COLORS[i % COLORS.length]} />
              ))}
            </Pie>
            <Legend
              iconSize={8}
              wrapperStyle={{ fontSize: 11, lineHeight: "16px" }}
              formatter={(v: string) => <span className="text-muted-foreground">{v}</span>}
            />
            <Tooltip contentStyle={tooltipStyle} />
          </PieChart>
        </ResponsiveContainer>
      </ChartCard>

      <ChartCard title="By Region">
        <BarsChart data={regionSeries} fill={COLORS[0]}
                   onBarClick={(name) => toggleSingle("regions", name)} />
      </ChartCard>

      <ChartCard title="By Vertical">
        <BarsChart data={verticalSeries} fill={COLORS[1]}
                   onBarClick={(name) => toggleSingle("verticals", name)} />
      </ChartCard>

      <ChartCard title="Upcoming Deadlines">
        <ul className="max-h-[230px] space-y-2 overflow-y-auto pr-1">
          {stats.upcoming_deadlines.map((o) => (
            <li key={o.id} className="flex items-center justify-between gap-3 text-sm">
              <a href={o.opportunity_url} target="_blank" rel="noreferrer"
                 className="truncate hover:text-primary hover:underline" title={o.title}>
                {o.title}
              </a>
              <span className="shrink-0 text-xs font-medium text-amber-500">
                {formatDate(o.deadline)}
              </span>
            </li>
          ))}
          {stats.upcoming_deadlines.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No upcoming deadlines match the current filters.
            </p>
          )}
        </ul>
      </ChartCard>
    </div>
  );
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  // min-w-0 lets ResponsiveContainer shrink correctly inside CSS grid (no clipping)
  return (
    <Card className="min-w-0">
      <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
      <CardContent className="min-w-0">{children}</CardContent>
    </Card>
  );
}

function BarsChart({
  data,
  fill,
  onBarClick,
}: {
  data: { name: string; value: number }[];
  fill: string;
  onBarClick?: (name: string) => void;
}) {
  if (data.length === 0)
    return <p className="text-sm text-muted-foreground">No data for the current filters.</p>;
  return (
    <ResponsiveContainer width="100%" height={230}>
      <BarChart data={data.slice(0, 8)} layout="vertical" margin={{ left: 4, right: 12 }}>
        <XAxis type="number" hide />
        <YAxis
          type="category"
          dataKey="name"
          width={108}
          tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
          tickFormatter={(v: string) => (v.length > 15 ? `${v.slice(0, 14)}…` : v)}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "hsl(var(--muted))", opacity: 0.4 }} />
        <Bar
          dataKey="value"
          fill={fill}
          radius={[0, 6, 6, 0]}
          barSize={14}
          className={onBarClick ? "cursor-pointer" : undefined}
          onClick={(entry: { name?: string }) => entry?.name && onBarClick?.(entry.name)}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
