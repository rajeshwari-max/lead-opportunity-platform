import { DashboardLeads } from "@/components/DashboardLeads";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChartsRow } from "@/components/ChartsRow";
import { ExpertsCard } from "@/components/ExpertsCard";
import { SiteLoginsCard } from "@/components/SiteLoginsCard";
import { FiltersSidebar } from "@/components/FiltersSidebar";
import { Header } from "@/components/Header";
import { AutoEmailPanel } from "@/components/AutoEmailPanel";
import { LoginScreen } from "@/components/LoginScreen";
import { UserMenu } from "@/components/UserMenu";
import { UserDashboard } from "@/components/UserDashboard";
import { OpportunitiesTable } from "@/components/OpportunitiesTable";
import { ReviewQueueCard } from "@/components/ReviewQueueCard";
import { ScraperHealthCard } from "@/components/ScraperHealthCard";
import { UnclassifiedCard } from "@/components/UnclassifiedCard";
import { ScraperPanel } from "@/components/ScraperPanel";
import { StatCards } from "@/components/StatCards";
import { TeamPanel } from "@/components/TeamPanel";
import { useDashboardData, useOpportunities, useScrapeProgress } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { emptyFilters, type FilterState } from "@/lib/types";

function loadFilters(saved: Partial<FilterState> = {}): FilterState {
  // Restore only recognised values with the expected shape.
  saved = Object.fromEntries(Object.entries(saved).filter(([key, value]) => {
    const template = emptyFilters[key as keyof FilterState];
    return Array.isArray(template) ? Array.isArray(value) && value.every(v => typeof v === "string") : typeof value === typeof template;
  })) as Partial<FilterState>;
  // The Research/Implementation buttons are gone, so nothing on screen can
  // clear a work_type left in localStorage from before. Someone who had
  // "Research" active would come back to a silently filtered table with no
  // control to unset it. A link can still set it (see below); a stale saved
  // value cannot.
  delete (saved as Partial<FilterState>).work_type;

  const params = new URLSearchParams(window.location.search);
  // Switching layouts must not clear the saved opportunity filters.
  params.delete("view");
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
  const restoredOwner = useRef<string | null>(null);
  const [filters, setFilters] = useState<FilterState>(() => loadFilters());
  const [preferencesReady, setPreferencesReady] = useState(false);
  const [preferencesError, setPreferencesError] = useState("");
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
  const [isAdmin, setIsAdmin] = useState(false);
  const [user, setUser] = useState({ name: "", email: "", authRequired: false });

  useEffect(() => {
    api
      .config()
      .then(async (c) => {
        setReadOnly(c.read_only);
        if (!c.authenticated) { restoredOwner.current = null; setPreferencesReady(false); }
        if (c.authenticated && restoredOwner.current !== c.email) {
          setPreferencesReady(false);
          try {
            const r = await fetch("/api/my-leads/preferences");
            if (!r.ok) throw new Error("Could not restore your filters");
            const saved = await r.json();
            setFilters(loadFilters(saved.filters));
            restoredOwner.current = c.email;
            setPreferencesReady(true);
            setPreferencesError("");
          } catch { setPreferencesError("Your saved filters could not be restored. Refresh to try again."); }
        }
        setAuthed(c.authenticated);
        setIsAdmin(c.is_admin);
        setUser({ name: c.name, email: c.email, authRequired: c.auth_required });
      })
      .catch(() => {
        setIsAdmin(false);
        setReadOnly(false);
        setAuthed(false);   // backend unreachable — don't trap the user behind a
                           // login form that cannot possibly succeed
      });
  }, [refreshKey]);

  useEffect(() => {
    if (!preferencesReady || !authed || readOnly) return;
    const timer = setTimeout(() => {
      void fetch("/api/my-leads/preferences", {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({filters})})
        .then(r => { if (!r.ok) throw new Error(); setPreferencesError(""); })
        .catch(() => setPreferencesError("Filter changes could not be saved. Your saved leads are unaffected."));
    }, 700);
    return () => clearTimeout(timer);
  }, [filters, preferencesReady, authed, readOnly]);

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
  if (!authed || window.location.hash.startsWith("#setup=")) return <LoginScreen onSuccess={() => setRefreshKey((k) => k + 1)} />;



  const userView = new URLSearchParams(window.location.search).get("view") === "user";
  const viewUrl = (view: string) => {
    const params = new URLSearchParams(window.location.search);
    params.set("view", view);
    return `${window.location.pathname}?${params}`;
  };
  if (!isAdmin || userView) return <><p role="status">{preferencesError}</p><UserDashboard filters={filters} onChange={setFilters}
    onRefresh={resetAndRefresh} onDataRefresh={refresh} data={data} loading={loading}
    stats={stats} statsLoading={statsLoading} facets={facets} readOnly={readOnly}
    user={user} adminViewUrl={isAdmin ? viewUrl("admin") : undefined} /></>;

  return (
    <div className="min-h-screen">
      <Header filters={filters} onChange={setFilters} onRefresh={resetAndRefresh} stats={stats}
              userMenu={<div className="flex items-center gap-3"><a href="#my-leads" className="whitespace-nowrap rounded-md border border-border px-3 py-2 text-xs font-medium">My leads & team activity</a><a href={viewUrl("user")} className="whitespace-nowrap rounded-md border border-border px-3 py-2 text-xs font-medium hover:bg-accent">User dashboard</a><UserMenu name={user.name} email={user.email} isAdmin={isAdmin}
                                  authRequired={user.authRequired} /></div>} />

      <main className="mx-auto flex max-w-[1600px] flex-col gap-6 p-4 sm:p-6">
        <p role="status">{preferencesError}</p>
        <DashboardLeads isAdmin={isAdmin} name={user.name} />
        <StatCards stats={stats} loading={statsLoading} filters={filters} onChange={setFilters} />
        <ChartsRow stats={stats} loading={statsLoading} filters={filters} onChange={setFilters} />

        <div className="flex flex-col gap-6 lg:flex-row">
          <FiltersSidebar brandHierarchy facets={facets} filters={filters} onChange={setFilters} />
          <div id="opportunities-table" className="flex min-w-0 flex-1 scroll-mt-20 flex-col gap-6">
            <OpportunitiesTable data={data} loading={loading} filters={filters} onChange={setFilters}
                                facets={facets} readOnly={readOnly} />
          </div>
          <div className="flex w-full flex-col gap-6 lg:w-80 lg:shrink-0">
            {/* Administrative review queues. Hiding the cards is paired with
                backend authorization, so an ordinary user cannot bypass the
                UI and call their endpoints directly. */}
            {isAdmin && <ReviewQueueCard readOnly={readOnly} />}
            {isAdmin && <UnclassifiedCard readOnly={readOnly} />}
            <ScraperHealthCard isAdmin={isAdmin} />
            {isAdmin && <ScraperPanel sources={sources} progress={progress} />}
            {/* Admin-only: this panel sets the send time and reminder days for
                the WHOLE team, and its PUT route already rejects non-admins.
                Showing a control that would 403 is worse than hiding it. */}
            {isAdmin && <AutoEmailPanel readOnly={readOnly} />}
            {isAdmin && <TeamPanel readOnly={readOnly} />}
            <ExpertsCard readOnly={readOnly} isAdmin={isAdmin} />
            <SiteLoginsCard isAdmin={isAdmin} />
          </div>
        </div>
      </main>
    </div>
  );
}
