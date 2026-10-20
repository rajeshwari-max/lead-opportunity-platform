"""The website's own files are never an opportunity's official documents.

DevelopmentAid detail pages were read before the Angular app had rendered,
so the only PDF on the page was the footer's ISO 9001 certificate, and it was
stored as the "Official source document" of opportunities.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from app.schemas.opportunity import RawOpportunity
from app.services.site_chrome import is_site_chrome_document

CERT = {"url": "https://www.developmentaid.org/files/iso-9001-certificate.pdf",
        "label": "Certified to ISO 9001:2015 by QMSCERT+ Certificate No. 230421-8",
        "kind": "pdf"}
TOR = {"url": "https://www.developmentaid.org/files/tor.pdf",
       "label": "Terms of Reference", "kind": "pdf"}


def test_footer_certificate_is_recognised_and_real_documents_are_not():
    assert is_site_chrome_document(CERT)
    assert is_site_chrome_document(json.dumps([CERT]))
    assert not is_site_chrome_document(TOR)
    assert not is_site_chrome_document(
        {"url": "https://x.org/call/terms-and-conditions.pdf", "label": "Terms and conditions"})


@pytest.fixture
def db_mod(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "chrome.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    return db_module


def _raw(docs, checked=True):
    return RawOpportunity(
        title="Call for proposals: community health resilience in Odisha",
        organization="EC", source_website="DevelopmentAid",
        website="https://www.developmentaid.org", summary="",
        opportunity_url="https://www.developmentaid.org/grants/view/123456/call",
        deadline_raw="2099-01-01", source_documents=docs, documents_checked=checked,
    )


def _row(db_mod):
    from sqlalchemy import select

    from app.database.models import Opportunity
    with db_mod.session_scope() as db:
        o = db.execute(select(Opportunity)).scalars().one()
        return o.documents_json, o.pdf_url


def _ingest(batch):
    from app.services.scraper_manager import ScraperManager
    return ScraperManager()._ingest(batch, curated=True, source_key="developmentaid")


def test_new_row_never_stores_the_footer_file(db_mod):
    _ingest([_raw([CERT, TOR])])
    docs, pdf = _row(db_mod)
    assert [d["label"] for d in json.loads(docs)] == ["Terms of Reference"]
    assert pdf == TOR["url"]


def test_rescrape_replaces_a_footer_only_list(db_mod):
    _ingest([_raw([])])                       # first read: nothing usable
    from sqlalchemy import update

    from app.database.models import Opportunity
    with db_mod.session_scope() as db:        # what the old bug stored
        db.execute(update(Opportunity).values(
            documents_json=json.dumps([CERT]), pdf_url=CERT["url"]))
    _ingest([_raw([TOR])])                    # rendered read finds the ToR
    docs, pdf = _row(db_mod)
    assert json.loads(docs) == [TOR] and pdf == TOR["url"]


def test_cleanup_script_strips_footer_files_and_keeps_rows(db_mod):
    from sqlalchemy import update

    from app.database.models import Opportunity
    import scripts.clean_site_chrome_documents as clean

    _ingest([_raw([])])
    with db_mod.session_scope() as db:
        db.execute(update(Opportunity).values(
            documents_json=json.dumps([CERT, TOR]), pdf_url=CERT["url"],
            pdf_status="found"))
    with db_mod.SessionLocal() as s:
        changes = clean.plan(s)
        assert len(changes) == 1
        clean.apply(changes)
        s.commit()
    docs, pdf = _row(db_mod)
    assert json.loads(docs) == [TOR] and pdf == TOR["url"]


def test_detail_json_is_kept_only_for_the_opportunity_being_read():
    from types import SimpleNamespace

    from app.scrapers.developmentaid import DevelopmentAidScraper

    s = DevelopmentAidScraper()
    s._detail_id, s._detail_json = "123456", []

    def resp(url, body, ctype="application/json"):
        return SimpleNamespace(url=url, headers={"content-type": ctype},
                               json=lambda: body)

    s._capture_detail_json(resp("https://www.developmentaid.org/api/frontend/grants/123456", {"a": 1}))
    s._capture_detail_json(resp("https://www.developmentaid.org/api/frontend/grants/999", {"b": 2}))
    s._capture_detail_json(resp("https://www.developmentaid.org/grants/view/123456", {"c": 3}))
    s._capture_detail_json(resp("https://www.developmentaid.org/api/x/123456", "x", "text/html"))
    assert s._detail_json == [{"a": 1}]


def test_fetch_documents_script_selects_and_saves(db_mod, monkeypatch):
    import importlib

    import scripts.devaid_fetch_documents as fetch
    importlib.reload(fetch)                     # bind to this test's database
    _ingest([_raw([], checked=False)])          # never read: documents_json NULL
    leads = fetch.select_leads(include_closed=True, retry_empty=False, limit=0)
    assert len(leads) == 1 and leads[0]["docs"] is None

    item = _raw([CERT, TOR])
    item.summary = "Strengthen community health systems."
    assert fetch.save(leads[0], item) == "documents"
    docs, pdf = _row(db_mod)
    assert json.loads(docs) == [TOR] and pdf == TOR["url"]
    # Done: not selected again.
    assert fetch.select_leads(True, False, 0) == []

    assert fetch.needs_documents("[]", retry_empty=False) is False
    assert fetch.needs_documents("[]", retry_empty=True) is True
    assert fetch.needs_documents(json.dumps([CERT]), retry_empty=False) is True


class _FakeDetail:
    """Just enough of a Playwright page for the enrichment loop."""

    def __init__(self, status, titles, html):
        self.status, self.titles, self.html, self.url = status, list(titles), html, ""

    def on(self, *a):
        pass

    def goto(self, url, **kw):
        from types import SimpleNamespace
        self.url = url
        return SimpleNamespace(status=self.status)

    def title(self):
        return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]

    def wait_for_timeout(self, ms):
        pass

    def wait_for_function(self, *a, **kw):
        pass

    def content(self):
        return self.html

    def close(self):
        pass


def _enrich_with(detail):
    from types import SimpleNamespace

    from app.scrapers.developmentaid import DevelopmentAidScraper
    s = DevelopmentAidScraper()
    page = SimpleNamespace(context=SimpleNamespace(new_page=lambda: detail))
    item = _raw([], checked=False)
    s._enrich_from_signed_in_details(page, [item])
    return s, item


HTML = ('<html><body><h1>Call for proposals: community health resilience in Odisha</h1>'
        '<h2>Description</h2><p>Strengthen community health systems.</p>'
        '<a href="/files/tor.pdf">Terms of Reference</a>'
        '<footer><a href="/files/iso-9001.pdf">Certified to ISO 9001:2015</a></footer>'
        '</body></html>')


def test_a_403_security_check_that_clears_is_read_not_skipped(monkeypatch):
    monkeypatch.setattr("app.scrapers.developmentaid.settings.devaid_detail_enrichment_limit", 10)
    _, item = _enrich_with(_FakeDetail(403, ["Just a moment...", "Call for proposals"], HTML))
    assert item.documents_checked
    assert [d["label"] for d in item.source_documents] == ["Terms of Reference"]
    assert item.summary.startswith("Strengthen community health")


def test_a_check_that_never_clears_is_reported(monkeypatch):
    monkeypatch.setattr("app.scrapers.developmentaid.settings.devaid_detail_enrichment_limit", 10)
    s, item = _enrich_with(_FakeDetail(403, ["Just a moment..."], HTML))
    assert not item.documents_checked
    assert "security check did not clear" in s._detail_skip_reasons[item.opportunity_url]
