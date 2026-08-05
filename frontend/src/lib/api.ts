import type { DigestRunResult, EmailSettings, Facets, FilterState, Opportunity, Paginated, Progress, ScheduleStatus, SourceInfo, Stats, TeamMember } from "./types";

const BASE = "/api";

export function filterParams(f: FilterState): URLSearchParams {
  const p = new URLSearchParams();
  f.categories.forEach((c) => p.append("categories", c));
  f.verticals.forEach((s) => p.append("verticals", s));
  f.countries.forEach((c) => p.append("countries", c));
  f.regions.forEach((r) => p.append("regions", r));
  f.sources.forEach((s) => p.append("sources", s));
  if (f.search) p.set("search", f.search);
  if (f.deadline_before) p.set("deadline_before", f.deadline_before);
  if (f.deadline_after) p.set("deadline_after", f.deadline_after);
  if (f.archived) p.set("archived", "true");
  if (f.new_today) p.set("new_today", "true");
  if (f.approved) p.set("approved", "true");
  if (f.work_type) p.set("work_type", f.work_type);
  if (f.study_type) p.set("study_type", f.study_type);
  p.set("page", String(f.page));
  p.set("page_size", String(f.page_size));
  p.set("sort_by", f.sort_by);
  p.set("sort_dir", f.sort_dir);
  return p;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  opportunities: (f: FilterState) => get<Paginated>(`/opportunities?${filterParams(f)}`),
  facets: () => get<Facets>("/filters"),
  config: () => get<{ read_only: boolean }>("/config"),
  /** Stats honour the active filters — cards/charts/deadlines follow the selection. */
  stats: (f?: FilterState) => get<Stats>(`/stats${f ? `?${filterParams(f)}` : ""}`),
  sources: () => get<SourceInfo[]>("/sources"),
  progress: () => get<Progress>("/progress"),
  startScrape: (sources: string[], verticals: string[] = []) =>
    fetch(`${BASE}/scrape`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sources, verticals }),
    }),
  /** Human sign-off on a row. 403 on the read-only mirror, by design. */
  approve: async (id: number, approved: boolean): Promise<Opportunity> => {
    const res = await fetch(`${BASE}/opportunities/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json() as Promise<Opportunity>;
  },
  pause: () => fetch(`${BASE}/scrape/pause`, { method: "POST" }),
  resume: () => fetch(`${BASE}/scrape/resume`, { method: "POST" }),
  stop: () => fetch(`${BASE}/stop`, { method: "POST" }),
  getSchedule: () => get<ScheduleStatus>("/schedule"),
  setSchedule: async (body: { mode: string; cron?: string; hour?: number; minute?: number }): Promise<ScheduleStatus | null> => {
    const res = await fetch(`${BASE}/schedule`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.ok ? ((await res.json()) as ScheduleStatus) : null;
  },
  exportUrl: (kind: "csv" | "xlsx", f: FilterState) => `${BASE}/export/${kind}?${filterParams(f)}`,

  // ---- team & lead routing ----
  team: () => get<TeamMember[]>("/team"),
  addMember: (body: Omit<TeamMember, "id" | "created_at">) =>
    fetch(`${BASE}/team`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  updateMember: (id: number, body: Omit<TeamMember, "id" | "created_at">) =>
    fetch(`${BASE}/team/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteMember: (id: number) => fetch(`${BASE}/team/${id}`, { method: "DELETE" }),
  memberMatches: (id: number) => get<Opportunity[]>(`/team/${id}/matches`),
  /** resend=true reissues everything currently matching, including items
   *  already sent — the normal send skips those and would find nothing. */
  sendToMember: (id: number, resend = false) =>
    fetch(`${BASE}/team/${id}/send${resend ? "?resend=true" : ""}`, { method: "POST" }),
  emailStatus: () => get<{ configured: boolean }>("/email/status"),
  emailSettings: () => get<EmailSettings>("/email/settings"),
  updateEmailSettings: async (body: Partial<EmailSettings>): Promise<EmailSettings> => {
    const res = await fetch(`${BASE}/email/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json() as Promise<EmailSettings>;
  },
  /** Fire the daily digest + reminders now, to prove the automation works. */
  runDigestNow: async (): Promise<DigestRunResult> => {
    const res = await fetch(`${BASE}/email/run-now`, { method: "POST" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json() as Promise<DigestRunResult>;
  },
};
