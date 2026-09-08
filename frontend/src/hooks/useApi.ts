import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Facets, FilterState, Paginated, Progress, SourceInfo, Stats } from "@/lib/types";

/** Debounced, filter-driven opportunity list (server-side filtering — never re-scrapes). */
export function useOpportunities(filters: FilterState, refreshKey: number) {
  const [data, setData] = useState<Paginated | null>(null);
  const [loading, setLoading] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api
        .opportunities(filters)
        .then((result) => { if (!cancelled) setData(result); })
        .catch((error) => { if (!cancelled) { setData(null); console.error(error); } })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 200); // debounce keystrokes in the search box
    return () => { cancelled = true; clearTimeout(timer.current); };
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
        archived: filters.archived, new_today: filters.new_today,
        approved: filters.approved, work_type: filters.work_type,
        study_type: filters.study_type, english_only: filters.english_only,
        has_vertical: filters.has_vertical,
        unclassified_only: filters.unclassified_only,
      }),
    [filters]
  );
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  useEffect(() => {
    let cancelled = false;
    setStatsLoading(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api
        .stats(filtersRef.current)
        .then((result) => { if (!cancelled) setStats(result); })
        .catch((error) => { if (!cancelled) { setStats(null); console.error(error); } })
        .finally(() => { if (!cancelled) setStatsLoading(false); });
    }, 200); // debounce so typing in search doesn't spam the stats endpoint
    return () => { cancelled = true; clearTimeout(timer.current); };
  }, [statsKey, refreshKey]);

  // Facets now follow the active filters, so a Source dropdown under a chosen
  // vertical offers only the sources that actually have rows there. Same
  // debounce and same key as stats: this is a scan over the whole table, so
  // firing it per keystroke would queue up ~800ms queries behind each other.
  const facetTimer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => {
    let cancelled = false;
    clearTimeout(facetTimer.current);
    facetTimer.current = setTimeout(() => {
      api.facets(filtersRef.current).then((result) => { if (!cancelled) setFacets(result); }).catch(console.error);
    }, 200);
    return () => { cancelled = true; clearTimeout(facetTimer.current); };
  }, [statsKey, refreshKey]);

  // The scraper's own source list is a fixed catalogue of what CAN be scraped,
  // not what the current filter reaches — it must not narrow.
  useEffect(() => {
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
