import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { formatFundingAmount } from "@/lib/money";
import { formatDate } from "@/lib/utils";
import { emptyFilters, type Opportunity, type WrikeAssignee } from "@/lib/types";
import "./product-tour.css";

/**
 * "How to use this platform": a short product tour, built like the CSR
 * dashboard's guide (Step N of M, progress bars, Back / Next / Done, Esc).
 *
 * It is a live demo on ONE real lead — the one open in the brief, or else the
 * first lead in the current list — so people see their own data, not a
 * made-up example.
 *
 * The Wrike step is a practice copy of the real Wrike panel. It never calls
 * the create-task endpoint: the final button is disabled and says so. The
 * only Wrike request it makes is the same read-only member list the real
 * panel loads (GET /wrike/assignees), and only when that step is reached.
 *
 * Open it from the link, or with a #tour link (e.g. https://leads.catalysts.org/#tour)
 * to send to someone new.
 */

type Assignees = { state: "idle" | "loading" | "ready" | "error"; items: WrikeAssignee[] };

const split = (value: string | undefined) => (value || "").split(",").map(v => v.trim()).filter(Boolean);

function daysLeftLabel(value: string | null) {
  if (!value) return "No fixed closing date";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((new Date(value.slice(0, 10) + "T00:00:00").getTime() - today.getTime()) / 86400000);
  return days < 0 ? "Closed" : days === 0 ? "Closes today" : days === 1 ? "Closes tomorrow" : `${days} days left`;
}

export function ProductTourLink({ lead, className, label }: { lead?: Opportunity | null; className?: string; label?: string }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (window.location.hash !== "#tour") return;
    history.replaceState({}, "", `${window.location.pathname}${window.location.search}`);
    setOpen(true);
  }, []);
  return <>
    <button type="button" className={className ?? "lt-tour-link"} onClick={() => setOpen(true)}>
      {label ?? <>How to use this platform · <span>Take a tour</span></>}
    </button>
    {/* Portal: the admin header uses backdrop-filter, which would otherwise
        trap a position:fixed overlay inside the header bar. */}
    {open && createPortal(<ProductTour lead={lead ?? null} onClose={() => setOpen(false)} />, document.body)}
  </>;
}

