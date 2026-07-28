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
  category: string;
  deadline: string | null;
  website: string;
  opportunity_url: string;
  summary: string;
  location: string;
  eligibility: string;
  funding_amount: string;
  status: string;
  source_website: string;
  date_scraped: string;
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

/** Canonical six-vertical system — mirrors backend services/verticals.py. */
export const VERTICALS = [
  "Livelihood",
  "Health",
  "E4C",
  "Climate/Sustainability",
  "Worker Wellbeing",
  "Innovative Finance",
] as const;

export const VERTICAL_DESCRIPTIONS: Record<string, string> = {
  Livelihood: "Agriculture and Rural Management",
  Health: "Health",
  E4C: "Research and Community Engagement",
  "Climate/Sustainability": "Climate / Sustainability",
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
