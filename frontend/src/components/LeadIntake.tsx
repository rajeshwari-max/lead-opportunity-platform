import { useEffect, useState } from "react";
import { DirectoryEntry, leadRequest } from "./DirectoryPanel";

export function ManualLeadForm({ readOnly = false, initiallyOpen = false }: { readOnly?: boolean; initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen), [resources, setResources] = useState<DirectoryEntry[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [category, setCategory] = useState("Other");
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    leadRequest("/directories/organisation").then(rows => { if (!cancelled) setResources(rows.filter((r: DirectoryEntry) => r.role === "resource" && r.status === "active")); })
      .catch(() => { if (!cancelled) setError("Resource organisations could not be loaded. You can still save a lead without one."); });
    return () => { cancelled = true; };
  }, [open]);
  return <div className="p-4 border rounded-lg my-4">
    <button type="button" className="ud-control" aria-expanded={open} disabled={readOnly} onClick={() => setOpen(!open)}>Add a lead manually</button>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {open && <form className="mt-4" onSubmit={async e => {
      e.preventDefault(); const form = e.currentTarget, fields = new FormData(form);
      const body: Record<string, unknown> = {};
      for (const name of ["title", "organization", "opportunity_url", "summary", "country", "location", "eligibility", "funding_amount", "next_action"]) body[name] = String(fields.get(name) || "").trim();
      body.category = category; body.deadline = fields.get("deadline") || null;
      body.next_action_date = fields.get("next_action_date") || null; body.rolling = fields.get("rolling") === "on";
      body.resource_organisation_id = fields.get("resource_organisation_id") ? Number(fields.get("resource_organisation_id")) : null;
      body.award_type = category === "Award" ? fields.get("award_type") : "unknown";
      body.award_evidence = category === "Award" ? fields.get("award_evidence") : "";
      setBusy(true); setError(""); setNotice("");
      try {
        const saved = await leadRequest("/my-leads/manual", "POST", body);
        form.reset(); setCategory("Other"); setOpen(false);
        setNotice(saved.reused_existing ? "Existing lead saved to My leads; its details and existing follow-up were retained." : "Lead saved. It will appear in Follow-ups due on its follow-up date.");
        window.dispatchEvent(new Event("saved-leads-changed"));
      } catch (e) { setError(e instanceof Error ? e.message : "Could not save lead"); } finally { setBusy(false); }
    }}><fieldset disabled={busy || readOnly} className="grid gap-3 sm:grid-cols-2">
      <legend className="mb-3">Save an opportunity now and return to it later. Manual lead details are shared with the team; your follow-up notes belong to your account.</legend>
      <label>Lead title<input className="ud-input" name="title" required minLength={3} maxLength={1000} /></label>
      <label>Organisation<input className="ud-input" name="organization" maxLength={512} /></label>
      <label>Source URL<input className="ud-input" name="opportunity_url" type="url" maxLength={2048} /></label>
      <label>Opportunity type<select className="ud-input" value={category} onChange={e => setCategory(e.target.value)}>{["Grant", "RFP", "Tender", "Proposal", "Fellowship", "Award", "Challenge", "Other"].map(c => <option key={c}>{c}</option>)}</select></label>
      <label>Country<input className="ud-input" name="country" maxLength={128} /></label>
      <label>Location<input className="ud-input" name="location" maxLength={512} /></label>
      <label>Deadline (leave blank if unknown)<input className="ud-input" type="date" name="deadline" /></label>
      <label className="flex items-center gap-2"><input type="checkbox" name="rolling" /> Source explicitly says rolling applications</label>
      <label>Summary<textarea className="ud-input" name="summary" maxLength={20000} /></label>
      <label>Eligibility<textarea className="ud-input" name="eligibility" maxLength={10000} /></label>
      <label>Funding amount<input className="ud-input" name="funding_amount" maxLength={256} /></label>
      <label>Resource organisation<select className="ud-input" name="resource_organisation_id"><option value="">None</option>{resources.map(r => <option key={r.id} value={r.id}>{r.name} · {r.location}</option>)}</select></label>
      {category === "Award" && <><label>Award benefit<select className="ud-input" name="award_type"><option value="unknown">Not confirmed</option><option value="monetary">Money</option><option value="recognition">Trophy / recognition</option><option value="both">Money and recognition</option></select></label><label>Award evidence<textarea className="ud-input" name="award_evidence" maxLength={10000} placeholder="Quote or describe the source’s stated benefit" /></label></>}
      <label>Next action<input className="ud-input" name="next_action" maxLength={2000} /></label>
      <label>Bring this lead back on<input className="ud-input" type="date" name="next_action_date" /></label>
      <button type="submit" className="ud-primary-button">{busy ? "Saving…" : "Save lead"}</button>
    </fieldset></form>}
  </div>;
}

export function LeadFollowUps() {
  const [rows, setRows] = useState<{ id: number; opportunity: { title: string }; next_action: string; next_action_date: string }[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    const load = () => { void leadRequest("/my-leads/due").then(data => { if (!cancelled) { setRows(data); setError(""); } }).catch(() => { if (!cancelled) setError("Follow-ups could not be loaded."); }); };
    load(); window.addEventListener("saved-leads-changed", load); const timer = window.setInterval(load, 60000);
    return () => { cancelled = true; window.removeEventListener("saved-leads-changed", load); window.clearInterval(timer); };
  }, []);
  return <div className="p-4 border rounded-lg my-4"><h3>Follow-ups due ({rows.length})</h3>{error && <p role="alert">{error}</p>}
    {!rows.length && !error && <p>No lead follow-ups due.</p>}{rows.map(r => <p key={r.id}><strong>{r.opportunity.title}</strong> · {r.next_action_date} · {r.next_action || "Review this lead"}</p>)}
  </div>;
}

export function AwardBenefit({ opportunityId, isAdmin = false, readOnly = false }: { opportunityId: number; isAdmin?: boolean; readOnly?: boolean }) {
  const [value, setValue] = useState({ award_type: "unknown", award_evidence: "" }), [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false; setError(""); setNotice(""); setValue({ award_type: "unknown", award_evidence: "" });
    leadRequest(`/my-leads/awards/${opportunityId}`).then(data => { if (!cancelled) setValue(data); }).catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [opportunityId]);
  const labels: Record<string, string> = { unknown: "Not confirmed", monetary: "Money", recognition: "Trophy / recognition", both: "Money and recognition" };
  return <section className="p-4 border rounded-lg"><h3>Award benefit</h3>{error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {isAdmin && !readOnly ? <form onSubmit={async e => { e.preventDefault(); setBusy(true); setError(""); setNotice(""); try { await leadRequest(`/my-leads/awards/${opportunityId}`, "PUT", value); setNotice("Award benefit saved"); } catch (e) { setError(e instanceof Error ? e.message : "Could not save"); } finally { setBusy(false); } }}><fieldset disabled={busy}><label>Benefit<select className="ud-input" value={value.award_type} onChange={e => setValue({ ...value, award_type: e.target.value })}>{Object.entries(labels).map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select></label><label>Source evidence<textarea className="ud-input" required={value.award_type !== "unknown"} maxLength={10000} value={value.award_evidence} onChange={e => setValue({ ...value, award_evidence: e.target.value })} /></label><button className="ud-control">Save award benefit</button></fieldset></form> : <><p>{labels[value.award_type]}</p><p>{value.award_evidence}</p></>}
  </section>;
}
