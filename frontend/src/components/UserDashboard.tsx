import { DashboardLeads, SaveLeadButton, ReviewLeadButton, recordLeadActivity } from "./DashboardLeads";
import { WrikeTaskAction } from "./WrikeTaskAction";
import { useEffect, useRef, useState } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { Download, Moon, RefreshCw, Sun } from "lucide-react";
import { ExpertsCard } from "./ExpertsCard";
import { FiltersSidebar } from "./FiltersSidebar";
import { brandPath } from "./BrandFilters";
import { SendSelectionBar } from "./SendSelectionBar";
import { UserMenu } from "./UserMenu";
import { api } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import { toInr, RATES_AS_OF } from "@/lib/money";
import { emptyFilters, type Facets, type FilterState, type Opportunity, type Paginated, type Stats } from "@/lib/types";
import "./user-dashboard.css";

interface Props {
  filters: FilterState;
  onChange: (filters: FilterState) => void;
  onRefresh: () => void;
  onDataRefresh: () => void;
  data: Paginated | null;
  loading: boolean;
  stats: Stats | null;
  statsLoading: boolean;
  facets: Facets | null;
  readOnly: boolean;
  user: { name: string; email: string; authRequired: boolean };
  adminViewUrl?: string;
}

const colors: Record<string, string> = { Grant: "#16a6bf", RFP: "#8970d5", Tender: "#e2a54a", Proposal: "#386ce0" };
const count = (n: number) => n.toLocaleString("en-IN");
const tags = (o: Opportunity) => (o.verticals || o.vertical || "").split(",").map(v => v.trim()).filter(Boolean);
const shortVertical = (s: string) => s.replace(/\(.*\)/, "").trim();
function deadlineLabel(value: string | null) {
  if (!value) return "Ongoing";
  const today = new Date();
  const date = new Date(value.slice(0, 10) + "T00:00:00");
  today.setHours(0, 0, 0, 0);
  const days = Math.round((date.getTime() - today.getTime()) / 86400000);
  return days < 0 ? "Closed" : days === 0 ? "Closes today" : days === 1 ? "Closes tomorrow" : days <= 7 ? `${days} days left` : "";
}

