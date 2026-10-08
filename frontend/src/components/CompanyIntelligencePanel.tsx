import { useCallback, useEffect, useState } from "react";
import { BrainCircuit, Building2, Loader2, RefreshCw, Save } from "lucide-react";
import { api } from "@/lib/api";
import type { CompanyIntelligenceProfile, IntelligenceLearningSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type ListField =
  | "countries_of_operation" | "industries" | "sectors" | "focus_areas"
  | "organization_types" | "capabilities" | "services" | "project_types"
  | "target_beneficiaries" | "geographic_focus" | "certifications"
  | "partnership_types" | "funding_types_of_interest";

const listFields: { key: ListField; label: string; help: string }[] = [
  { key: "countries_of_operation", label: "Countries of operation", help: "Where the company is legally or operationally present" },
  { key: "geographic_focus", label: "Geographic focus", help: "Priority countries, regions and communities" },
  { key: "industries", label: "Industries", help: "Industries in which the company has experience" },
  { key: "sectors", label: "Sectors", help: "Programme sectors and technical domains" },
  { key: "focus_areas", label: "Focus areas", help: "Themes the company actively wants to pursue" },
  { key: "organization_types", label: "Organisation types", help: "For example NGO, social enterprise or research institution" },
  { key: "capabilities", label: "Capabilities", help: "What the company can demonstrably deliver" },
  { key: "services", label: "Services", help: "Specific services offered to funders and partners" },
  { key: "project_types", label: "Project types", help: "Research, implementation, evaluation and similar work" },
  { key: "target_beneficiaries", label: "Target beneficiaries", help: "Populations and groups the company works with" },
  { key: "certifications", label: "Certifications", help: "Registration, accreditation and compliance credentials" },
  { key: "partnership_types", label: "Partnership types", help: "Prime, consortium, sub-grantee and similar roles" },
  { key: "funding_types_of_interest", label: "Funding types of interest", help: "Grant, RFP, tender and other preferred instruments" },
];

const weightLabels: Record<string, string> = {
  eligibility_fit: "Eligibility",
  company_strategic_fit: "Strategic fit",
  historical_similarity: "Historical similarity",
  past_success_pattern: "Past success",
  geographic_fit: "Geography",
  opportunity_quality: "Data quality",
};

const parseList = (value: string) => value
  .split(/[\n,;]/)
  .map(item => item.trim())
  .filter((item, index, values) => !!item && values.findIndex(v => v.toLowerCase() === item.toLowerCase()) === index);

export function CompanyIntelligencePanel({ readOnly = false }: { readOnly?: boolean }) {
  const [profile, setProfile] = useState<CompanyIntelligenceProfile | null>(null);
  const [learning, setLearning] = useState<IntelligenceLearningSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState(500);
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    try {
      const [nextProfile, nextLearning] = await Promise.all([
        api.intelligenceProfile(), api.intelligenceLearning(),
      ]);
      setProfile(nextProfile);
      setLearning(nextLearning);
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Company intelligence could not be loaded");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    if (!profile) return;
    setBusy(true);
    try {
      const { id, version, updated_by, updated_at, ...body } = profile;
      void id; void version; void updated_by; void updated_at;
      setProfile(await api.updateIntelligenceProfile(body));
      setMessage("Company profile saved. Recalculate to apply it to current opportunities.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Profile could not be saved");
    } finally {
      setBusy(false);
    }
  };

  const recalculate = async () => {
    setBusy(true);
    try {
      const result = await api.recalculateIntelligence(limit);
      setMessage(`Recalculated ${result.processed.toLocaleString()} opportunities against ${result.historical_leads.toLocaleString()} historical leads.`);
      setLearning(await api.intelligenceLearning());
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Recalculation failed");
    } finally {
      setBusy(false);
    }
  };

  if (!profile && !message) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Building2 className="h-4 w-4" /> Company intelligence
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-xs">
        {message && <p role="status" className="rounded-md bg-muted p-2 leading-relaxed">{message}</p>}
        {profile && <>
          <p className="text-muted-foreground">
            These facts drive eligibility and company-fit recommendations. Enter one item per line.
          </p>
          <label className="block font-medium">Company name
            <input className="mt-1 w-full rounded-md border bg-background px-2 py-1.5" value={profile.company_name}
              disabled={readOnly || busy} onChange={event => setProfile({ ...profile, company_name: event.target.value })} />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="font-medium">Company size
              <input className="mt-1 w-full rounded-md border bg-background px-2 py-1.5" value={profile.company_size}
                disabled={readOnly || busy} onChange={event => setProfile({ ...profile, company_size: event.target.value })} />
            </label>
            <label className="font-medium">Years operating
              <input type="number" min={0} max={500} className="mt-1 w-full rounded-md border bg-background px-2 py-1.5"
                value={profile.years_of_operation ?? ""} disabled={readOnly || busy}
                onChange={event => setProfile({ ...profile, years_of_operation: event.target.value ? Number(event.target.value) : null })} />
            </label>
          </div>
          <details className="rounded-lg border p-3" open>
            <summary className="cursor-pointer font-semibold">Profile facts</summary>
            <div className="mt-3 space-y-3">
              {listFields.map(field => <label className="block font-medium" key={field.key}>{field.label}
                <span className="block font-normal text-muted-foreground">{field.help}</span>
                <textarea rows={3} className="mt-1 w-full resize-y rounded-md border bg-background px-2 py-1.5"
                  value={profile[field.key].join("\n")} disabled={readOnly || busy}
                  onChange={event => setProfile({ ...profile, [field.key]: parseList(event.target.value) })} />
              </label>)}
            </div>
          </details>
          <details className="rounded-lg border p-3">
            <summary className="cursor-pointer font-semibold">Recommendation settings</summary>
            <p className="mt-2 text-muted-foreground">Weights are relative; they do not need to total 100.</p>
            <div className="mt-2 grid grid-cols-2 gap-2">
              {Object.entries(profile.recommendation_weights).map(([key, value]) => <label key={key} className="font-medium">
                {weightLabels[key] ?? key}
                <input type="number" min={0} step="1" className="mt-1 w-full rounded-md border bg-background px-2 py-1.5"
                  value={value} disabled={readOnly || busy}
                  onChange={event => setProfile({ ...profile, recommendation_weights: {
                    ...profile.recommendation_weights, [key]: Number(event.target.value),
                  } })} />
              </label>)}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              {(["medium", "high"] as const).map(key => <label key={key} className="font-medium capitalize">{key} priority from
                <input type="number" min={0} max={100} step="1" className="mt-1 w-full rounded-md border bg-background px-2 py-1.5"
                  value={profile.recommendation_thresholds[key] ?? 0} disabled={readOnly || busy}
                  onChange={event => setProfile({ ...profile, recommendation_thresholds: {
                    ...profile.recommendation_thresholds, [key]: Number(event.target.value),
                  } })} />
              </label>)}
            </div>
          </details>
          <Button className="w-full" size="sm" disabled={readOnly || busy} onClick={() => void save()}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />} Save profile
          </Button>
          <p className="text-muted-foreground">
            Profile version {profile.version}{profile.updated_by ? ` · last changed by ${profile.updated_by}` : ""}
          </p>
        </>}

        {learning && <details className="rounded-lg border p-3" open>
          <summary className="cursor-pointer font-semibold">Learning summary</summary>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.historical.total.toLocaleString()}</b>historical leads</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.historical.win_rate}%</b>verified win rate</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.historical.positive_unverified.toLocaleString()}</b>positive, outcome mixed</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.historical.rejected.toLocaleString()}</b>not pursued / rejected</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.feedback.total.toLocaleString()}</b>review decisions</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.feedback.corrected.toLocaleString()}</b>corrections</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.live_actions.applied.toLocaleString()}</b>live applications</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.live_actions.shortlisted.toLocaleString()}</b>live shortlisted</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.live_actions.won.toLocaleString()}</b>live wins</div>
            <div className="rounded-md bg-muted p-2"><b className="block text-lg">{learning.live_actions.lost.toLocaleString()}</b>live losses</div>
          </div>
          {!!learning.successful_verticals.length && <p className="mt-3 leading-relaxed text-muted-foreground">
            Successful verticals: {learning.successful_verticals.slice(0, 5).map(([name, value]) => `${name} (${value})`).join(", ")}
          </p>}
          {!!learning.top_rejection_reasons.length && <p className="mt-2 leading-relaxed text-muted-foreground">
            Common rejection reasons: {learning.top_rejection_reasons.slice(0, 5).map(([name, value]) => `${name} (${value})`).join(", ")}
          </p>}
        </details>}

        <div className="rounded-lg border p-3">
          <p className="flex items-center gap-2 font-semibold"><BrainCircuit className="h-4 w-4" />Apply current knowledge</p>
          <p className="mt-1 text-muted-foreground">Recalculate the next active opportunities after changing the profile or history.</p>
          <label className="mt-2 block font-medium">Maximum opportunities
            <input type="number" min={1} max={5000} value={limit} disabled={readOnly || busy}
              className="mt-1 w-full rounded-md border bg-background px-2 py-1.5"
              onChange={event => setLimit(Math.min(5000, Math.max(1, Number(event.target.value) || 1)))} />
          </label>
          <Button className="mt-2 w-full" size="sm" variant="outline" disabled={readOnly || busy} onClick={() => void recalculate()}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Recalculate
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
