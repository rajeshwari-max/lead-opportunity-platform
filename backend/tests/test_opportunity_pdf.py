"""Source PDFs, generated summary PDFs, and how emails link them."""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import httpx
import pytest

from app.database.models import Category, Opportunity, Status, TeamMember
from app.services import opportunity_pdf as P

PAGE = "https://funder.example.org/calls/2026-climate-resilience-rfp"

HTML = """
<html><body>
 <h1>Climate Resilience RFP 2026</h1>
 <p>Download the <a href="/docs/climate-resilience-rfp-terms-of-reference.pdf">Terms of Reference</a>
    and the <a href="https://cdn.example.net/forms/application-form.pdf">application form</a>.</p>
 <footer><a href="/about/annual-report-2025.pdf">Annual report</a>
         <a href="/privacy-policy.pdf">Privacy</a></footer>
</body></html>
"""


def _opp(**kw) -> Opportunity:
    fields = dict(
        id=7, unique_id="u7", title="Climate Resilience RFP 2026", organization="Funder",
        country="India", region="South Asia", funding_type="", vertical="",
        category=Category.RFP, deadline=date(2030, 1, 1), website="https://funder.example.org",
        opportunity_url=PAGE, summary="Resilience work.", location="India",
        eligibility="NGOs", funding_amount="USD 100,000", status=Status.ACTIVE,
        source_website="Funder", brands="Vrutti", verticals="Livelihood",
        archetypes="Devsol", date_scraped=datetime(2026, 10, 1),
    )
    fields.update(kw)
    return Opportunity(**fields)


# ------------------------------------------------------------------ choosing

def test_the_call_document_beats_footer_pdfs():
    assert P.best_pdf(HTML, PAGE, "Climate Resilience RFP 2026") == \
        "https://funder.example.org/docs/climate-resilience-rfp-terms-of-reference.pdf"


def test_a_generic_site_pdf_is_not_mistaken_for_the_call():
    html = '<a href="/about/our-strategy.pdf">Our strategy</a><a href="/privacy.pdf">Privacy</a>'
    assert P.best_pdf(html, PAGE, "Climate Resilience RFP 2026") is None


# ------------------------------------------------------------------ fetching

def _inspect(handler, url=PAGE):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     follow_redirects=True) as client:
            return await P.inspect(client, url, "Climate Resilience RFP 2026")
    return asyncio.run(go())


def test_inspect_finds_the_pdf_on_the_page():
    url, status = _inspect(lambda r: httpx.Response(200, text=HTML,
                                                    headers={"content-type": "text/html"}))
    assert status == P.FOUND and url.endswith("terms-of-reference.pdf")


def test_a_page_that_is_itself_a_pdf_is_the_pdf():
    url, status = _inspect(lambda r: httpx.Response(200, content=b"%PDF-1.7",
                                                    headers={"content-type": "application/pdf"}))
    assert (url, status) == (PAGE, P.FOUND)


@pytest.mark.parametrize("response", [
    httpx.Response(403, text="Forbidden"),
    httpx.Response(429, text="Slow down"),
    httpx.Response(200, text="<title>Just a moment...</title>", headers={"content-type": "text/html"}),
])
def test_bot_walls_are_recorded_not_fought(response):
    assert _inspect(lambda r: response) == (None, P.BLOCKED)


def test_a_listing_page_is_not_searched_for_this_calls_pdf():
    def never(request):
        raise AssertionError("listing pages must not be fetched")
    assert _inspect(never, "https://funder.example.org/grants") == (None, P.SKIPPED)


def test_a_direct_pdf_link_needs_no_fetch():
    def never(request):
        raise AssertionError("no fetch needed")
    url = "https://funder.example.org/docs/rfp.pdf"
    assert _inspect(never, url) == (url, P.FOUND)


# ------------------------------------------------------------------ links + rendering

def _patch_settings(monkeypatch, **values):
    """Set values on every live `settings` object these modules can see.

    Several test modules importlib.reload(app.core.config), which leaves
    modules imported earlier holding the OLD settings object. Patching only
    the current one passed in isolation and failed in a full sequential run
    (it read the real LOP_PUBLIC_BASE_URL from EC2's .env).
    """
    import app.core.auth as auth
    import app.core.config as config
    for target in {id(o): o for o in (config.settings, auth.settings, P.settings)}.values():
        for name, value in values.items():
            monkeypatch.setattr(target, name, value)


def test_signed_links_are_per_opportunity(monkeypatch):
    _patch_settings(monkeypatch, approval_secret="s3cret", public_base_url="http://15.0.0.1")
    url = P.brief_url(7)
    assert url.startswith("http://15.0.0.1/api/brief/7.pdf?sig=")
    sig = url.split("sig=")[1]
    assert P.brief_signature_ok(7, sig)
    assert not P.brief_signature_ok(8, sig)
    assert not P.brief_signature_ok(7, "")


def test_lead_link_prefers_the_source_pdf():
    assert P.lead_link(_opp(pdf_status="found", pdf_url="https://x.org/a.pdf")) == \
        ("https://x.org/a.pdf", "source_pdf")
    href, kind = P.lead_link(_opp(pdf_status="none", pdf_url=None))
    assert (href, kind) == ("", "unavailable")


# ------------------------------------------------------------------ endpoint

@pytest.fixture
def brief_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.api import documents
    from app.database.models import Base

    _patch_settings(monkeypatch, approval_secret="test-secret")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(_opp(id=77, unique_id="u77"))
    session.add(_opp(id=78, unique_id="u78", pdf_status="found",
                     pdf_url="https://funder.example.org/docs/tor.pdf"))
    session.commit()
    app = FastAPI()
    app.include_router(documents.router, prefix="/api")
    # Override the get_db the ROUTE holds. After another test reloads
    # app.database.db, the module's current get_db is a different function and
    # an override keyed on it is ignored — the route then read the real database.
    app.dependency_overrides[documents.get_db] = lambda: session
    with TestClient(app) as c:
        yield c
    session.close()


