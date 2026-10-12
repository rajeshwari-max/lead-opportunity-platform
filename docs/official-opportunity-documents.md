# Official opportunity documents

User and admin opportunity tables expose discovered source PDFs. New emails
link to the source PDF when found, otherwise to the original listing and state
“Official document unavailable”. No generated summary is substituted, including
for old signed brief links. Existing emails cannot be rewritten.

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
are skipped unless an exact, verified original call-page mapping is present.
One verified mapping covers DevelopmentAid grant 1697956 to the EIT Culture &
Creativity Shape/Scale call. Other missing original-funder mappings still need
source-specific enrichment; this is not a guarantee of official PDFs for all rows.

The unavailable label means no official PDF has been discovered, not proof that
the funder has never published one. Discovery status distinguishes unchecked,
not found, skipped, blocked and errors internally.
