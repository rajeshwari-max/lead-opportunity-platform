# Official opportunity documents

User and admin opportunity tables expose every discovered source document, not
only one PDF. New emails prefer the source PDF when found. When the source does
not publish a downloadable ToR, the signed document endpoint produces a clearly
labelled platform summary PDF from the opportunity title, source, deadline,
amount, summary and eligibility. It never describes that fallback as an official
source document. Existing emails cannot be rewritten.

The PDF columns are added by the existing database initialization. Discovery
runs after scrapes when `LOP_PDF_DISCOVERY_ENABLED` is enabled. Dashboard reads
do not perform network lookups.

After deploying both backend and frontend, inspect all unchecked records on EC2:

```bash
cd ~/Deployment/lead-opportunity-platform/backend
.venv/bin/python scripts/pdf_backfill.py --all --include-archived
```

This can take a long time. Run in an existing persistent terminal session.
Progress is committed in batches; rerunning resumes unchecked rows. The job only
changes pdf_url, pdf_status and pdf_checked_at. It does not delete opportunities.
To retry a bounded batch with missing documents:

```bash
.venv/bin/python scripts/pdf_backfill.py --retry-missing --include-archived --limit 400
```

Discovery checks the stored call page for PDF links. It does not search the
entire web or bypass login, membership or bot restrictions. Login-only sources
are skipped by the post-run public discovery job. DevelopmentAid instead
collects summaries, eligibility and attachment URLs from the JSON/markup already
visible inside its signed-in Playwright session, so no membership cookie is
copied into an unauthenticated HTTP client.
One verified mapping covers DevelopmentAid grant 1697956 to the EIT Culture &
Creativity Shape/Scale call. Other missing original-funder mappings still need
source-specific enrichment; this is not a guarantee of official PDFs for all rows.

The dashboard says no official source file was found when that is the case and
offers the generated summary PDF separately. This is not proof that the funder
has never published a file. Discovery status distinguishes unchecked, not found,
skipped, blocked and errors internally.
