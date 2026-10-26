import { useEffect, useRef, useState } from "react";
import { DashboardLeads } from "./DashboardLeads";
import { DirectoryPanel, leadRequest } from "./DirectoryPanel";
import { LeadFollowUps, ManualLeadForm } from "./LeadIntake";
import "./lead-tools.css";

type Tool = "my-leads" | "directories" | "add-lead";
const titles: Record<Tool, string> = {
  "my-leads": "My leads", directories: "Directories", "add-lead": "Add a lead",
};
const descriptions: Record<Tool, string> = {
  "my-leads": "Saved opportunities, follow-ups and application progress.",
  directories: "Review keywords, websites and organisations.",
  "add-lead": "Save an opportunity you found and plan your next action.",
};

export function AddLeadButton({ readOnly = false }: { readOnly?: boolean }) {
  return <button type="button" className="ud-control lt-add" disabled={readOnly}
    onClick={() => window.dispatchEvent(new CustomEvent("open-lead-tools", { detail: "add-lead" }))}>
    + Add lead
  </button>;
}

export function LeadTools({ isAdmin, name, readOnly }: { isAdmin: boolean; name: string; readOnly: boolean }) {
  const [active, setActive] = useState<Tool | null>(null);
  const [due, setDue] = useState<number | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => { void leadRequest("/my-leads/due").then(rows => {
      if (!cancelled) setDue(rows.length);
    }).catch(() => { if (!cancelled) setDue(null); }); };
    const open = (event: Event) => {
      const tool = (event as CustomEvent).detail;
      if (tool === "my-leads" || tool === "directories" || tool === "add-lead") setActive(tool);
    };
    const fromHash = () => {
      if (location.hash === "#my-leads") setActive("my-leads");
      else if (location.hash === "#directories") setActive("directories");
    };
    load(); fromHash();
    const timer = window.setInterval(load, 60000);
    window.addEventListener("saved-leads-changed", load);
    window.addEventListener("open-lead-tools", open);
    window.addEventListener("hashchange", fromHash);
    return () => {
      cancelled = true; window.clearInterval(timer);
      window.removeEventListener("saved-leads-changed", load);
      window.removeEventListener("open-lead-tools", open);
      window.removeEventListener("hashchange", fromHash);
    };
  }, []);

  useEffect(() => {
    const element = dialog.current;
    if (active && element && !element.open) element.showModal();
    else if (!active && element?.open) element.close();
  }, [active]);

  return <>
    <nav className="lt-nav" aria-label="Lead tools">
      <button type="button" className="ud-control" onClick={() => setActive("my-leads")}>
        My leads{due !== null && due > 0 && <span className="lt-due" aria-label={`${due} follow-ups due`}>{due} due</span>}
      </button>
      <button type="button" className="ud-control" onClick={() => setActive("directories")}>Directories</button>
    </nav>
    <dialog ref={dialog} className="lt-dialog" aria-labelledby="lead-tools-title"
      aria-describedby="lead-tools-description" onClose={() => setActive(null)}
      onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}>
      <div className="lt-shell">
        <header className="lt-heading"><div><h2 id="lead-tools-title">{active ? titles[active] : "My leads"}</h2>
          <p id="lead-tools-description">{active ? descriptions[active] : "Saved opportunities and follow-ups."}</p></div>
          <button type="button" className="ud-control" onClick={() => dialog.current?.close()}>Close</button>
        </header>
        <div className="lt-body">
          {/* Keep the saved-lead store mounted for Save buttons in the catalogue. */}
          <div hidden={active !== "my-leads"}>
            {active === "my-leads" && <LeadFollowUps />}
            <DashboardLeads isAdmin={isAdmin} name={name} readOnly={readOnly} initiallyOpen />
          </div>
          {active === "directories" && <DirectoryPanel isAdmin={isAdmin} readOnly={readOnly} initiallyOpen />}
          {active === "add-lead" && <ManualLeadForm readOnly={readOnly} initiallyOpen />}
        </div>
      </div>
    </dialog>
  </>;
}
