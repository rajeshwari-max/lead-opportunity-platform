import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, History, Loader2, Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import type { IntelligenceFeedbackInput, Opportunity, OpportunityIntelligence } from "@/lib/types";

const brands = ["CMS", "Swasti", "Vrutti", "Upfront", "Green Foundation", "Community Action Collab", "Setu"];
const archetypes = ["Devsol", "Social Business"];
const verticals = [
  "Livelihood", "Health", "E4C(Evidence for Change)", "Climate/Sustainability(ESG)",
  "Worker Wellbeing", "Innovative Finance",
];

const split = (value: string) => value.split(",").map(item => item.trim()).filter(Boolean);
const score = (value: number) => `${Math.round(value)}%`;
type FeedbackDraft = Required<IntelligenceFeedbackInput>;

function initialFeedback(opportunity: Opportunity): FeedbackDraft {
  const currentVerticals = split(opportunity.verticals || opportunity.vertical || "")
    .filter(value => verticals.includes(value));
  const currentArchetypes = split(opportunity.archetypes || "");
  if ((opportunity.verticals || "").includes("Social Business") && !currentArchetypes.includes("Social Business")) {
    currentArchetypes.push("Social Business");
  }
  return {
    decision: "accept",
    corrected_verticals: currentVerticals,
    corrected_brands: split(opportunity.brands || ""),
    corrected_archetypes: currentArchetypes,
    eligibility_override: "",
    reason: "",
    comment: "",
  };
}

function ChoiceList({ title, values, selected, onChange }: {
  title: string; values: string[]; selected: string[]; onChange: (values: string[]) => void;
}) {
  return <fieldset className="ud-intelligence-choices"><legend>{title}</legend>{values.map(value => <label key={value}>
    <input type="checkbox" checked={selected.includes(value)} onChange={event => onChange(
      event.target.checked ? [...selected, value] : selected.filter(item => item !== value),
    )} /> {value}
  </label>)}</fieldset>;
}

