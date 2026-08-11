import { useCallback, useEffect, useState } from "react";
import { ChartsRow } from "@/components/ChartsRow";
import { ExpertsCard } from "@/components/ExpertsCard";
import { FiltersSidebar } from "@/components/FiltersSidebar";
import { Header } from "@/components/Header";
import { AutoEmailPanel } from "@/components/AutoEmailPanel";
import { LoginScreen } from "@/components/LoginScreen";
import { UserMenu } from "@/components/UserMenu";
import { OpportunitiesTable } from "@/components/OpportunitiesTable";
import { ScraperPanel } from "@/components/ScraperPanel";
import { StatCards } from "@/components/StatCards";
import { TeamPanel } from "@/components/TeamPanel";
import { useDashboardData, useOpportunities, useScrapeProgress } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { emptyFilters, type FilterState } from "@/lib/types";

const FILTERS_KEY = "lop-filters";

/** Restore the last filter selection so a page refresh keeps the user's view.
 *
 *  A URL query string wins over the saved selection, so a link can put someone
 *  straight into a specific view — the digest email's region chips rely on this
 *  (?region=South+Asia). Without it a chip would open the dashboard showing
 *  whatever filters that person last used, which is not what the chip promised.
 */
function loadFilters(): FilterState {
  let saved: Partial<FilterState> = {};
  try {
    const raw = localStorage.getItem(FILTERS_KEY);
    if (raw) saved = JSON.parse(raw) as Partial<FilterState>;
  } catch {
    saved = {};
  }

  const params = new URLSearchParams(window.location.search);
  if (![...params.keys()].length) return { ...emptyFilters, ...saved };

  // A link is an explicit request for one view, so start from a clean slate
  // rather than layering onto stale saved filters.
  const fromUrl: Partial<FilterState> = {};
  const region = params.get("region");
  const country = params.get("country");
  const vertical = params.get("vertical");
  if (region) fromUrl.regions = [region];
  if (country) fromUrl.countries = [country];
  if (vertical) fromUrl.verticals = [vertical];
  if (params.get("work_type")) fromUrl.work_type = params.get("work_type") ?? "";
  if (params.get("approved") === "true") fromUrl.approved = true;
  if (params.get("search")) fromUrl.search = params.get("search") ?? "";
  return { ...emptyFilters, ...fromUrl };
}

export default function App() {
  const [filters, setFilters] = useState<FilterState>(loadFilters);
  const [refreshKey, setRefreshKey] = useState(0);
  // On the read-only cloud mirror (no scraper login session), the admin panels
  // (scraper controls, team routing, expert pool connect) don't function —
  // hide them instead of showing viewers "not configured" / "connect account"
  // warnings that look like something is broken.
  const [readOnly, setReadOnly] = useState(false);
  // null = we haven't asked the server yet, so render nothing rather than
  // flashing the dashboard before the gate is known.
  const [authed, setAuthed] = useState<boolean | null>(null);
  // Admin unlocks the panels that change behaviour — scraping, team routing,
  // email schedule. Reading and approving stay open to everyone signed in.
  const [isAdmin, setIsAdmin] = useState(true);
  const [user, setUser] = useState({ name: "", email: "", authRequired: false });

  useEffect(() => {
    api
      .config()
      .then((c) => {
        setReadOnly(c.read_only);
        setAuthed(c.authenticated);
        setIsAdmin(c.is_admin);
        setUser({ name: c.name, email: c.email, authRequired: c.auth_required });
      })
      .catch(() => {
        setReadOnly(false);
        setAuthed(true);   // backend unreachable — don't trap the user behind a
                           // login form that cannot possibly succeed
      });
  }, [refreshKey]);

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

  if (authed === null) return null;
  if (!authed) return <LoginScreen onSuccess={() => setRefreshKey((k) => k + 1)} />;

  return (
    <div className="min-h-screen">
      <Header filters={filters} onChange={setFilters} onRefresh={resetAndRefresh} stats={stats}
              userMenu={<UserMenu name={user.name} email={user.email} isAdmin={isAdmin}
                                  authRequired={user.authRequired} />} />

      <main className="mx-auto flex max-w-[1600px] flex-col gap-6 p-4 sm:p-6">
        <StatCards stats={stats} loading={statsLoading} filters={filters} onChange={setFilters} />
        <ChartsRow stats={stats} loading={statsLoading} filters={filters} onChange={setFilters} />

        <div className="flex flex-col gap-6 lg:flex-row">
          <FiltersSidebar facets={facets} filters={filters} onChange={setFilters} />
          <div id="opportunities-table" className="flex min-w-0 flex-1 scroll-mt-20 flex-col gap-6">
            <OpportunitiesTable data={data} loading={loading} filters={filters} onChange={setFilters} readOnly={readOnly} />
          </div>
          <div className="flex w-full flex-col gap-6 lg:w-80 lg:shrink-0">
            {/* Only the scraper is admin-only. Everything else stays visible. */}
            {isAdmin && <ScraperPanel sources={sources} progress={progress} />}
            <AutoEmailPanel readOnly={readOnly} />
            <TeamPanel readOnly={readOnly} />
            <ExpertsCard readOnly={readOnly} isAdmin={isAdmin} />
          </div>
        </div>
      </main>
    </div>
  );
}
