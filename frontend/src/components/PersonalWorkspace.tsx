import { useEffect, useState, type FormEvent } from "react";
import "./personal-workspace.css";
import { AccountManagement } from "./AccountManagement";

type Profile = { title: string; accent: string; keywords: string; verticals: string; countries: string; experience: string; registrations: string };
type Opportunity = { id: number; title: string; organization: string; category: string; deadline: string; summary: string; url: string; country: string };
type Journey = { id: number; opportunity: Opportunity; stage: string; notes: string; factors: string[]; next_action: string; next_action_date: string | null };
type Contact = { id?: number; name: string; organization: string; role: string; email: string; tags: string; strength: string; notes: string };
type Suggestion = Opportunity & { score: number; reasons: string[]; contacts: { id: number; name: string; organization: string; reason: string }[] };
type Details = { events: { id: number; stage: string; note: string; created_at: string }[]; attachments: { id: number; filename: string }[] };
const stages = ["Saved", "Preparing", "Applied", "Shortlisted", "Accepted", "Unsuccessful", "Withdrawn"];
const factors = ["Funder relationship", "Relevant experience", "Registration/licence", "Eligibility gap", "Proposal quality", "Budget", "Competition", "Other"];
const emptyContact: Contact = { name: "", organization: "", role: "", email: "", tags: "", strength: "Known", notes: "" };
const initialProfile: Profile = { title: "My opportunity journey", accent: "blue", keywords: "", verticals: "", countries: "", experience: "", registrations: "" };

async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch("/api/workspace" + path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : "Please check your input and try again.");
  }
  return response.json();
}

