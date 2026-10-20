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
