import { useCallback, useEffect, useState } from "react";
import { ChartsRow } from "@/components/ChartsRow";
import { ExpertsCard } from "@/components/ExpertsCard";
import { FiltersSidebar } from "@/components/FiltersSidebar";
import { Header } from "@/components/Header";
import { OpportunitiesTable } from "@/components/OpportunitiesTable";
import { ScraperPanel } from "@/components/ScraperPanel";
import { StatCards } from "@/components/StatCards";
import { TeamPanel } from "@/components/TeamPanel";
import { useDashboardData, useOpportunities, useScrapeProgress } from "@/hooks/useApi";
import { emptyFilters, type FilterState } from "@/lib/types";

const FILTERS_KEY = "lop-filters";

/** Restore the last filter selection so a page refresh keeps the user's view. */
function loadFilters(): FilterState {
  try {
    const raw = localStorage.getItem(FILTERS_KEY);
    if (!raw) return emptyFilters;
    return { ...emptyFilters, ...(JSON.parse(raw) as Partial<FilterState>) };
  } catch {
    return emptyFilters;
  }
}

export default function App() {
  const [filters, setFilters] = useState<FilterState>(loadFilters);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    localStorage.setItem(FILTERS_KEY, JSON.stringify(filters));
  }, [filters]);

  // Background refresh (e.g. after a scrape finishes): refetch, keep the user's filters.
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  // Header refresh button: clear every filter and show the original global
  // numbers (cards, charts, table) — a true "reset view".
  const resetAndRefresh = useCallback(() => {
    setFilters(emptyFilters);
    setRefreshKey((k) => k + 1);
  }, []);

  const { data, loading } = useOpportunities(filters, refreshKey);
  const { stats, statsLoading, facets, sources } = useDashboardData(filters, refreshKey);
  const progress = useScrapeProgress(refresh); // auto-refresh when a scrape finishes

  return (
    <div className="min-h-screen">
      <Header filters={filters} onChange={setFilters} onRefresh={resetAndRefresh} stats={stats} />

      <main className="mx-auto flex max-w-[1600px] flex-col gap-6 p-4 sm:p-6">
        <StatCards stats={stats} loading={statsLoading} filters={filters} onChange={setFilters} />
        <ChartsRow stats={stats} loading={statsLoading} filters={filters} onChange={setFilters} />

        <div className="flex flex-col gap-6 lg:flex-row">
          <FiltersSidebar facets={facets} filters={filters} onChange={setFilters} />
          <div id="opportunities-table" className="flex min-w-0 flex-1 scroll-mt-20 flex-col gap-6">
            <OpportunitiesTable data={data} loading={loading} filters={filters} onChange={setFilters} />
          </div>
          <div className="flex w-full flex-col gap-6 lg:w-80 lg:shrink-0">
            <ScraperPanel sources={sources} progress={progress} />
            <TeamPanel />
            <ExpertsCard />
          </div>
        </div>
      </main>
    </div>
  );
}