export function PersonalWorkspace({ isAdmin }: { isAdmin: boolean }) {
  const [session, setSession] = useState<{ owner: string; unlocked: boolean; configured: boolean; development: boolean; read_only: boolean } | null>(null);
  const [profile, setProfile] = useState<Profile>(initialProfile);
  const [journeys, setJourneys] = useState<Journey[]>([]);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [contact, setContact] = useState<Contact>(emptyContact);
  const [recommendations, setRecommendations] = useState<Suggestion[]>([]);
  const [method, setMethod] = useState("");
  const [tab, setTab] = useState("journey");
  const [selected, setSelected] = useState<Journey | null>(null);
  const [details, setDetails] = useState<Details>({ events: [], attachments: [] });



  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const pending = new URLSearchParams(location.search).get("opportunity");
  const writeDisabled = busy || !!session?.read_only;
  async function run(task: () => Promise<void>, success = "") {
    setBusy(true); setError(""); setMessage("");
    try { await task(); setMessage(success); } catch (e) { setError(e instanceof Error ? e.message : "Request failed"); } finally { setBusy(false); }
  }
  function clearPrivate() {
    setJourneys([]); setContacts([]); setSelected(null); setRecommendations([]);
    setProfile(initialProfile); setContact({ ...emptyContact }); setDetails({ events: [], attachments: [] });
  }
  async function refreshSession() {
    try {
      const value = await api<NonNullable<typeof session>>("/session");
      if (!value.unlocked) clearPrivate();
      setSession(value);
    } catch (e) { clearPrivate(); setSession(null); throw e; }
  }
  async function load() {
    const [p, j, c] = await Promise.all([api<Profile>("/profile"), api<Journey[]>("/journeys"), api<Contact[]>("/contacts")]);
    setProfile(p); setJourneys(j); setContacts(c);
  }
  async function suggest() {
    const result = await api<{ items: Suggestion[]; method: string }>("/recommendations?q=" + encodeURIComponent(search));
    setRecommendations(result.items); setMethod(result.method);
  }
  useEffect(() => { void run(refreshSession); }, []);
  useEffect(() => { if (session?.unlocked) void run(load); }, [session?.unlocked]);
  useEffect(() => {
    const check = () => { if (document.visibilityState === "visible") void refreshSession().catch(e => setError(e.message)); };
    window.addEventListener("pageshow", check);
    document.addEventListener("visibilitychange", check);
    const timer = window.setInterval(check, 60000);
    return () => { window.clearInterval(timer); window.removeEventListener("pageshow", check); document.removeEventListener("visibilitychange", check); };
  }, []);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    setDetails({ events: [], attachments: [] });
    api<Details>(`/journeys/${selected.id}/details`).then(d => { if (active) setDetails(d); }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [selected?.id]);
  const submit = (fn: () => Promise<void>, success = "") => (e: FormEvent) => { e.preventDefault(); void run(fn, success); };
  async function track(id: number) {
    const row = await api<Journey>("/journeys", "POST", { opportunity_id: id });
    setSelected(row); setTab("journey"); await load();
    const url = new URL(location.href); url.searchParams.delete("opportunity"); history.replaceState({}, "", url);
  }
  async function saveJourney() {
    if (!selected) return;
    const { stage, notes, factors, next_action, next_action_date } = selected;
    const row = await api<Journey>(`/journeys/${selected.id}`, "PUT", { stage, notes, factors, next_action, next_action_date: next_action_date || null });
    setSelected(row); setJourneys(await api("/journeys")); setDetails(await api(`/journeys/${row.id}/details`));
  }
  const decided = journeys.filter(j => ["Accepted", "Unsuccessful"].includes(j.stage));
  const wins = journeys.filter(j => j.stage === "Accepted").length;
  const filtered = journeys.filter(j => !filter || j.stage === filter);
  return <main className={`pw pw-${profile.accent}`}>
    <header className="pw-header"><a href="?view=user" className="pw-logo">CMS</a><div><h1>{profile.title}</h1><p>{session?.owner || "Personal workspace"}</p></div><nav><a href="?view=user">Opportunities</a>{isAdmin && <a href="?view=admin">Admin dashboard</a>}</nav></header>
    <div aria-live="polite">{error && <p role="alert" className="pw-error">{error}</p>}{message && <p className="pw-success">{message}</p>}{busy && <p role="status">Working…</p>}</div>
    {session?.development && <p className="pw-notice">Development workspace: authentication is disabled. Enable individual workspace passwords before using real private records on a shared server.</p>}
    {session?.read_only && <p className="pw-notice">Read-only mode: changes and uploads are unavailable.</p>}
    {session?.unlocked && <>
      <section className="pw-metrics"><article><span>My applications</span><strong>{journeys.length}</strong></article><article><span>Applied or shortlisted</span><strong>{journeys.filter(j => ["Applied", "Shortlisted"].includes(j.stage)).length}</strong></article><article><span>Accepted</span><strong>{wins}</strong></article><article><span>Historical strike rate</span><strong>{decided.length ? Math.round(wins / decided.length * 100) + "%" : "—"}</strong><small>{decided.length} decided applications · not a forecast</small></article></section>
      <nav className="pw-tabs" aria-label="Workspace sections">{[["journey", "My journey"], ["discover", "Recommended for me"], ["network", "My network"], ["preferences", "Personalise"]].map(([key, label]) => <button key={key} aria-pressed={tab === key} onClick={() => { setTab(key); if (key === "discover") void run(suggest); }}>{label}</button>)}</nav>
      {pending && <section className="pw-card"><p>Save the opportunity you selected to your personal journey.</p><button className="pw-primary" disabled={writeDisabled} onClick={() => void run(() => track(Number(pending)), "Opportunity added to your journey")}>Track selected opportunity</button></section>}
      {tab === "journey" && <div className="pw-layout"><section className="pw-card"><div className="pw-title"><h2>Application pipeline</h2><select aria-label="Filter applications by stage" value={filter} onChange={e => setFilter(e.target.value)}><option value="">All stages</option>{stages.map(s => <option key={s}>{s}</option>)}</select></div>{filtered.length === 0 && <div className="pw-empty"><h3>Your next opportunity starts here</h3><p>Open Recommended for me, or select Track in my workspace from an opportunity brief.</p><button onClick={() => { setTab("discover"); void run(suggest); }}>Find opportunities</button></div>}{filtered.map(j => <button className={`pw-application ${selected?.id === j.id ? "pw-selected" : ""}`} key={j.id} onClick={() => setSelected(j)}><span className="pw-badge">{j.stage}</span><h3>{j.opportunity.title}</h3><p>{j.opportunity.organization || "Organisation not listed"}</p><small>Deadline: {j.opportunity.deadline || "Not listed"}{j.next_action_date && ` · Next action: ${j.next_action_date}`}</small></button>)}</section>
      <aside className="pw-card">{selected ? <><h2>{selected.opportunity.title}</h2><p>{selected.opportunity.summary}</p>{/^https?:\/\//i.test(selected.opportunity.url) && <a href={selected.opportunity.url} target="_blank" rel="noreferrer">Open original opportunity ↗</a>}<form onSubmit={submit(saveJourney, "Application saved")}><fieldset disabled={writeDisabled}><label>Application stage<select value={selected.stage} onChange={e => setSelected({ ...selected, stage: e.target.value })}>{stages.map(s => <option key={s}>{s}</option>)}</select></label><label>Application notes / why we won or lost<textarea rows={5} maxLength={20000} value={selected.notes} onChange={e => setSelected({ ...selected, notes: e.target.value })} /></label><div className="pw-factors"><span>Contributing factors</span>{factors.map(f => <label key={f}><input type="checkbox" checked={selected.factors.includes(f)} onChange={e => setSelected({ ...selected, factors: e.target.checked ? [...selected.factors, f] : selected.factors.filter(v => v !== f) })} />{f}</label>)}</div><label>Next action<input maxLength={2000} value={selected.next_action} onChange={e => setSelected({ ...selected, next_action: e.target.value })} /></label><label>Next action date<input type="date" value={selected.next_action_date || ""} onChange={e => setSelected({ ...selected, next_action_date: e.target.value || null })} /></label><button className="pw-primary">Save application</button></fieldset></form>
      <h3>Proposal & supporting files</h3><p className="pw-muted">5 MB per file · 10 files per application · 50 MB per workspace</p><label>Attach a file<input type="file" disabled={writeDisabled} onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; if (!file) return; void run(async () => { if (file.size > 5 * 1024 * 1024) throw new Error("File must be 5 MB or smaller"); const r = await fetch(`/api/workspace/journeys/${selected.id}/attachments`, { method: "POST", headers: { "X-Filename": encodeURIComponent(file.name), "Content-Type": "application/octet-stream" }, body: file }); if (!r.ok) { const body = await r.json(); throw new Error(body.detail || "Upload failed"); } setDetails(await api(`/journeys/${selected.id}/details`)); }, "File attached"); }} /></label>{details.attachments.map(a => <div className="pw-file" key={a.id}><a href={`/api/workspace/journeys/${selected.id}/attachments/${a.id}`}>{a.filename}</a><button disabled={writeDisabled} onClick={() => { if (confirm(`Delete ${a.filename}?`)) void run(async () => { await api(`/journeys/${selected.id}/attachments/${a.id}`, "DELETE"); setDetails(await api(`/journeys/${selected.id}/details`)); }, "Attachment deleted"); }}>Delete</button></div>)}
      <h3>Journey timeline</h3><ol className="pw-timeline">{details.events.map(e => <li key={e.id}><strong>{e.stage}</strong> — {e.note}<small>{new Date(e.created_at + (/Z$|[+]\d\d:\d\d$/.test(e.created_at) ? "" : "Z")).toLocaleString()}</small></li>)}</ol></> : <div className="pw-empty">Select an application to update your journey.</div>}</aside></div>}
      {tab === "discover" && <section className="pw-card"><h2>Opportunities with a reason to pursue</h2><p>{method || "Personal matches combine your interests, outcome history and contacts."}</p><form className="pw-search" onSubmit={submit(suggest)}><input aria-label="Search recommendations" placeholder="Search title or offering organisation…" value={search} maxLength={200} onChange={e => setSearch(e.target.value)} /><button disabled={busy}>Search</button></form><div className="pw-recommendations">{recommendations.map(o => <article key={o.id}><span className="pw-badge">{o.category} · {o.deadline}</span><h3>{o.title}</h3><p>{o.organization} {o.country && `· ${o.country}`}</p><ul>{o.reasons.map(r => <li key={r}>{r}</li>)}</ul>{o.contacts.map(c => <button className="pw-contact-link" key={c.id} onClick={() => { setContact(contacts.find(x => x.id === c.id) || emptyContact); setTab("network"); }}>{c.name} · {c.reason}</button>)}<button disabled={writeDisabled} className="pw-primary" onClick={() => void run(() => track(o.id), "Opportunity added to your journey")}>{journeys.some(j => j.opportunity.id === o.id) ? "Open my application" : "Track opportunity"}</button></article>)}</div>{!busy && !recommendations.length && <p>No matching active opportunities. Try another search.</p>}</section>}
      {tab === "network" && <div className="pw-layout"><section className="pw-card"><h2>My relationship network</h2><p>Contacts stay in your workspace. Suggestions use organisation and expertise matches.</p><button onClick={() => setContact({ ...emptyContact })}>Add contact</button>{contacts.map(c => <button className="pw-application" key={c.id} onClick={() => setContact({ ...c })}><h3>{c.name}</h3><p>{c.organization} · {c.role}</p><span className="pw-badge">{c.strength}</span></button>)}</section><form className="pw-card" onSubmit={submit(async () => { await api(contact.id ? `/contacts/${contact.id}` : "/contacts", contact.id ? "PUT" : "POST", Object.fromEntries(Object.entries(contact).filter(([k]) => k !== "id"))); setContacts(await api("/contacts")); setContact({ ...emptyContact }); }, "Contact saved")}><h2>{contact.id ? "Edit contact" : "Add a contact"}</h2><fieldset disabled={writeDisabled}>{(["name", "organization", "role", "email", "tags", "notes"] as const).map(key => <label key={key}>{({ name: "Name", organization: "Organisation", role: "Role", email: "Email", tags: "Expertise tags (comma-separated)", notes: "Relationship notes" })[key]}<input required={key === "name"} type={key === "email" ? "email" : "text"} maxLength={key === "notes" ? 10000 : key === "tags" ? 2000 : key === "organization" ? 300 : key === "email" ? 320 : 200} value={contact[key]} onChange={e => setContact({ ...contact, [key]: e.target.value })} /></label>)}<label>Relationship strength<select value={contact.strength} onChange={e => setContact({ ...contact, strength: e.target.value })}>{["Known", "Warm", "Strong"].map(s => <option key={s}>{s}</option>)}</select></label><button className="pw-primary">Save contact</button>{contact.id && <button type="button" onClick={() => { if (confirm("Delete this contact?")) void run(async () => { await api(`/contacts/${contact.id}`, "DELETE"); setContacts(await api("/contacts")); setContact({ ...emptyContact }); }, "Contact deleted"); }}>Delete contact</button>}</fieldset></form></div>}
      {tab === "preferences" && <form className="pw-card pw-preferences" onSubmit={submit(async () => { setProfile(await api("/profile", "PUT", profile)); }, "Personalisation saved")}><h2>Make this workspace yours</h2><p>Your preferences and organisation experience stay with your account across devices. Experience and registrations are stored for your review; the current ranking uses interests, outcomes and contacts.</p><fieldset disabled={writeDisabled}>{(["title", "keywords", "verticals", "countries", "experience", "registrations"] as const).map(key => <label key={key}>{({ title: "Workspace name", keywords: "Interests (comma-separated)", verticals: "Preferred CMS verticals (comma-separated)", countries: "Preferred countries (comma-separated)", experience: "Relevant experience and capabilities", registrations: "Registrations and licences" })[key]}<textarea rows={key === "experience" || key === "registrations" ? 4 : 2} maxLength={key === "title" ? 120 : key === "experience" || key === "registrations" ? 10000 : 2000} required={key === "title"} value={profile[key]} onChange={e => setProfile({ ...profile, [key]: e.target.value })} /></label>)}<label>Colour theme<select value={profile.accent} onChange={e => setProfile({ ...profile, accent: e.target.value })}>{["blue", "teal", "violet"].map(s => <option key={s}>{s}</option>)}</select></label><button className="pw-primary">Save preferences</button></fieldset></form>}
    </>}
    {isAdmin && <AccountManagement />}
  </main>;
}
