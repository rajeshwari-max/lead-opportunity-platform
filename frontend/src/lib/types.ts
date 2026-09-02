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
  /** Always-clickable destination, computed by the backend.
   *  "direct"  = the opportunity's own page
   *  "listing" = the funder's index/section page — the call is on it somewhere,
   *              but the reader still has to find the row
   *  "search"  = a search on the source site that will find it
   *  "none"    = nothing to open; the source published no link for this row.
   *              Such rows are no longer stored (backend
   *              LOP_REQUIRE_USABLE_LINK) and existing ones are removed by
   *              scripts/clean_dashboard.py. */
  link: string;
  link_kind: "direct" | "listing" | "search" | "none";
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
  /** Empty = everywhere, like every other routing field. */
  countries?: string;
  regions?: string;
  geo_include_unknown?: boolean;
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
  "Social Business",
] as const;

export const VERTICAL_DESCRIPTIONS: Record<string, string> = {
  Livelihood: "Agriculture and Rural Management",
  Health: "Health",
  "E4C(Evidence for Change)": "Research and Community Engagement",
  "Climate/Sustainability(ESG)": "Climate / Sustainability",
  "Worker Wellbeing": "Worker Wellbeing (WWB)",
  "Innovative Finance": "Innovative Finance",
  "Social Business": "Social Business and Market Systems",
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

/** One row awaiting a human decision about its closing date, plus the evidence
 *  needed to make it — `deadline_raw` is the source's own words. */
export interface ReviewQueueItem {
  id: number;
  title: string;
  organization: string;
  source_website: string;
  opportunity_url: string;
  deadline_raw: string;
  deadline_confidence: string;
  date_scraped: string | null;
  last_seen: string | null;
  summary: string;
}

export interface ReviewQueueResponse {
  total: number;
  /** Where the backlog sits. A backlog concentrated in one source is a parser
   *  bug for that source, not a review job. */
  by_source: { source_website: string; count: number }[];
  items: ReviewQueueItem[];
}

/** One source's health, read from the evidence its runs recorded. */
export interface SourceHealth {
  source_key: string;
  display_name: string;
  listing_url: string;
  /** generic = one of the 71 config-driven sources; bespoke = its own parser. */
  implementation: "generic" | "bespoke";
  requires_login: boolean;
  fetch_mode: "http" | "browser";
  pagination: string;
  expected_types: string;
  /** False means nobody has stated what this source should yield. */
  scope_confirmed: boolean;
  last_run_at: string | null;
  last_outcome: string;
  last_error_code: string;
  last_error_message: string;
  last_http_status: number | null;
  runs_30d: number;
  unhealthy_streak: number;
  total_rows: number;
  last_saved_at: string | null;
  /** Measured from the last row SAVED, not the last run attempted. */
  days_since_saved: number | null;
  state: "ok" | "never_produced" | "stale" | "failing" | "unknown";
  note: string;
}

export interface ScraperHealth {
  summary: { total: number; by_state: Record<string, number>; needs_attention: number };
  alerting: string[];
  sources: SourceHealth[];
  thresholds: { failure_streak: number; stale_days: number };
}

/** What the model would have said, and on what evidence. Shown to the reviewer
 *  because a bare confidence number gives them nothing to agree with. */
export interface VerticalSuggestion {
  vertical: string;
  score: number;
  evidence: string[];
}

export interface UnclassifiedItem {
  id: number;
  title: string;
  organization: string;
  source_website: string;
  opportunity_url: string;
  summary: string;
  country: string;
  category: string;
  deadline: string | null;
  date_scraped: string | null;
  classification_status: "classified" | "uncertain" | "unclassified";
  suggestions: VerticalSuggestion[];
}

export interface UnclassifiedQuery {
  search?: string;
  sources?: string[];
  countries?: string[];
  organizations?: string[];
  categories?: string[];
  deadline_before?: string;
  deadline_after?: string;
  page?: number;
  page_size?: number;
  sort_by?: string;
  sort_dir?: string;
}

export interface UnclassifiedResponse {
  total: number;
  /** Before any filter — so the header can say "12 matching of 4,982". */
  unfiltered_total: number;
  page: number;
  page_size: number;
  pages: number;
  by_source: { source_website: string; count: number }[];
  verticals: string[];
  items: UnclassifiedItem[];
}