export function OpportunityIntelligenceSection({ opportunity, readOnly = false }: {
  opportunity: Opportunity; readOnly?: boolean;
}) {
  const [data, setData] = useState<OpportunityIntelligence | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [feedback, setFeedback] = useState<FeedbackDraft>(() => initialFeedback(opportunity));

  const refresh = async (id: number) => {
    const result = await api.opportunityIntelligence(id);
    setData(result);
    setError("");
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    setData(null);
    setError("");
    setNotice("");
    setFeedback(initialFeedback(opportunity));
    api.opportunityIntelligence(opportunity.id)
      .then(result => { if (active) setData(result); })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : "Intelligence could not be loaded"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [opportunity.id]);

  const submit = async () => {
    setSaving(true);
    setNotice("");
    setError("");
    try {
      const original = initialFeedback(opportunity);
      const normalized = (values: string[]) => [...values].sort().join("\u0000");
      const classificationChanged = normalized(feedback.corrected_verticals) !== normalized(original.corrected_verticals)
        || normalized(feedback.corrected_brands) !== normalized(original.corrected_brands)
        || normalized(feedback.corrected_archetypes) !== normalized(original.corrected_archetypes);
      if (feedback.decision === "correct" && !classificationChanged && !feedback.eligibility_override) {
        throw new Error("Choose a different label or an eligibility result before saving a correction.");
      }
      const body: IntelligenceFeedbackInput = {
        decision: feedback.decision, reason: feedback.reason, comment: feedback.comment,
      };
      if (feedback.decision === "correct" && classificationChanged) {
        // All three axes travel together so changing one never clears another.
        body.corrected_verticals = feedback.corrected_verticals;
        body.corrected_brands = feedback.corrected_brands;
        body.corrected_archetypes = feedback.corrected_archetypes;
      }
      if (feedback.decision === "correct" && feedback.eligibility_override) {
        body.eligibility_override = feedback.eligibility_override;
      }
      await api.submitIntelligenceFeedback(opportunity.id, body);
      await refresh(opportunity.id);
      setNotice(feedback.decision === "correct"
        ? "Correction saved. Human labels are protected from automatic replacement."
        : `Decision saved as ${feedback.decision}.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Feedback could not be saved");
    } finally {
      setSaving(false);
    }
  };

  return <section className="ud-intelligence" aria-label="Company intelligence">
    <div className="ud-intelligence-heading"><h3><Sparkles size={14} />Company intelligence</h3>
      {data && <span className={`ud-priority ud-priority-${data.priority.toLowerCase()}`}>{data.priority} priority</span>}
    </div>
    {loading && <p className="ud-sub"><Loader2 className="ud-spin" size={14} /> Calculating fit from company history…</p>}
    {error && <p role="alert" className="ud-intelligence-error">{error}</p>}
    {data && <>
      <div className="ud-recommendation-score">
        <strong>{score(data.recommendation_score)}</strong>
        <span>recommendation score<br />{data.confidence.toLowerCase()} confidence</span>
      </div>
      <div className="ud-intelligence-metrics">
        <div><span>Eligibility</span><b>{score(data.eligibility_score)}</b><small>{data.eligibility_level}{data.eligibility_override ? ` · human override (calculated ${data.computed_eligibility_level})` : ""}</small></div>
        <div><span>Company fit</span><b>{score(data.company_fit_score)}</b></div>
        <div><span>Historical similarity</span><b>{score(data.historical_similarity)}</b></div>
        <div><span>Past success pattern</span><b>{score(data.success_pattern_score)}</b></div>
      </div>

      <details className="ud-intelligence-details" open>
        <summary>Eligibility criteria</summary>
        <div className="ud-criteria">{data.eligibility_matches.map((item, index) => <article key={`${item.criterion}-${index}`}>
          <div><strong>{item.criterion}</strong><span className={`ud-criterion ud-criterion-${item.status.toLowerCase()}`}>{item.status.replace("_", " ")}</span></div>
          <p><b>Required:</b> {item.requirement}</p>
          <p><b>Company:</b> {item.company_information}</p>
          {item.evidence && <p className="ud-sub">Evidence: {item.evidence}</p>}
        </article>)}</div>
      </details>

      <details className="ud-intelligence-details">
        <summary>Classification and confidence</summary>
        <p className="ud-sub">
          {[data.classification.source || "source not recorded", data.classification.version,
            data.classification.status].filter(Boolean).join(" · ")}
        </p>
        {(["brands", "archetypes", "verticals"] as const).map(group => {
          const values = Object.entries(data.classification.scores[group])
            .sort((left, right) => right[1] - left[1]);
          return values.length ? <div className="ud-classification-scores" key={group}>
            <strong>{group === "verticals" ? "Devsol verticals" : group}</strong>
            <div>{values.map(([label, value]) => <span key={label}>{label} · {score(value * 100)}</span>)}</div>
          </div> : null;
        })}
      </details>

      <div className="ud-intelligence-reasons">
        <div><h4><CheckCircle2 size={13} />Why it may fit</h4><ul>{data.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul></div>
        <div><h4><AlertTriangle size={13} />Risks and gaps</h4>{data.risks.length
          ? <ul>{data.risks.map((risk, index) => <li key={index}>{risk}</li>)}</ul>
          : <p className="ud-sub">No specific eligibility risk was identified.</p>}</div>
      </div>

      <details className="ud-intelligence-details">
        <summary><History size={13} /> Similar historical leads ({data.similar_leads.length})</summary>
        {data.similar_leads.length ? <div className="ud-similar-leads">{data.similar_leads.map(lead => <article key={lead.id}>
          <strong>{lead.title}</strong><span>{score(lead.similarity)} similar</span>
          <p>{[lead.status, lead.won_lost, lead.outcome].filter(Boolean).join(" · ") || "Outcome not verified"}</p>
          {lead.reason && <p className="ud-sub">{lead.reason}</p>}
        </article>)}</div> : <p className="ud-sub">No sufficiently similar historical lead was found.</p>}
      </details>

      <form className="ud-intelligence-feedback" onSubmit={event => { event.preventDefault(); void submit(); }}>
        <h4>Help the system learn</h4>
        {data.human_decision && <p className="ud-sub">Latest review: <b>{data.human_decision}</b>{data.human_reason ? ` — ${data.human_reason}` : ""}</p>}
        <div className="ud-decision-buttons">{(["accept", "reject", "correct"] as const).map(decision => <button
          type="button" key={decision} aria-pressed={feedback.decision === decision}
          onClick={() => setFeedback({ ...feedback, decision })}>{decision}</button>)}</div>
        {feedback.decision === "correct" && <div className="ud-correction-fields">
          <ChoiceList title="Brands" values={brands} selected={feedback.corrected_brands}
            onChange={corrected_brands => setFeedback({ ...feedback, corrected_brands })} />
          <ChoiceList title="Archetypes" values={archetypes} selected={feedback.corrected_archetypes}
            onChange={corrected_archetypes => setFeedback({ ...feedback, corrected_archetypes })} />
          <ChoiceList title="Devsol verticals" values={verticals} selected={feedback.corrected_verticals}
            onChange={corrected_verticals => setFeedback({ ...feedback, corrected_verticals })} />
          <label>Correct eligibility
            <select value={feedback.eligibility_override} onChange={event => setFeedback({ ...feedback,
              eligibility_override: event.target.value as FeedbackDraft["eligibility_override"],
            })}><option value="">No change</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option><option>UNKNOWN</option></select>
          </label>
        </div>}
        <label>Reason <input maxLength={1000} placeholder="Why did you make this decision?" value={feedback.reason}
          onChange={event => setFeedback({ ...feedback, reason: event.target.value })} /></label>
        <label>Comment <textarea rows={2} maxLength={10000} placeholder="Optional detail for future reviewers" value={feedback.comment}
          onChange={event => setFeedback({ ...feedback, comment: event.target.value })} /></label>
        <button className="ud-intelligence-submit" disabled={readOnly || saving}>{saving ? "Saving…" : "Save review"}</button>
        {readOnly && <p className="ud-sub">Feedback is disabled on this read-only server.</p>}
        {notice && <p role="status" className="ud-intelligence-notice">{notice}</p>}
      </form>
    </>}
  </section>;
}
