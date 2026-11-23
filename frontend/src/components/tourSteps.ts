/**
 * Script for the animated guided tour (ProductTour.tsx), modelled on the CSR
 * dashboard's tour. Each step spotlights one thing on the real user dashboard,
 * can prepare the page first (`before`) and can "click" something with the
 * animated pointer (`click`, or `action` to replace the real click).
 *
 * The tour walks through ONE real lead: the one open in the brief (the
 * highlighted row). It opens that lead's real Wrike panel to show where to
 * push it, but it NEVER clicks "Yes, create task", so nothing is sent to
 * Wrike. It also never clicks Save lead, Mark reviewed or anything else that
 * writes.
 *
 * Targets use the dashboard's own class names. A step whose target is missing
 * (for example Wrike not connected on this server) still shows, centred, with
 * its `fallback` text, so the tour never gets stuck.
 */

export type ChapterId = "intro" | "find" | "lead" | "wrike" | "finish";

export interface TourStep {
  id: string;
  chapter: ChapterId;
  /** What to spotlight. No target = centred card with a dimmed page. */
  target?: string | (() => Element | null);
  title: string;
  body: string | (() => string);
  /** Shown instead of `body` when the target is not on screen. */
  fallback?: string;
  /** Small "watch this" line under the text, for live demos. */
  watch?: string;
  placement?: "auto" | "bottom" | "top" | "right" | "left";
  block?: ScrollLogicalPosition;
  /** Runs before the element is looked up (open or close something). */
  before?: () => void | Promise<void>;
  /** Element the pointer moves to and clicks. */
  click?: string | (() => Element | null);
  /** Replaces the real click. */
  action?: () => void;
  /** Wait after the click before measuring again (ms). */
  settle?: number;
  /** Spotlight this instead of `target` once the click has happened. */
  afterClick?: string | (() => Element | null);
}

export const TOUR_CHAPTERS: Record<ChapterId, { label: string }> = {
  intro: { label: "Getting started" },
  find: { label: "Finding leads" },
  lead: { label: "One lead, start to finish" },
  wrike: { label: "Push to Wrike" },
  finish: { label: "All done" },
};

/* ------------------------------------------------------------------ */
/* Finders and helpers                                                 */
/* ------------------------------------------------------------------ */

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

/** The lead in the brief: the highlighted row, else the first row. */
const leadRow = () =>
  document.querySelector(".user-dashboard tr.ud-selected") ??
  document.querySelector(".user-dashboard .ud-table-scroll tbody tr");
const leadTitle = () => leadRow()?.querySelector(".ud-title")?.textContent?.trim() || "";
const quoted = () => (leadTitle() ? `“${leadTitle().slice(0, 90)}${leadTitle().length > 90 ? "…" : ""}”` : "this lead");

const dialog = () => document.querySelector(".user-dashboard .ud-wrike-dialog");
const inDialog = (selector: string) => () => dialog()?.querySelector(selector) ?? null;

/** A button by its visible text, inside the Wrike dialog. */
const dialogButton = (text: string) => () =>
  Array.from(dialog()?.querySelectorAll<HTMLButtonElement>("button") ?? [])
    .find(b => b.textContent?.trim().toLowerCase().startsWith(text.toLowerCase())) ?? null;

/** Close the Wrike dialog if the tour (or anyone) left it open. */
export function closeWrikeDialog() {
  (document.querySelector(".user-dashboard .ud-wrike-dialog-head button") as HTMLButtonElement | null)?.click();
}

/** Leave the confirm step ("Create this Wrike task?") without creating anything. */
async function backFromConfirm() {
  const back = dialog()?.querySelector<HTMLButtonElement>(".ud-wrike-confirm .ud-approve");
  if (back) { back.click(); await sleep(150); }
}

/** Make the member list open (true) or closed (false). Closing clears any ticks. */
function setMembersOpen(open: boolean) {
  const toggle = dialog()?.querySelector<HTMLButtonElement>(".ud-wrike-optional");
  if (toggle && (toggle.getAttribute("aria-expanded") === "true") !== open) toggle.click();
}

/** Open the Wrike dialog for the lead, if it is not already open. */
async function openWrikeDialog() {
  if (dialog()) return;
  const button = leadRow()?.querySelector<HTMLButtonElement>(".ud-row-wrike");
  if (button && !button.disabled) button.click();
  await sleep(250);
}

/* ------------------------------------------------------------------ */
/* Script                                                              */
/* ------------------------------------------------------------------ */

