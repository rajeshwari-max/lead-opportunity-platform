import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { WrikeAssignee, WrikeFolder, WrikeStatus, WrikeTaskLink } from "../lib/types";

type Props = { opportunityId: number; opportunityTitle: string; readOnly: boolean; onCreated?: () => void };

export function WrikeTaskAction({ opportunityId, opportunityTitle, readOnly, onCreated }: Props) {
  const [status, setStatus] = useState<WrikeStatus | null>(null);
  const [folder, setFolder] = useState<WrikeFolder | null>(null);
  const [assignees, setAssignees] = useState<WrikeAssignee[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [link, setLink] = useState<WrikeTaskLink | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [error, setError] = useState("");
  const [assigneeError, setAssigneeError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setAssigneeError("");
    async function load() {
      try {
        const wrike = await api.wrikeStatus();
        if (cancelled) return;
        setStatus(wrike);
        if (!wrike.connected) return;
        const saved = await api.wrikeTaskForOpportunity(opportunityId);
        if (cancelled) return;
        setLink(saved);
        if (wrike.enabled && saved.status === "not_created" && !readOnly) {
          const destination = await api.wrikeFolder();
          if (cancelled) return;
          setFolder(destination);
          try {
            const members = await api.wrikeAssignees();
            if (!cancelled) setAssignees(members);
          } catch {
            if (!cancelled) setAssigneeError("Assignees could not be loaded. You can still create the task without an assignee.");
          }
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Could not load Wrike details");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [opportunityId, readOnly]);

  function toggleMember(id: number) {
    setSelected(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]);
  }

  async function create() {
    if (!folder || !reviewing || !status?.enabled || creating) return;
    setCreating(true);
    setError("");
    try {
      setLink(await api.createWrikeTask(opportunityId, selected));
      onCreated?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create the Wrike task");
      // A timed-out POST may have succeeded. Read the saved reservation rather
      // than offering a second POST that could create a duplicate task.
      try { setLink(await api.wrikeTaskForOpportunity(opportunityId)); } catch { /* Keep the original error. */ }
    } finally {
      setCreating(false);
      setReviewing(false);
    }
  }

  if (loading) return <section className="ud-wrike" aria-label="Wrike task"><h3>Wrike task</h3><p className="ud-sub">Checking connection…</p></section>;
  if (!status?.connected && !error) return null;

  return <section className="ud-wrike" aria-label="Wrike task">
    <h3>Wrike task</h3>
    {error && <p role="alert" className="ud-wrike-error">{error}</p>}
    {link?.status === "created" && <>
      <p className="ud-sub">One shared Wrike task has been created for this opportunity.</p>
      {link.permalink && <a className="ud-primary-button" href={link.permalink} target="_blank" rel="noopener noreferrer">Open task in Wrike ↗</a>}
      {link.assignment_warning && <p role="alert" className="ud-wrike-error">Check the task in Wrike: one or more assignees may not have been added.</p>}
    </>}
    {link?.status === "creating" && <p role="status" className="ud-sub">Task creation is in progress. Do not create it again.</p>}
    {link?.status === "uncertain" && <p role="alert" className="ud-wrike-error">Wrike may have created this task. Ask an administrator to check the folder before trying again.</p>}
    {link?.status === "not_created" && !status?.enabled && <p className="ud-sub">Wrike is connected, but task creation is disabled by the server setting.</p>}
    {link?.status === "not_created" && status?.enabled && readOnly && <p className="ud-sub">Task creation is unavailable on this read-only dashboard.</p>}
    {link?.status === "not_created" && status?.enabled && !readOnly && folder && <>
      {!reviewing ? <>
        <p className="ud-sub">Destination: {folder.title}. Assignees are optional.</p>
        <fieldset className="ud-wrike-assignees" disabled={creating}>
          <legend>Assign to (optional)</legend>
          {assignees.map(member => <label key={member.id}>
            <input type="checkbox" checked={selected.includes(member.id)} disabled={!member.available} onChange={() => toggleMember(member.id)} />
            <span>{member.name}<small>{member.available ? member.email : `${member.email} · No unique active Wrike match`}</small></span>
          </label>)}
          {assigneeError && <p className="ud-sub">{assigneeError}</p>}
          {assignees.length === 0 && !assigneeError && <p className="ud-sub">No active platform team members are available.</p>}
        </fieldset>
        <button className="ud-approve" type="button" onClick={() => setReviewing(true)}>Review before creating</button>
      </> : <div className="ud-wrike-confirm" role="group" aria-label="Confirm Wrike task creation">
        <p><strong>Create this Wrike task?</strong></p>
        <p className="ud-sub">Opportunity: {opportunityTitle}</p>
        <p className="ud-sub">Folder: {folder.title}</p>
        <p className="ud-sub">Assignees: {selected.length ? assignees.filter(member => selected.includes(member.id)).map(member => member.name).join(", ") : "None — add to folder unassigned"}</p>
        <p className="ud-sub">Nothing will be sent to Wrike until you choose Yes.</p>
        <div className="ud-wrike-confirm-actions">
          <button className="ud-approve" type="button" disabled={creating} onClick={() => setReviewing(false)}>Go back</button>
          <button className="ud-primary-button" type="button" disabled={creating} onClick={() => void create()}>{creating ? "Creating in Wrike…" : "Yes, create task"}</button>
        </div>
      </div>}
    </>}
  </section>;
}
