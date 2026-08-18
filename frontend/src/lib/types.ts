export interface Opportunity {
  id: number;
  unique_id: string;
  title: string;
  organization: string;
  country: string;
  region: string;
  funding_type: string;
  vertical: string;
  /** Canonical comma-separated vertical tags, e.g. "Health, Climate/Sustainability". */
  verticals: string;
  /** Research | Implementation | "" — decides which team an RFP goes to. */
  work_type: string;
  /** Baseline | Endline | Data Collection | … when the call names one. */
  study_type: string;
  category: string;
  deadline: string | null;
  website: string;
  opportunity_url: string;
  /** Always-clickable destination. "direct" = the listing, "search" = a search
   *  on the source site that will find it. Computed by the backend. */
  link: string;
  link_kind: "direct" | "search";
  summary: string;
  location: string;
  eligibility: string;
  funding_amount: string;
  status: string;
  source_website: string;
  date_scraped: string;
  /** Human sign-off — the gate for everything downstream of the dashboard. */
  approved: boolean;
  approved_at: string | null;
  approved_by: string;
}

export interface Paginated {
  items: Opportunity[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface Facets {
  categories: string[];
  verticals: string[];
  countries: string[];
  regions: string[];
  sources: string[];
  organizations: string[];
}

export interface Stats {
  total_active: number;
  by_category: Record<string, number>;
  by_region: Record<string, number>;
  by_vertical: Record<string, number>;
  todays_new: number;
  upcoming_deadlines: Opportunity[];
  last_scraped: string | null;
}

export interface SourceProgress {
  display_name: string;
  pages: number;
  found: number;
  saved: number;
  skipped_expired: number;
  duplicates: number;
  /** Rejected as advertising/junk before reaching the database. */
  spam: number;
  /** Dropped because the run was restricted to certain verticals. */
  off_vertical: number;
  errors: number;
  status: string;
}

export interface Progress {
  state: "idle" | "running" | "paused" | "stopping";
  progress_percent: number;
  elapsed_seconds: number;
  eta_seconds: number | null;
  sources: Record<string, SourceProgress>;
  logs: string[];
}

export interface SourceInfo {
  name: string;
  display_name: string;
  website: string;
}

export interface FilterState {
  categories: string[];
  verticals: string[];
  countries: string[];
  regions: string[];
  sources: string[];
  search: string;
  deadline_before: string;
  deadline_after: string;
  /** Show the closed/expired archive instead of live opportunities. */
  archived: boolean;
  /** Only opportunities first scraped today — set by the "New Today" card. */
  new_today: boolean;
  /** Restrict to approved rows only. */
  approved: boolean;
  work_type: string;
  study_type: string;
  /** Hide listings whose title is in a non-Latin script. */
  english_only: boolean;
  /** Hide rows with no vertical. Default on. */
  has_vertical: boolean;
  page: number;
  page_size: number;
  sort_by: string;
  sort_dir: "asc" | "desc";
}

export const emptyFilters: FilterState = {
  categories: [],
  verticals: [],
  countries: [],
  regions: [],
  sources: [],
  search: "",
  deadline_before: "",
  deadline_after: "",
  archived: false,
  new_today: false,
  approved: false,
  work_type: "",
  study_type: "",
  english_only: true,
  has_vertical: true,
  page: 1,
  page_size: 25,
  sort_by: "deadline",
  sort_dir: "asc",
};

export interface TeamMember {
  id: number;
  name: string;
  email: string;
  keywords: string;
  categories: string;
  verticals: string;
  auto_send: boolean;
  active: boolean;
  created_at: string;
}

/** Canonical six-vertical system — these strings must match backend
 *  services/verticals.py EXACTLY. The backend drops any vertical it doesn't
 *  recognise, so a mismatch here silently turns a vertical-filtered scrape
 *  into an unfiltered one (previously "E4C" and "Climate/Sustainability" were
 *  short of the backend's "E4C(Evidence for Change)" and
 *  "Climate/Sustainability(ESG)" and were being discarded). */
export const VERTICALS = [
  "Livelihood",
  "Health",
  "E4C(Evidence for Change)",
  "Climate/Sustainability(ESG)",
  "Worker Wellbeing",
  "Innovative Finance",
] as const;

export const VERTICAL_DESCRIPTIONS: Record<string, string> = {
  Livelihood: "Agriculture and Rural Management",
  Health: "Health",
  "E4C(Evidence for Change)": "Research and Community Engagement",
  "Climate/Sustainability(ESG)": "Climate / Sustainability",
  "Worker Wellbeing": "Worker Wellbeing (WWB)",
  "Innovative Finance": "Innovative Finance",
};

export interface ScheduleStatus {
  mode: string;
  cron: string | null;
  hour: number;
  minute: number;
  next_run: string | null;
  last_run: string | null;
  last_status: string | null;
  last_success: string | null;
}

export interface SendResult {
  member: string;
  sent: number;
  detail?: string | null;
}

export interface EmailSettings {
  digest_enabled: boolean;
  digest_hour: number;
  digest_minute: number;
  /** Days-before-deadline at which reminders are sent, descending. */
  reminder_days: number[];
  /** Email new matches the moment a scrape finishes. */
  send_on_scrape: boolean;
  next_run: string | null;
}

export interface DigestRunResult {
  members_emailed: number;
  opportunities_sent: number;
  reminders_sent: number;
}