export function ProductTour({ lead: given, onClose }: { lead: Opportunity | null; onClose: () => void }) {
  const [step, setStep] = useState(0);
  const [lead, setLead] = useState<Opportunity | null>(given);
  const [assignees, setAssignees] = useState<Assignees>({ state: "idle", items: [] });
  const panel = useRef<HTMLDivElement>(null);

  // No lead on screen (e.g. still loading): fetch the first live one.
  useEffect(() => {
    if (given) { setLead(given); return; }
    let cancelled = false;
    api.opportunities({ ...emptyFilters, page_size: 1 })
      .then(page => { if (!cancelled) setLead(page.items[0] ?? null); })
      .catch(() => { /* the tour still works with generic wording */ });
    return () => { cancelled = true; };
  }, [given]);

  const asked = useRef(false);
  const loadAssignees = useCallback(() => {
    if (asked.current) return;
    asked.current = true;
    setAssignees({ state: "loading", items: [] });
    api.wrikeAssignees()
      .then(items => setAssignees({ state: "ready", items }))
      .catch(() => setAssignees({ state: "error", items: [] }));
  }, []);

  const steps = buildSteps(lead, assignees);
  const last = step === steps.length - 1;
  const go = useCallback((to: number) => setStep(Math.max(0, Math.min(steps.length - 1, to))), [steps.length]);

  useEffect(() => { if (steps[step].needsAssignees) loadAssignees(); }, [step, steps, loadAssignees]);
  useEffect(() => { panel.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      const typing = event.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
      if (typing) return;
      if (event.key === "ArrowRight") go(step + 1);
      if (event.key === "ArrowLeft") go(step - 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, go, step]);

  const current = steps[step];
  return <div className="lt-tour-overlay" onClick={onClose}>
    <div className="lt-tour" ref={panel} tabIndex={-1} role="dialog" aria-modal="true"
         aria-labelledby="lt-tour-title" onClick={event => event.stopPropagation()}>
      <div className="lt-tour-head">
        <div>
          <h2 id="lt-tour-title">Welcome to the Lead Scanning Platform</h2>
          <p>Product tour · one real lead, from finding it to pushing it to Wrike.</p>
        </div>
        <button type="button" className="lt-tour-close" onClick={onClose} aria-label="Close tour">×</button>
      </div>
      <p className="lt-tour-count">Step {step + 1} of {steps.length}</p>
      <h3 className="lt-tour-step-title">{current.title}</h3>
      <p className="lt-tour-text">{current.text}</p>
      {current.body && <div className="lt-tour-body">{current.body}</div>}
      <div className="lt-tour-progress">
        {steps.map((item, index) => <button key={item.title} type="button"
          aria-label={`Step ${index + 1}: ${item.title}`} aria-current={index === step ? "step" : undefined}
          className={index <= step ? "is-done" : ""} onClick={() => go(index)} />)}
      </div>
      <div className="lt-tour-nav">
        <button type="button" className="lt-tour-btn" onClick={() => go(step - 1)} disabled={step === 0}>Back</button>
        <button type="button" className="lt-tour-btn lt-tour-btn-primary" onClick={() => (last ? onClose() : go(step + 1))}>
          {last ? "Done" : "Next"}
        </button>
      </div>
    </div>
  </div>;
}

type Step = { title: string; text: string; body?: React.ReactNode; needsAssignees?: boolean };

function buildSteps(lead: Opportunity | null, assignees: Assignees): Step[] {
  const title = lead?.title ?? "the lead";
  const brands = split(lead?.brands);
  const brand = brands.find(b => b !== "CMS") ?? brands[0];
  return [
    {
      title: "Find leads",
      text: "Search by keyword, click a stat card (Grants, RFPs, Tenders, New today) or use the filters on the left: brand, CMS vertical, region, country and category. Every lead in the list is open and its closing date has not passed.",
      body: lead && <div className="lt-tour-chips" aria-label="Example filters">
        <span>Category: {lead.category}</span>
        {brand && <span>Brand: {brand}</span>}
        {lead.country && <span>Country: {lead.country}</span>}
      </div>,
    },
    {
      title: "Open a lead",
      text: "Click a lead's title or View. Its brief opens on the right. This tour uses the lead below.",
      body: <LeadCard lead={lead} />,
    },
    {
      title: "Read the brief",
      text: "The brief shows the closing date, amount, brands and verticals, and Platform intelligence (how well it fits us and who could work on it). Open original source takes you to the funder's own page to check the details.",
      body: lead && <dl className="lt-tour-facts">
        <div><dt>Closes</dt><dd>{lead.deadline ? formatDate(lead.deadline) : "Ongoing"} · {daysLeftLabel(lead.deadline)}</dd></div>
        <div><dt>Source</dt><dd>{lead.source_website || "—"}</dd></div>
        <div><dt>Amount</dt><dd>{formatFundingAmount(lead.funding_amount) || "Not stated"}</dd></div>
        <div><dt>Labels</dt><dd>{[...split(lead.brands), ...split(lead.verticals)].join(" · ") || "—"}</dd></div>
        {(lead.link || lead.opportunity_url) && <div><dt>Link</dt><dd><a href={lead.link || lead.opportunity_url} target="_blank" rel="noreferrer">Open original source ↗</a></dd></div>}
      </dl>,
    },
    {
      title: "Save and track it",
      text: "In the brief: Save lead keeps it in My leads, Mark reviewed records that you have looked at it, and Track in my workspace follows it through your stages: Saved, Preparing, Applied, Shortlisted, then Accepted or Unsuccessful.",
      body: <div className="lt-tour-chips lt-tour-chips-buttons" aria-hidden="true"><span>Save lead</span><span>Mark reviewed</span><span>Track in my workspace</span></div>,
    },
    {
      title: "Where to push it to Wrike",
      text: "On the user dashboard there are two ways in, and both open the same Wrike panel: the Wrike task button in the lead's row of the table, or the Wrike task section in the lead's brief. Try the practice panel below. Nothing in this tour is sent to Wrike.",
      body: <WrikePractice title={title} assignees={assignees} />,
      needsAssignees: true,
    },
    {
      title: "Assignee: add one or leave it empty",
      text: "Assigning is optional. Choose Add to folder without assignee and the task goes into the team's Wrike folder unassigned; anyone can assign it later in Wrike. Or tick members first. Only people registered on this platform appear in the list, and a person can be ticked only when their platform email matches exactly one active Wrike account. To make someone assignable, an admin adds them under Team using the email they use in Wrike.",
      body: <AssigneeSummary assignees={assignees} />,
      needsAssignees: true,
    },
    {
      title: "Daily emails and reminders",
      text: "Each morning, new leads that match what the admin set for you (brands, verticals, regions, countries) arrive by email, and reminders follow before closing dates. The Add to Wrike button in an email opens the same Wrike panel here, which still asks before creating anything.",
    },
  ];
}

function LeadCard({ lead }: { lead: Opportunity | null }) {
  if (!lead) return <p className="lt-tour-muted">Loading a lead from the platform…</p>;
  const labels = [...split(lead.brands), ...split(lead.verticals)].slice(0, 4);
  return <article className="lt-tour-lead">
    <span className="lt-tour-tag">{lead.category}</span>
    <strong>{lead.title}</strong>
    <p>{[lead.organization, lead.source_website, lead.country].filter(Boolean).join(" · ")}</p>
    <div className="lt-tour-lead-foot">
      <span>{lead.deadline ? `Closes ${formatDate(lead.deadline)}` : "Ongoing"}</span>
      <span className="lt-tour-urgent">{daysLeftLabel(lead.deadline)}</span>
    </div>
    {labels.length > 0 && <div className="lt-tour-chips">{labels.map(l => <span key={l}>{l}</span>)}</div>}
  </article>;
}

/** A practice copy of WrikeTaskAction. It has no create call at all. */
function WrikePractice({ title, assignees }: { title: string; assignees: Assignees }) {
  const [showMembers, setShowMembers] = useState(false);
  const [picked, setPicked] = useState<number[]>([]);
  const [reviewing, setReviewing] = useState(false);
  const toggle = (id: number) => setPicked(p => p.includes(id) ? p.filter(v => v !== id) : [...p, id]);
  const names = assignees.items.filter(m => picked.includes(m.id)).map(m => m.name);

  return <section className="lt-tour-wrike" aria-label="Practice Wrike panel">
    <div className="lt-tour-wrike-head"><strong>Wrike task</strong><span className="lt-tour-demo">Practice · nothing is sent</span></div>
    {!reviewing ? <>
      <p className="lt-tour-muted">Destination: the team's Wrike folder (set by the admin)</p>
      {!picked.length && <p className="lt-tour-note">No assignee will be added. You can assign the task later in Wrike.</p>}
      <button type="button" className="lt-tour-btn lt-tour-btn-small" aria-expanded={showMembers}
              onClick={() => { setShowMembers(s => !s); if (showMembers) setPicked([]); }}>
        {showMembers ? "Continue without assigning" : "Assign members (optional)"}
      </button>
      {showMembers && <fieldset className="lt-tour-members">
        <legend>Choose members</legend>
        {assignees.state === "loading" && <p className="lt-tour-muted">Loading members registered on the platform…</p>}
        {assignees.state === "error" && <p className="lt-tour-muted">The member list could not be loaded here. The real panel shows it when you open Wrike task on a lead.</p>}
        {assignees.state === "ready" && !assignees.items.length && <p className="lt-tour-muted">No active team members are registered on the platform yet.</p>}
        {assignees.items.map(m => <label key={m.id} className={m.available ? "" : "is-off"}>
          <input type="checkbox" disabled={!m.available} checked={picked.includes(m.id)} onChange={() => toggle(m.id)} />
          <span>{m.name}<small>{m.available ? "Can be assigned" : "No matching Wrike account"}</small></span>
        </label>)}
      </fieldset>}
      <button type="button" className="lt-tour-btn lt-tour-btn-primary lt-tour-btn-block" onClick={() => setReviewing(true)}>
        {picked.length ? "Review assigned task" : "Add to folder without assignee"}
      </button>
    </> : <div className="lt-tour-confirm">
      <p><strong>Create this Wrike task?</strong></p>
      <p className="lt-tour-muted">Opportunity: {title}</p>
      <p className="lt-tour-muted">Assignees: {names.length ? names.join(", ") : "None — add to folder unassigned"}</p>
      <p className="lt-tour-note">In the real panel, choosing Yes creates one shared task for this lead. Here the button is switched off.</p>
      <div className="lt-tour-confirm-actions">
        <button type="button" className="lt-tour-btn" onClick={() => setReviewing(false)}>Go back</button>
        <button type="button" className="lt-tour-btn lt-tour-btn-primary" disabled title="Tour only: nothing is sent to Wrike">Yes, create task</button>
      </div>
    </div>}
  </section>;
}

function AssigneeSummary({ assignees }: { assignees: Assignees }) {
  if (assignees.state === "loading" || assignees.state === "idle") return <p className="lt-tour-muted">Counting members registered on the platform…</p>;
  if (assignees.state === "error") return <p className="lt-tour-muted">The member count could not be loaded here. You can still add tasks without an assignee.</p>;
  const ready = assignees.items.filter(m => m.available).length;
  return <div className="lt-tour-stats">
    <div><strong>{assignees.items.length}</strong><span>registered on the platform</span></div>
    <div><strong>{ready}</strong><span>can be assigned in Wrike now</span></div>
    <div><strong>{assignees.items.length - ready}</strong><span>need a matching Wrike email</span></div>
  </div>;
}
