import { useEffect, useState } from "react";
import { VERTICALS } from "@/lib/types";

export async function leadRequest(path: string, method = "GET", body?: unknown) {
  const response = await fetch(`/api${path}`, { method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Check the form and try again.");
  return data;
}

type Kind = "keyword" | "website" | "organisation";
export type DirectoryEntry = { id: number; kind: Kind; name: string; scope: string; url: string;
  institution: string; location: string; role: string; status: string; notes: string;
  review_date: string | null; review_due?: boolean; reviewed_at?: string | null;
  performance?: { runs: number; found: number; saved: number; last_attempt: string | null } };
type History = { action: string; note: string; actor: string; created_at: string };
const blank = (): Omit<DirectoryEntry, "id" | "kind"> => ({ name: "", scope: "", url: "", institution: "", location: "", role: "", status: "active", notes: "", review_date: null });

export function DirectoryPanel({ isAdmin, readOnly, initiallyOpen = false }: { isAdmin: boolean; readOnly: boolean; initiallyOpen?: boolean }) {
  const [kind, setKind] = useState<Kind>("keyword"), [rows, setRows] = useState<DirectoryEntry[]>([]);
  const [open, setOpen] = useState(initiallyOpen), [query, setQuery] = useState("");
  const [edit, setEdit] = useState<ReturnType<typeof blank>>(blank), [id, setId] = useState<number | null>(null);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<History[]>([]), [action, setAction] = useState("reviewed"), [note, setNote] = useState("");
  const writable = isAdmin && !readOnly;
  async function load() { setRows(await leadRequest(`/directories/${kind}`)); }
  async function run(fn: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : "Request failed"); } finally { setBusy(false); }
  }
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setBusy(true); setError(""); setRows([]);
    leadRequest(`/directories/${kind}`).then(data => { if (!cancelled) setRows(data); })
      .catch(e => { if (!cancelled) setError(e.message); }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [open, kind]);
  function switchKind(value: Kind) { setKind(value); setEdit(blank()); setId(null); setEvents([]); setNote(""); setQuery(""); }
  const filtered = rows.filter(r => `${r.name} ${r.scope} ${r.institution} ${r.location}`.toLowerCase().includes(query.toLowerCase()));
  return <section className="dl-panel" id="directories">
    <button className="dl-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>Directories <span>Keywords, websites and organisations</span></button>
    {open && <div className="p-4 space-y-4">
      <div className="flex flex-wrap gap-3">{(["keyword", "website", "organisation"] as Kind[]).map(k => <button type="button" className="ud-control" aria-pressed={kind === k} disabled={busy} key={k} onClick={() => switchKind(k)}>{k === "keyword" ? "Keywords" : k === "website" ? "Websites" : "Organisations"}</button>)}</div>
      {kind === "keyword" && <p>Active terms feed the keyword classifier. Core classification rules also apply. Reviewed labels stay protected; changes do not retrain the ML model.</p>}
      {kind === "website" && <p>Track source performance and follow-ups. Paused or dropped websites are excluded from new scrape runs. Candidate websites need a scraper before they can run.</p>}
      {kind === "organisation" && <p>Keep preferred organisations and resource organisations with institution and location. This directory is separate from the company profile’s excluded organisations.</p>}
      <p>{rows.filter(r => r.review_due).length} reviews due</p>
      {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      <label className="block">Search directory<input className="ud-input" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="max-h-[480px] overflow-y-auto space-y-2">{!busy && !filtered.length && <p>No matching entries.</p>}{filtered.map(row => <button type="button" disabled={busy} className="dl-lead w-full text-left" key={row.id} onClick={() => void run(async () => {
          setId(row.id); const { name, scope, url, institution, location, role, status, notes, review_date } = row;
          setEdit({ name, scope, url, institution, location, role, status, notes, review_date }); setNote(""); setEvents([]);
          setEvents(await leadRequest(`/directories/${kind}/${row.id}/events`));
        })}><strong>{row.name}</strong><span>{row.status} · {row.scope || row.role || "Candidate"}{row.review_due ? " · Review due" : ""}</span>
          {kind === "website" && <small>{row.performance?.runs || 0} runs · {row.performance?.found || 0} found · {row.performance?.saved || 0} saved · Last attempt: {row.performance?.last_attempt?.slice(0, 10) || "None recorded"}</small>}
          {kind === "organisation" && <small>{row.institution} · {row.location}</small>}</button>)}</div>
        <div>
          {writable && <button type="button" className="ud-control" disabled={busy} onClick={() => { setId(null); setEdit(blank()); setEvents([]); setNote(""); }}>New entry</button>}
          {(writable || id !== null) && <form className="space-y-3" onSubmit={e => { e.preventDefault(); void run(async () => {
            const saved = await leadRequest(`/directories/${kind}${id ? `/${id}` : ""}`, id ? "PUT" : "POST", edit);
            setId(saved.id); await load(); setEvents(await leadRequest(`/directories/${kind}/${saved.id}/events`)); setNotice("Directory entry saved");
          }); }}><fieldset disabled={busy || !writable} className="space-y-3">
            <label className="block">Name<input className="ud-input" required maxLength={256} value={edit.name} onChange={e => setEdit({ ...edit, name: e.target.value })} /></label>
            {kind === "keyword" ? <label className="block">Vertical<select className="ud-input" required value={edit.scope} onChange={e => setEdit({ ...edit, scope: e.target.value })}><option value="">Choose vertical</option>{VERTICALS.map(v => <option key={v}>{v}</option>)}</select></label> : <>
              <label className="block">Website URL<input className="ud-input" type="url" maxLength={2048} value={edit.url} onChange={e => setEdit({ ...edit, url: e.target.value })} /></label>
              {kind === "website" ? <label className="block">Installed scraper key (blank for candidate)<input className="ud-input" value={edit.scope} onChange={e => setEdit({ ...edit, scope: e.target.value })} /></label> : <>
                <label className="block">Organisation role<select className="ud-input" required value={edit.role} onChange={e => setEdit({ ...edit, role: e.target.value })}><option value="">Choose role</option><option value="whitelist">Preferred / whitelist</option><option value="resource">Resource organisation</option></select></label>
                <label className="block">Institution<input className="ud-input" maxLength={256} value={edit.institution} onChange={e => setEdit({ ...edit, institution: e.target.value })} /></label>
                <label className="block">Location<input className="ud-input" maxLength={256} value={edit.location} onChange={e => setEdit({ ...edit, location: e.target.value })} /></label>
              </>}
            </>}
            <label className="block">Status<select className="ud-input" value={edit.status} onChange={e => setEdit({ ...edit, status: e.target.value })}>{["active", "review", "paused", "dropped"].map(s => <option key={s}>{s}</option>)}</select></label>
            <label className="block">Next review date<input className="ud-input" type="date" value={edit.review_date || ""} onChange={e => setEdit({ ...edit, review_date: e.target.value || null })} /></label>
            <label className="block">Notes / decision reason<textarea className="ud-input" required={edit.status === "dropped"} maxLength={10000} value={edit.notes} onChange={e => setEdit({ ...edit, notes: e.target.value })} /></label>
            {writable && <button className="ud-primary-button" type="submit">Save entry</button>}
          </fieldset></form>}
          {id !== null && <>
            {writable && <form className="mt-4 space-y-3" onSubmit={e => { e.preventDefault(); void run(async () => {
              await leadRequest(`/directories/${kind}/${id}/events`, "POST", { action, note, review_date: edit.review_date });
              setNote(""); setEvents(await leadRequest(`/directories/${kind}/${id}/events`)); await load(); setNotice("Follow-up recorded");
            }); }}><fieldset disabled={busy}><legend>Record follow-up</legend><label className="block">Action<select className="ud-input" value={action} onChange={e => setAction(e.target.value)}>{["pinged", "reviewed", "no_response", "response_received"].map(s => <option value={s} key={s}>{s.replace(/_/g, " ")}</option>)}</select></label><label className="block">Follow-up note<textarea className="ud-input" required maxLength={10000} value={note} onChange={e => setNote(e.target.value)} /></label><button type="submit" className="ud-control">Record follow-up</button></fieldset></form>}
            <h3 className="mt-4 font-semibold">History</h3><ul>{events.map((ev, i) => <li key={i} className="py-2 border-b">{ev.created_at.slice(0, 10)} · {ev.action} · {ev.actor}<p>{ev.note}</p></li>)}</ul>
          </>}
        </div>
      </div>
    </div>}
  </section>;
}
