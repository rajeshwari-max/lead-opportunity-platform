import type { Opportunity } from "@/lib/types";

export function OpportunityDocument({ opportunity }: { opportunity: Opportunity }) {
  const available = opportunity.pdf_status === "found" &&
    /^https?:\/\//i.test(opportunity.pdf_url || "");
  return <div className="my-2 text-xs">
    {available ? <a href={opportunity.pdf_url!} target="_blank" rel="noreferrer"
      className="font-medium text-primary underline">Open source PDF ↗</a> :
      <span className="text-muted-foreground">Official document unavailable</span>}
  </div>;
}