def test_endpoint_needs_the_signature(brief_client):
    assert brief_client.get("/api/brief/77.pdf?sig=wrong").status_code == 404
    assert brief_client.get("/api/brief/77.pdf").status_code == 404
    ok = brief_client.get(f"/api/brief/77.pdf?sig={P.brief_signature(77)}")
    assert ok.status_code == 404
    assert ok.json()["detail"] == "Official document unavailable"


def test_endpoint_redirects_once_the_source_pdf_is_known(brief_client):
    r = brief_client.get(f"/api/brief/78.pdf?sig={P.brief_signature(78)}",
                         follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://funder.example.org/docs/tor.pdf"


def test_brief_links_are_exempt_from_the_dashboard_login():
    """Email recipients open these from their inbox, not the dashboard."""
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    gate = main.split("async def _require_password", 1)[1].split("return await call_next", 1)[0]
    assert '/brief/")' in gate


# ------------------------------------------------------------------ email

def _member():
    return TeamMember(name="Asha", email="a@example.org", keywords="", categories="",
                      verticals="", active=True, auto_send=False)


def test_email_links_the_pdf_shows_real_archetype_and_intelligence(monkeypatch):
    from app.services import email_service as E

    monkeypatch.setattr(E, "_intelligence_for", lambda opps: {7: {
        "score": 72.4, "priority": "HIGH", "confidence": "MEDIUM",
        "reasons": ["Sector match: livelihood"], "risks": ["Geography: Unknown"]}})
    html = E._digest_html(_member(), [_opp(pdf_status="found",
                                           pdf_url="https://funder.example.org/docs/tor.pdf")])
    assert 'href="https://funder.example.org/docs/tor.pdf"' in html
    assert "PDF from the source" in html
    assert "open the original listing" in html
    assert "<b>Archetype:</b> Devsol" in html
    assert "Work / Archetype" not in html and ">Type</th>" in html
    assert "Company intelligence:" in html and "72% · High priority" in html
    assert "Sector match: livelihood" in html and "Geography: Unknown" in html


def test_email_without_a_source_pdf_reports_unavailable(monkeypatch):
    from app.services import email_service as E

    monkeypatch.setattr(E, "_intelligence_for", lambda opps: {})
    html = E._digest_html(_member(), [_opp(pdf_status="none", archetypes="")])
    assert "/api/brief/" not in html
    assert "Official document unavailable" in html
    assert "<b>Archetype:</b> Not classified" in html
    assert "Company intelligence:" not in html


# ------------------------------------------------------------------ discovery job

def test_discovery_records_results_and_skips_login_only_sources(monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    import app.database.db as dbmod
    from app.database.models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)

    @contextmanager
    def scope():
        s = Session(engine)
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(dbmod, "session_scope", scope)
    with scope() as s:
        s.add(_opp(id=1, unique_id="a"))
        s.add(_opp(id=2, unique_id="b", source_website="DevelopmentAid"))
        s.add(_opp(id=3, unique_id="c", deadline=date(2020, 1, 1)))      # closed: not checked

    seen = []

    async def fake_discover(rows, concurrency, budget):
        seen.extend(r[0] for r in rows)
        return {1: ("https://funder.example.org/docs/tor.pdf", P.FOUND)}

    monkeypatch.setattr(P, "_discover", fake_discover)
    counts = P.discover_pdfs(limit=10)

    assert seen == [1]                       # DevelopmentAid is never fetched
    assert counts[P.FOUND] == 1 and counts[P.SKIPPED] == 1
    with scope() as s:
        one, two, three = (s.get(Opportunity, i) for i in (1, 2, 3))
        assert (one.pdf_status, one.pdf_url) == (P.FOUND, "https://funder.example.org/docs/tor.pdf")
        assert one.pdf_checked_at is not None
        assert two.pdf_status == P.SKIPPED
        assert three.pdf_checked_at is None

    async def archived_discover(rows, concurrency, budget):
        assert [r[0] for r in rows] == [3]
        return {3: (None, P.NONE)}

    monkeypatch.setattr(P, "_discover", archived_discover)
    counts = P.discover_pdfs(limit=10, include_archived=True)
    assert counts[P.NONE] == 1
    assert counts["unchecked_left_for_next_run"] == 0



def test_discovery_runs_after_every_scrape():
    import inspect

    from app.services.scraper_manager import ScraperManager
    src = inspect.getsource(ScraperManager._maintenance)
    assert "discover_pdfs()" in src and "pdf_discovery_enabled" in src


def test_verified_original_call_mapping_is_exact():
    url = "https://www.developmentaid.org/grants/view/1697956/financial-support"
    assert P.discovery_page(url).startswith("https://eit-culture-creativity.eu/")
    other = "https://www.developmentaid.org/grants/view/16979560/other-call"
    assert P.discovery_page(other) == other
    malicious = "https://example.org/grants/view/1697956/financial-support"
    assert P.discovery_page(malicious) == malicious


def test_api_exposes_document_status_for_existing_rows():
    from app.schemas.opportunity import OpportunityOut
    value = OpportunityOut.model_validate(_opp(work_type="", study_type="", approved=False, approved_by="", pdf_url="https://example.org/call.pdf", pdf_status="found"))
    assert value.pdf_url == "https://example.org/call.pdf"
    assert value.pdf_status == "found"