export function UserDashboard({ filters, onChange, onRefresh, onDataRefresh, data, loading, stats, statsLoading, facets, readOnly, user, adminViewUrl }: Props) {
  const [dark, setDark] = useState(() => localStorage.getItem("lop-theme") === "dark");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<number | null>(null);
  const [approval, setApproval] = useState<Opportunity | null>(null);
  const [wrikeDialog, setWrikeDialog] = useState<Opportunity | null>(null);
  const [wrikeRevision, setWrikeRevision] = useState(0);
  const [message, setMessage] = useState("");
  const [showInr, setShowInr] = useState(false);
  const [hovered, setHovered] = useState<{ name: string; value: number } | null>(null);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const briefRef = useRef<HTMLElement>(null);
  const railRef = useRef<HTMLElement>(null);
  const tableRef = useRef<HTMLElement>(null);
  const unclassified = !!filters.unclassified_only;
  const switchTab = (value: boolean) => {
    setSelectedId(null);
    setChecked(new Set());
    setApproval(null);
    setMessage("");
    setHovered(null);
    onChange({ ...filters, unclassified_only: value, has_vertical: !value, verticals: [], page: 1 });
    railRef.current?.scrollTo({ top: 0, left: 0 });
  };
  const resetView = () => {
    if (!unclassified) { onRefresh(); return; }
    onChange({ ...emptyFilters, unclassified_only: true, has_vertical: false });
    onDataRefresh();
  };
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("lop-theme", dark ? "dark" : "light");
  }, [dark]);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!wrikeDialog) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setWrikeDialog(null);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [wrikeDialog]);
  useEffect(() => { setApproval(null); }, [data]);
  const current = data?.items.find(o => o.id === selectedId) ?? data?.items[0];
  const selected = current && approval?.id === current.id ? approval : current;
  const series = Object.entries(stats?.by_category ?? {}).map(([name, value]) => ({ name, value }));
  const remaining = (stats?.total_active ?? 0) - series.reduce((sum, s) => sum + s.value, 0);
  const pieSeries = remaining > 0 ? [...series, { name: "Other categories", value: remaining }] : series;
  const allChecked = !!data?.items.length && data.items.every(o => checked.has(o.id));
  const change = (patch: Partial<FilterState>) => onChange({ ...filters, ...patch, page: 1 });
  const toggle = (key: "categories" | "regions" | "verticals", value: string) => change({ [key]: filters[key].length === 1 && filters[key][0] === value ? [] : [value] });
  const openBrief = (o: Opportunity) => {
    setSelectedId(o.id);
    void recordLeadActivity(o.id,"viewed").catch(()=>setMessage("Activity could not be saved. Please try again."));
    setMessage(`Viewing ${o.title}`);
    // A previously scrolled brief must reveal the new title on every click,
    // including when the already-selected row is opened again.
    railRef.current?.scrollTo({ top: 0, left: 0, behavior: "instant" });
    briefRef.current?.focus({ preventScroll: true });
    if (window.matchMedia("(max-width: 1050px)").matches) {
      briefRef.current?.scrollIntoView({ block: "start", behavior: "auto" });
      briefRef.current?.focus({ preventScroll: true });
    }
  };
  const approve = async (o: Opportunity) => {
    setBusy(o.id); setMessage("");
    try {
      setApproval(await api.approve(o.id, !o.approved));
      setMessage(o.approved ? "Approval removed." : "Opportunity approved.");
      onDataRefresh();
    } catch { setMessage("Approval could not be saved. Please try again."); }
    finally { setBusy(null); }
  };
  const goPage = (page: number) => {
    onChange({ ...filters, page });
    tableRef.current?.scrollIntoView({ block: "start", behavior: "auto" });
  };
  const pager = () => {
    const page = data?.page ?? 1;
    const pages = Math.max(1, data?.pages ?? 1);
    const numbers = [...new Set([1, page - 1, page, page + 1, pages])].filter(n => n > 0 && n <= pages).sort((a, b) => a - b);
    return <div className="ud-pager">
      <span>{data?.total ? `${count((page - 1) * data.page_size + 1)}–${count(Math.min(page * data.page_size, data.total))} of ${count(data.total)}` : "0 opportunities"}</span>
      <nav aria-label="Opportunity pages"><button disabled={loading || page <= 1} onClick={() => goPage(page - 1)} aria-label="Previous page">‹</button>
        {numbers.map((n, i) => <span key={n}>{i > 0 && n > numbers[i - 1] + 1 && <span className="ud-ellipsis">…</span>}<button aria-current={n === page ? "page" : undefined} disabled={loading} onClick={() => goPage(n)}>{n}</button></span>)}
        <button disabled={loading || page >= pages} onClick={() => goPage(page + 1)} aria-label="Next page">›</button></nav>
      <select aria-label="Opportunities per page" value={filters.page_size} onChange={e => change({ page_size: Number(e.target.value) })}>{[10,25,50,100].map(n => <option key={n} value={n}>{n} / page</option>)}</select>
    </div>;
  };
  const statCards = [
    { label: filters.archived ? "Archived opportunities" : "Active opportunities", value: stats?.total_active, note: "Across your current view", tone: "primary", active: !filters.categories.length && !filters.new_today, action: () => change({ categories: [], new_today: false }) },
    ...["Grant", "RFP", "Tender"].map((name, i) => ({ label: ["Grants", "RFPs", "Tenders"][i], value: stats?.by_category[name] ?? 0, note: ["Explore funding calls", "Explore proposals", "Explore procurement"][i], tone: ["cyan", "lilac", "amber"][i], active: filters.categories.includes(name), action: () => toggle("categories", name) })),
    { label: "New today", value: stats?.todays_new, note: "Newly discovered opportunities", tone: "mint", active: filters.new_today, action: () => change({ new_today: !filters.new_today }) },
  ];
  return <div className="user-dashboard">
    <header className="ud-header"><div className="ud-brand"><span className="ud-logo">CMS</span><div><strong>Lead Scanning Platform</strong><p>Funding &amp; opportunity intelligence</p></div></div>
      <div className="ud-actions">{adminViewUrl && <a className="ud-control" href={adminViewUrl}>Admin panel</a>}<UserMenu {...user} isAdmin={!!adminViewUrl} authRequired={user.authRequired} />
        <a className="ud-control" href={api.exportUrl("csv", filters)} download><Download size={14} /> CSV</a><a className="ud-control" href={api.exportUrl("xlsx", filters)} download>Excel</a>
        <button className="ud-control" onClick={resetView} aria-label="Clear filters and refresh"><RefreshCw size={16} /></button><button className="ud-control" onClick={() => setDark(!dark)} aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}>{dark ? <Sun size={16} /> : <Moon size={16} />}</button></div>
    </header>
    <main className="ud-main"><DashboardLeads name={user.name} isAdmin={!!adminViewUrl} /><div className="ud-heading"><div><h1>Your opportunity dashboard</h1><p>Explore funding. Track deadlines. Find your next opportunity.</p></div>{stats?.last_scraped && <span className="ud-updated">Updated {new Date(/Z$|[+-]\d{2}:\d{2}$/.test(stats.last_scraped) ? stats.last_scraped : stats.last_scraped + "Z").toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</span>}</div>
      <div className="ud-stats" aria-busy={statsLoading}>{statCards.map(card => <button key={card.label} className={`ud-stat ud-${card.tone}`} aria-pressed={card.active} onClick={card.action}><span>{card.label}</span><strong>{statsLoading || card.value == null ? "…" : count(card.value)}</strong><small>{card.note}</small></button>)}</div>
      <div className="ud-charts" aria-busy={statsLoading}>
        <section className="ud-panel ud-chart"><h2>By category</h2><p className="ud-sub">Distribution in your current view</p><div className="ud-donut">
          <ResponsiveContainer width="100%" height={210}><PieChart><Pie data={pieSeries} dataKey="value" nameKey="name" innerRadius={69} outerRadius={91} paddingAngle={2} stroke="none" isAnimationActive={!reducedMotion} animationDuration={350} onMouseEnter={entry => setHovered({ name: entry.name, value: entry.value })} onMouseLeave={() => setHovered(null)} onClick={entry => { if (entry.name !== "Other categories") toggle("categories", entry.name); }}>{pieSeries.map(s => <Cell key={s.name} fill={colors[s.name] ?? "#8b9db7"} />)}</Pie><Tooltip contentStyle={{ background: "var(--ud-paper)", border: "1px solid var(--ud-line)", borderRadius: 8, color: "var(--ud-ink)" }} /></PieChart></ResponsiveContainer>
          <div className="ud-donut-label"><strong>{count(hovered?.value ?? stats?.total_active ?? 0)}</strong><span>{hovered?.name ?? (filters.archived ? "Archived" : "Active")}<br />{!hovered && "opportunities"}</span></div>
        </div><div className="ud-legend">{series.map(s => <button key={s.name} aria-pressed={filters.categories.includes(s.name)} onClick={() => toggle("categories", s.name)} onMouseEnter={() => setHovered(s)} onMouseLeave={() => setHovered(null)}><i style={{ background: colors[s.name] ?? "#8b9db7" }} />{s.name}</button>)}</div>{remaining > 0 && <p className="ud-sub">{count(remaining)} in other categories</p>}{!statsLoading && !stats && <p className="ud-sub">Overview unavailable. Please refresh.</p>}{stats && !series.length && <p className="ud-sub">No categories in this view.</p>}</section>
        <Bars title="By region" subtitle="Opportunities by location" values={stats?.by_region ?? {}} selected={filters.regions} onSelect={name => toggle("regions", name)} />
        <Bars title="CMS classifications" subtitle={unclassified ? "These opportunities have no assigned vertical" : "Devsol verticals and Social Business · may overlap"} values={unclassified ? {} : stats?.by_vertical ?? {}} selected={filters.verticals} onSelect={name => toggle("verticals", name)} cyan />
        <section className="ud-panel ud-chart"><h2>Upcoming deadlines</h2><p className="ud-sub">Plan your next application</p><ul className="ud-deadlines">{stats?.upcoming_deadlines.slice(0, 4).map(o => <li key={o.id}><span>{o.deadline ? new Date(o.deadline).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }) : "Open"}</span><a href={o.link || o.opportunity_url} target="_blank" rel="noreferrer">{o.title}</a></li>)}</ul>{!stats?.upcoming_deadlines.length && <p className="ud-sub">No upcoming deadlines in this view.</p>}</section>
      </div>
      <div className="ud-workspace"><div className="ud-filter-panel ud-panel" role="region" aria-label="Opportunity filters" tabIndex={0}><FiltersSidebar brandHierarchy facets={facets} filters={filters} hideVerticals={unclassified} onChange={next => onChange({ ...next, unclassified_only: unclassified, has_vertical: unclassified ? false : next.has_vertical })} /></div>
        <section className="ud-panel ud-table-panel" ref={tableRef} id="opportunities-table" aria-busy={loading}>
          <div className="ud-table-heading"><nav className="ud-tabs" aria-label="Opportunity lists"><button aria-pressed={!unclassified} onClick={() => switchTab(false)}>Opportunities</button><button aria-pressed={unclassified} onClick={() => switchTab(true)}>Unclassified{unclassified && !loading && data ? ` · ${count(data.total)}` : ""}</button></nav><h2>{unclassified ? "Unclassified" : filters.archived ? "Archived" : "Latest"} opportunities</h2>{unclassified && <p className="ud-sub">No vertical assigned yet. Browse the full details here; classification is managed by admins. Counts follow your current filters.</p>}<input className="ud-search" type="search" aria-label="Search opportunities" placeholder="Search opportunities, organisations or keywords…" value={filters.search} onChange={e => change({ search: e.target.value })} />
            <div className="ud-table-tools">{([['archived', 'Archive'], ['approved', 'Approved only'], ['english_only', 'English only'], ['has_vertical', 'With CMS classification']] as const).filter(([key]) => !unclassified || key !== "has_vertical").map(([key, label]) => <label key={key}><input type="checkbox" checked={filters[key]} onChange={e => change({ [key]: e.target.checked })} />{label}</label>)}<label><input type="checkbox" checked={showInr} onChange={e => setShowInr(e.target.checked)} />INR estimate</label>
              <select aria-label="Sort opportunities" value={`${filters.sort_by}:${filters.sort_dir}`} onChange={e => { const [sort_by, sort_dir] = e.target.value.split(":"); change({ sort_by, sort_dir: sort_dir as "asc" | "desc" }); }}><option value="deadline:asc">Deadline: soonest</option><option value="deadline:desc">Deadline: latest</option><option value="date_scraped:desc">Newest first</option><option value="title:asc">Title: A–Z</option><option value="title:desc">Title: Z–A</option></select></div>
            {showInr && <p className="ud-sub">Approximate conversion · rates as of {RATES_AS_OF}. Original amounts retained.</p>}
            {(filters.new_today || filters.work_type || filters.study_type) && <p className="ud-sub">Also filtering: {[filters.new_today && "New today", filters.work_type, filters.study_type].filter(Boolean).join(" · ")} <button onClick={() => change({ new_today: false, work_type: "", study_type: "" })}>Clear</button></p>}
          </div>{pager()}
          {!readOnly && <SendSelectionBar selectedIds={[...checked]} onClear={() => setChecked(new Set())} />}
          <div className="ud-table-scroll"><table><thead><tr><th><input type="checkbox" aria-label="Select this page" checked={allChecked} disabled={loading} onChange={e => setChecked(previous => { const next = new Set(previous); data?.items.forEach(o => e.target.checked ? next.add(o.id) : next.delete(o.id)); return next; })} /></th><th>Opportunity / source / location</th><th>Deadline</th><th>Amount</th><th>Brief</th></tr></thead>
            <tbody>{loading ? <tr><td colSpan={5} className="ud-empty" role="status">Loading opportunities…</td></tr> : data?.items.map(o => <tr key={o.id} className={selected?.id === o.id ? "ud-selected" : ""}>
              <td><input type="checkbox" aria-label={`Select ${o.title}`} checked={checked.has(o.id)} onChange={e => setChecked(previous => { const next = new Set(previous); e.target.checked ? next.add(o.id) : next.delete(o.id); return next; })} /></td>
              <td><button className="ud-title" onClick={() => openBrief(o)}>{o.title}</button><p className="ud-metadata">{[o.organization, o.source_website, o.country].filter(Boolean).join(" · ")}</p><div className="ud-tags"><span className={`ud-tag ud-tag-${o.category}`}>{o.category}</span>{unclassified && <span className="ud-tag ud-unclassified">Unclassified</span>}{tags(o).map(t => <span className="ud-tag" key={t} title={brandPath(t)}>{shortVertical(t)}</span>)}{o.approved && <span className="ud-tag ud-approved">Approved</span>}</div><div className="ud-row-save"><SaveLeadButton id={o.id} /></div></td>
              <td>{formatDate(o.deadline)}<span className="ud-urgent">{deadlineLabel(o.deadline)}</span></td><td>{o.funding_amount || "—"}{showInr && toInr(o.funding_amount) && <p className="ud-sub">{toInr(o.funding_amount)}</p>}</td><td><button className="ud-view" aria-label={`View brief for ${o.title}`} onClick={() => openBrief(o)}>View</button><button className="ud-view ud-row-wrike" aria-label={`Create or view Wrike task for ${o.title}`} disabled={readOnly} onClick={() => setWrikeDialog(o)}>Wrike task</button></td>
            </tr>)}{!loading && !data?.items.length && <tr><td colSpan={5} className="ud-empty">{data ? "No opportunities match these filters." : "Opportunities could not be loaded."}<br /><button className="ud-view" onClick={data ? resetView : onDataRefresh}>{data ? "Clear filters" : "Retry"}</button></td></tr>}</tbody></table></div>{pager()}
        </section>
        <aside className="ud-rail" ref={railRef}><section className="ud-panel ud-brief" ref={briefRef} tabIndex={-1} aria-label="Opportunity brief"><div className="ud-brief-head">Opportunity brief</div><div className="ud-brief-body">{selected ? <><span className={`ud-tag ud-tag-${selected.category}`}>{selected.category}</span><h2 aria-live="polite">{selected.title}</h2><p className="ud-sub">{selected.organization || "Organisation not listed"}</p><dl><div><dt>Deadline</dt><dd>{selected.deadline ? formatDate(selected.deadline) : "Ongoing"}<span className="ud-urgent">{deadlineLabel(selected.deadline)}</span></dd></div><div><dt>Amount</dt><dd>{selected.funding_amount || "Not listed"}</dd></div><div><dt>Location</dt><dd>{selected.location || selected.country || "Not listed"}</dd></div><div><dt>Source</dt><dd>{selected.source_website || "Not listed"}</dd></div></dl><h3>Brands</h3>{unclassified && <p className="ud-sub">Not assigned</p>}<div className="ud-tags">{tags(selected).map(t => <span key={t} className="ud-tag">{brandPath(t)}</span>)}</div><h3>Source summary</h3><p className="ud-summary">{selected.summary || "No summary was provided by the source."}</p>{selected.eligibility && <><h3>Eligibility</h3><p className="ud-summary">{selected.eligibility}</p></>}{selected.work_type && <p className="ud-sub">Work type: {selected.work_type}</p>}{selected.study_type && <p className="ud-sub">Study: {selected.study_type}</p>}
          <SaveLeadButton id={selected.id} /><ReviewLeadButton id={selected.id} />
          <WrikeTaskAction key={`${selected.id}-${wrikeRevision}`} opportunityId={selected.id} opportunityTitle={selected.title} readOnly={readOnly} onCreated={() => setWrikeRevision(value => value + 1)} />
          {selected.link && selected.link_kind !== "none" ? <a className="ud-primary-button" href={selected.link} onClick={()=>void recordLeadActivity(selected.id,"source_opened").catch(()=>setMessage("Source opened, but activity could not be saved."))} target="_blank" rel="noreferrer">{selected.link_kind === "search" ? `Search ${selected.source_website}` : selected.link_kind === "listing" ? "Open source listing page ↗" : "Open original source ↗"}</a> : <p className="ud-sub">No source link provided.</p>}
          {/developmentaid|devex|globaltenders/i.test(selected.source_website) && <p className="ud-sub">Source membership or sign-in may be required.</p>}
          <button className="ud-approve" disabled={readOnly || busy !== null || loading} onClick={() => approve(selected)}>{busy === selected.id ? "Saving…" : selected.approved ? "Approved · Undo" : "Approve opportunity"}</button>{readOnly && <p className="ud-sub">Approvals unavailable on this read-only dashboard.</p>}<p role="status" className="ud-status">{message}</p></> : <p className="ud-sub">Select an opportunity to see its brief.</p>}</div></section><div className="ud-experts"><ExpertsCard readOnly={readOnly} isAdmin={false} /></div><section className="ud-signal"><h2>From discovery<br />to a decision.</h2><p>Select an opportunity to review its funding, eligibility and source details.</p><span>Discover → Review → Act</span></section></aside>
      </div>
    </main>
    {wrikeDialog && <div className="ud-wrike-overlay">
      <div className="ud-wrike-dialog" role="dialog" aria-modal="true" aria-labelledby="ud-wrike-dialog-title">
        <div className="ud-wrike-dialog-head"><h2 id="ud-wrike-dialog-title">Add opportunity to Wrike</h2><button type="button" aria-label="Close Wrike dialog" onClick={() => setWrikeDialog(null)}>×</button></div>
        <p className="ud-sub">{wrikeDialog.title}</p>
        <WrikeTaskAction key={wrikeDialog.id} opportunityId={wrikeDialog.id} opportunityTitle={wrikeDialog.title} readOnly={readOnly} onCreated={() => setWrikeRevision(value => value + 1)} />
      </div>
    </div>}
  </div>;
}

function Bars({ title, subtitle, values, selected, onSelect, cyan = false }: { title: string; subtitle: string; values: Record<string, number>; selected: string[]; onSelect: (name: string) => void; cyan?: boolean }) {
  const entries = Object.entries(values).slice(0, 8);
  const max = Math.max(1, ...entries.map(([, value]) => value));
  return <section className={`ud-panel ud-chart ${cyan ? "ud-cyan-bars" : ""}`}><h2>{title}</h2><p className="ud-sub">{subtitle}</p><div className="ud-bars">{entries.map(([name, value]) => <button key={name} aria-pressed={selected.includes(name)} onClick={() => onSelect(name)} title={`${name}: ${count(value)} opportunities`}><span><span>{shortVertical(name)}</span><b>{count(value)}</b></span><i><i style={{ width: `${value / max * 100}%` }} /></i></button>)}</div>{!entries.length && <p className="ud-sub">No data in this view.</p>}</section>;
}
