import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Facets, FilterState, Paginated, Progress, SourceInfo, Stats } from "@/lib/types";

/** Debounced, filter-driven opportunity list (server-side filtering — never re-scrapes). */
export function useOpportunities(filters: FilterState, refreshKey: number) {
  const [data, setData] = useState<Paginated | null>(null);
  const [loading, setLoading] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    setLoading(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api
        .opportunities(filters)
        .then(setData)
        .catch(console.error)
        .finally(() => setLoading(false));
    }, 200); // debounce keystrokes in the search box
    return () => clearTimeout(timer.current);
  }, [filters, refreshKey]);

  return { data, loading };
}

export function useDashboardData(filters: FilterState, refreshKey: number) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [sources, setSources] = useState<SourceInfo[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  // Stats only depend on DATA filters — page/sort changes must not refetch them.
  const statsKey = useMemo(
    () =>
      JSON.stringify({
        c: filters.categories, se: filters.verticals, co: filters.countries,
        r: filters.regions, so: filters.sources, q: filters.search,
        db: filters.deadline_before, da: filters.deadline_after,
      }),
    [filters]
  );
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  useEffect(() => {
    setStatsLoading(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api
        .stats(filtersRef.current)
        .then(setStats)
        .catch(console.error)
        .finally(() => setStatsLoading(false));
    }, 200); // debounce so typing in search doesn't spam the stats endpoint
    return () => clearTimeout(timer.current);
  }, [statsKey, refreshKey]);

  // Facets and sources are filter-independent — fetched only on refresh.
  useEffect(() => {
    api.facets().then(setFacets).catch(console.error);
    api.sources().then(setSources).catch(console.error);
  }, [refreshKey]);

  return { stats, statsLoading, facets, sources };
}

/** Polls /progress while a scrape is active; fires onFinished when it returns to idle. */
export function useScrapeProgress(onFinished: () => void) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const wasActive = useRef(false);

  const tick = useCallback(async () => {
    try {
      const p = await api.progress();
      setProgress(p);
      if (p.state !== "idle") wasActive.current = true;
      else if (wasActive.current) {
        wasActive.current = false;
        onFinished();
      }
    } catch (e) {
      console.error(e);
    }
  }, [onFinished]);

  useEffect(() => {
    tick();
    const id = setInterval(tick, 1500);
    return () => clearInterval(id);
  }, [tick]);

  return progress;
}
