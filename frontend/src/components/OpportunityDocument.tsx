import type { Opportunity } from "@/lib/types";

export function OpportunityDocument({ opportunity }: { opportunity: Opportunity }) {
  const documents = (opportunity.source_documents || []).filter(document =>
    /^https?:\/\//i.test(document.url || ""));
  const fallback = /^(https?:\/\/|\/)/i.test(opportunity.brief_pdf_url || "")
    ? opportunity.brief_pdf_url : "";
  return <div className="my-2 text-xs">
    {documents.length > 0 ? <>
      <strong>Official source documents</strong>
      <ul className="mt-1 space-y-1">
        {documents.map((document, index) => <li key={`${document.url}-${index}`}>
          <a href={document.url} target="_blank" rel="noreferrer"
            className="font-medium text-primary underline">
            {document.label || `Source document ${index + 1}`} ↗
          </a>
        </li>)}
      </ul>
    </> : <>
      <span className="text-muted-foreground">No official ToR or source file was found. </span>
      {fallback && <a href={fallback} target="_blank" rel="noreferrer"
        className="font-medium text-primary underline">Open summary PDF ↗</a>}
    </>}
  </div>;
}
