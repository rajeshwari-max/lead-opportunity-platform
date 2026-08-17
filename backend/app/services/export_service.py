"""CSV / Excel export of the currently-filtered result set."""
from __future__ import annotations

import csv
import io

from openpyxl import Workbook

from app.database.models import Opportunity
from app.services.links import resolve_link

_COLUMNS = [
    "unique_id", "title", "organization", "country", "region", "funding_type",
    "vertical", "verticals", "category", "deadline", "website", "opportunity_url",
    # The resolved link — a search URL when no direct one exists, so an
    # exported row is never a dead end either.
    "link",
    "summary", "location", "eligibility", "funding_amount", "status",
    "source_website", "date_scraped",
]


def _row(o: Opportunity) -> list[str]:
    return [
        o.unique_id, o.title, o.organization, o.country, o.region, o.funding_type,
        o.vertical, o.verticals or "", o.category.value,
        o.deadline.isoformat() if o.deadline else "",
        o.website, o.opportunity_url,
        resolve_link(o.opportunity_url, o.website, o.source_website, o.title)[0],
        o.summary, o.location, o.eligibility,
        o.funding_amount, o.status.value, o.source_website,
        o.date_scraped.isoformat(sep=" ", timespec="seconds"),
    ]


def to_csv(rows: list[Opportunity]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_COLUMNS)
    writer.writerows(_row(o) for o in rows)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 correctly


def to_xlsx(rows: list[Opportunity]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Opportunities"
    ws.append(_COLUMNS)
    for o in rows:
        ws.append(_row(o))
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
