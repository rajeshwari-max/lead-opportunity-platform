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
  const [showAssignees, setShowAssignees] = useState(false);
  const [loadingAssignees, setLoadingAssignees] = useState(false);
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

  async function revealAssignees() {
    if (showAssignees) {
      setShowAssignees(false);
      setSelected([]);
      return;
    }
    setShowAssignees(true);
    if (assignees.length || loadingAssignees || assigneeError) return;
    setLoadingAssignees(true);
    try {
      setAssignees(await api.wrikeAssignees());
    } catch {
      setAssigneeError("Assignees could not be loaded. You can still add the task to the folder without an assignee.");
    } finally {
      setLoadingAssignees(false);
    }
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
  if (status && !status.connected) return <section className="ud-wrike" aria-label="Wrike task">
    <h3>Wrike task</h3>
    <p className="ud-wrike-error">
      {status.configured
        ? "Wrike is configured but has not been connected on this server. An administrator must connect Wrike on this dashboard before tasks can be created."
        : "Wrike is not configured on this server. An administrator must add the production Wrike settings first."}
    </p>
  </section>;

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
        <p className="ud-sub">Destination: {folder.title}</p>
        <p className="ud-wrike-unassigned">No assignee will be added. You can assign the task later in Wrike.</p>
        <button className="ud-wrike-optional" type="button" aria-expanded={showAssignees} onClick={() => void revealAssignees()}>
          {showAssignees ? "Continue without assigning" : "Assign members (optional)"}
        </button>
        {showAssignees && <fieldset className="ud-wrike-assignees" disabled={creating || loadingAssignees}>
          <legend>Choose members</legend>
          {loadingAssignees && <p className="ud-sub">Loading members…</p>}
          {assignees.map(member => <label key={member.id}>
            <input type="checkbox" checked={selected.includes(member.id)} disabled={!member.available} onChange={() => toggleMember(member.id)} />
            <span>{member.name}<small>{member.available ? member.email : `${member.email} · No unique active Wrike match`}</small></span>
          </label>)}
          {assigneeError && <p className="ud-sub">{assigneeError}</p>}
          {!loadingAssignees && assignees.length === 0 && !assigneeError && <p className="ud-sub">No active platform team members are available.</p>}
        </fieldset>}
        <button className="ud-approve" type="button" onClick={() => setReviewing(true)}>{selected.length ? "Review assigned task" : "Add to folder without assignee"}</button>
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