export const TOUR_STEPS: TourStep[] = [
  /* ---------- Getting started ---------- */
  {
    id: "welcome",
    chapter: "intro",
    title: "Let's take a quick tour",
    body: "This platform collects open grants, RFPs and tenders from funders and portals every night. The blue pointer will show you where to click and will open things for you. Nothing is saved, sent or pushed to Wrike during the tour.",
    watch: "Press → or Next to move on. Press Esc to stop at any time.",
    before: closeWrikeDialog,
  },
  {
    id: "stats",
    chapter: "intro",
    target: ".user-dashboard .ud-stats",
    placement: "bottom",
    block: "start",
    title: "The numbers at a glance",
    body: "How many leads are open right now, split into Grants, RFPs and Tenders, plus what was found today. Click a card to show only those leads; click it again to show all.",
    before: closeWrikeDialog,
  },

  /* ---------- Finding leads ---------- */
  {
    id: "search",
    chapter: "find",
    target: ".user-dashboard .ud-search",
    placement: "bottom",
    title: "Search",
    body: "Type a keyword, a funder or an organisation, for example “nutrition” or “UNICEF”. The list, the cards and the charts all follow your search.",
    before: closeWrikeDialog,
  },
  {
    id: "filters",
    chapter: "find",
    target: ".user-dashboard .ud-filter-panel",
    placement: "right",
    block: "start",
    title: "Filters",
    body: "Narrow the list by brand (Swasti, Vrutti, Green Foundation…), CMS vertical, region, country, source website, category and closing date. Your filters are remembered the next time you sign in.",
    before: closeWrikeDialog,
  },
  {
    id: "table",
    chapter: "find",
    target: ".user-dashboard .ud-table-panel",
    placement: "left",
    block: "start",
    title: "Every open lead",
    body: "Each row is one opportunity whose closing date has not passed: title, funder, source, country, deadline and amount. Sort by deadline or newest; tick Archive to see closed ones.",
    before: closeWrikeDialog,
  },

  /* ---------- One lead ---------- */
  {
    id: "open-lead",
    chapter: "lead",
    target: leadRow,
    placement: "bottom",
    title: "Open a lead",
    body: () => `Click a lead's title or View to open its brief. Let's follow one real lead: ${quoted()}.`,
    fallback: "Click a lead's title or View to open its brief. There are no leads in your current view; clear the filters and start the tour again.",
    watch: "Watch: the pointer opens this lead.",
    before: closeWrikeDialog,
    click: () => leadRow()?.querySelector(".ud-title") ?? null,
    // The brief already shows the highlighted row; a real click would only
    // record a "viewed" event, so the pointer clicks without one.
    action: () => {},
  },
  {
    id: "brief",
    chapter: "lead",
    target: ".user-dashboard .ud-brief",
    placement: "left",
    block: "start",
    title: "Read the brief",
    body: "Closing date, amount, eligibility, brands and verticals, and Platform intelligence: how well the lead fits us and who could work on it. “Open original source” at the bottom takes you to the funder's own page.",
    before: closeWrikeDialog,
  },
  {
    id: "save",
    chapter: "lead",
    target: ".user-dashboard .ud-brief .dl-save-button",
    placement: "left",
    title: "Save it to My leads",
    body: "Save lead keeps it in My leads, where you can add follow-up dates. Mark reviewed records that you have looked at it.",
    before: closeWrikeDialog,
  },
  {
    id: "workspace",
    chapter: "lead",
    target: () => Array.from(document.querySelectorAll<HTMLElement>(".user-dashboard .ud-brief a.ud-approve"))
      .find(a => /workspace/i.test(a.textContent || "")) ?? null,
    placement: "left",
    title: "Track it in your workspace",
    body: "Follow the lead through your stages: Saved, Preparing, Applied, Shortlisted, then Accepted or Unsuccessful.",
    fallback: "My workspace is not switched on for this dashboard, so this step is skipped.",
    before: closeWrikeDialog,
  },
  {
    id: "my-leads",
    chapter: "lead",
    target: ".user-dashboard .lt-nav",
    placement: "bottom",
    title: "My leads and Directories",
    body: "Your saved leads and follow-ups are up here, next to Directories of funders and partners.",
    before: closeWrikeDialog,
  },

  /* ---------- Push to Wrike ---------- */
  {
    id: "wrike-brief",
    chapter: "wrike",
    target: ".user-dashboard .ud-brief .ud-wrike",
    placement: "left",
    title: "Where to push it to Wrike: way 1",
    body: "In the lead's brief, scroll to the Wrike task section.",
    fallback: "In the lead's brief, scroll to the Wrike task section.",
    before: closeWrikeDialog,
  },
  {
    id: "wrike-row",
    chapter: "wrike",
    target: () => leadRow()?.querySelector(".ud-row-wrike") ?? null,
    placement: "bottom",
    title: "Way 2: the Wrike task button",
    body: () => `Or use the Wrike task button in the lead's row. Both open the same Wrike panel. The pointer opens it for ${quoted()}.`,
    fallback: "Or use the Wrike task button in the lead's row. Both open the same Wrike panel.",
    watch: "Watch: the pointer opens the Wrike panel.",
    before: async () => { closeWrikeDialog(); await sleep(150); },
    click: () => leadRow()?.querySelector(".ud-row-wrike") ?? null,
    action: () => { void openWrikeDialog(); },
    settle: 900,
    afterClick: dialog,
  },
  {
    id: "wrike-panel",
    chapter: "wrike",
    target: dialog,
    placement: "right",
    title: "The Wrike panel",
    body: "It shows the Wrike folder the task will go into. By default no assignee is added.",
    fallback: "The Wrike panel could not open here: Wrike is not connected on this server, or this dashboard is read-only. Ask an admin to connect Wrike.",
    before: async () => { await openWrikeDialog(); await backFromConfirm(); setMembersOpen(false); },
  },
  {
    id: "wrike-assign",
    chapter: "wrike",
    target: inDialog(".ud-wrike-assignees"),
    placement: "right",
    title: "Option 1: add an assignee",
    body: "Click “Assign members (optional)” and tick people. Only people registered on this platform are listed. Anyone greyed out has no Wrike account with the same email, so they cannot be assigned yet. An admin fixes that under Team, using the email they use in Wrike.",
    fallback: "With Wrike connected, “Assign members (optional)” lists the people registered on this platform. Only those whose email matches a Wrike account can be ticked.",
    watch: "Watch: the pointer opens the member list. Nothing is ticked.",
    before: async () => { await openWrikeDialog(); await backFromConfirm(); },
    click: inDialog(".ud-wrike-optional"),
    action: () => setMembersOpen(true),
    settle: 1400,
  },
  {
    id: "wrike-unassigned",
    chapter: "wrike",
    target: dialogButton("Add to folder without assignee"),
    placement: "right",
    title: "Option 2: no assignee",
    body: "Assigning is optional. Choose “Add to folder without assignee” and the task goes into the team's Wrike folder unassigned; anyone can assign it later in Wrike.",
    fallback: "Assigning is optional: you can add the task to the Wrike folder without an assignee and assign it later in Wrike.",
    before: async () => { await openWrikeDialog(); await backFromConfirm(); setMembersOpen(false); },
  },
  {
    id: "wrike-review",
    chapter: "wrike",
    target: inDialog(".ud-wrike-confirm"),
    placement: "right",
    title: "Check before it goes",
    body: "A last check: the lead, the folder and the assignees. Nothing is sent to Wrike until you choose Yes.",
    fallback: "Before anything is sent, the panel asks you to confirm the lead, the folder and the assignees.",
    watch: "Watch: the pointer opens the check. Nothing is sent.",
    before: async () => { await openWrikeDialog(); await backFromConfirm(); setMembersOpen(false); },
    click: dialogButton("Add to folder without assignee"),
    settle: 500,
  },
  {
    id: "wrike-yes",
    chapter: "wrike",
    target: () => dialog()?.querySelector(".ud-wrike-confirm .ud-primary-button") ?? null,
    placement: "right",
    title: "This is where you push it",
    body: "“Yes, create task” creates one shared Wrike task for this lead. The tour stops here and does not click it, so nothing has been sent.",
    fallback: "“Yes, create task” is the button that creates the Wrike task. The tour never clicks it.",
  },

  /* ---------- Finish ---------- */
  {
    id: "finish",
    chapter: "finish",
    title: "You're ready",
    body: "Each morning, new leads that match what the admin set for you (brands, verticals, regions, countries) arrive by email, with reminders before closing dates. The Add to Wrike button in an email opens this same panel. Take the tour again any time from the header.",
    before: async () => { await backFromConfirm(); closeWrikeDialog(); },
  },
];
