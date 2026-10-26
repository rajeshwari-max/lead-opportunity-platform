import { useEffect, useState } from "react";
import { leadRequest } from "./DirectoryPanel";

export function WrikeWorkflow({ opportunityId }: { opportunityId: number }) {
  const [state, setState] = useState<{ status_name: string; status_group: string; workflow_name: string; status_id: string; synced_at: string; resolved: boolean; permalink: string | null } | null>(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let cancelled = false; setBusy(true); setError(""); setState(null);
    leadRequest(`/wrike/opportunities/${opportunityId}/workflow`).then(data => { if (!cancelled) { setState(data); window.dispatchEvent(new Event("saved-leads-changed")); } })
      .catch(e => { if (!cancelled) setError(e.message); }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [opportunityId, revision]);
  return <section className="my-3"><h4>Wrike stage and status</h4>{error && <p role="alert">{error}</p>}
    {state && <><p>{state.workflow_name ? `${state.workflow_name} · ` : ""}{state.status_name || state.status_group}</p>
      {!state.resolved && <p>The custom status name is unavailable from the account workflow list. Check this task’s space workflow in Wrike. Status ID: {state.status_id || "Unavailable"}.</p>}
      <small>Checked: {new Date(state.synced_at).toLocaleString()}. Change workflow status in Wrike.</small>
      {state.permalink && <p><a href={state.permalink} target="_blank" rel="noopener noreferrer">Open linked task in Wrike</a></p>}</>}
    <button className="ud-control" type="button" disabled={busy} onClick={() => setRevision(r => r + 1)}>{busy ? "Checking Wrike…" : "Refresh Wrike status"}</button>
  </section>;
}
