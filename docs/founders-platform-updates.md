# Founders meeting: implementation sequence

Scope confirmed on 26 October 2026. Big Bets describes Catalyst's strategic priorities; it is not a new lead type. Ecosystem, SI and the Claude scraping tool are excluded.

| Order | Jira-ready task | Implemented behaviour | Remaining input / validation |
| --- | --- | --- | --- |
| 1 | Audit existing lead, scoring and Wrike features | Reused saved leads, personal follow-up dates, classifier and company-intelligence profile | No production deployment performed |
| 2 | Create editable keyword directory | Import existing vocabulary once; add, rename, pause and review terms; history and review dates; preserve core classification rules | Updates change the rule classifier and search vocabulary, not the trained ML model or human labels |
| 3 | Create website directory | Import installed sources with stable scraper keys and URLs; record candidates; show run/found/saved totals and last attempt | Candidates need a scraper before they can run. Older runs without stable source keys are not attributed to these totals. |
| 4 | Add manual leads and future follow-up | Save to catalogue and user's saved list; deduplicate; distinguish dated, rolling and unknown deadlines; show due follow-ups | Dashboard follow-ups appear while open or when next opened; no new email automation |
| 5 | Mirror linked Wrike stages and statuses | Retrieve exact task/workflow labels; cache checked state; show in saved leads and workspace; prevent contradictory local stage edits | Live verification remains. Account workflows supported; space-only names may be unavailable, shown explicitly with status group/ID. Change status in Wrike and refresh here. Unlinked leads retain personal stages. |
| 6 | Track website outreach and retirement | Ping/no-response/response/review history; actor/date/notes; require drop reason; paused/dropped sources excluded from new scrape runs | Existing runs are not cancelled. Reactivate before testing a retired source. |
| 7 | Distinguish monetary and recognition awards | Unknown/money/recognition/both with evidence; manual intake and award details; admin corrections | Unknown until reviewed; no guessing from title/amount. Existing scrape category restrictions remain. |
| 8 | Add organisation/resource directory | Role, institution, location, URL, status and reviews; link resources to manual leads | Kedar's data and intended inclusion/exclusion meaning need verification. Existing company exclusions preserved separately. Whitelist does not establish eligibility or automatically boost scores. |
| 9 | Validate Big Bets relevance and scoring | Existing Big Bets-derived profile knowledge and weights preserved; admin profile explains strategic-fit contribution | Original file required to validate coverage and propose evidence-backed priority, label and weight changes against reviewed examples |

## Where to find the changes

- Admin/user dashboards: opportunities stay in the main dashboard. My leads and Directories open from navigation in a side panel; Add lead sits beside the opportunity list controls.
- Follow-ups appear inside My leads, with a due-count badge in navigation.
- My leads: follow-up dates, linked resource organisation and Wrike stage.
- Personal workspace: Wrike status replaces the local stage editor for linked leads.
- Award briefs and expanded admin records: award benefit and evidence.
- Admin Company intelligence: Catalyst profile and recommendation weights.

Directory mutations and award corrections require an administrator. Writes honour existing read-only and origin checks. Personal follow-ups belong to their account; manually entered catalogue details are team-visible.

## Persistence

New tables are additive and created by the existing startup flow. Keyword inventory is imported once; installed websites are added by stable scraper key. Restarting does not restore renamed/retired terms or overwrite directory edits. Keyword rules refresh in the serving process and reload at startup. An eventual multi-worker deployment needs shared rule-cache invalidation.

## Validation

Socket-free service tests cover reviews, directory duplicates, rule updates, lead deduplication, account-scoped follow-ups, award evidence, resource linking, source retirement, Wrike labels and idempotent startup imports.
The focused service and existing keyword-classification suites pass: 63 tests.

HTTP tests in `test_founders_updates.py` require a normal development/CI environment: this execution environment blocks the local socket pair required by TestClient. Run them with existing workspace/Wrike suites before deployment. Live Wrike calls and production data changes were not exercised.

Frontend checks include TypeScript and a production Vite bundle. This Windows environment needs a temporary build configuration using `preserveSymlinks` and a pre-created output folder. Deployment configuration is unchanged.
