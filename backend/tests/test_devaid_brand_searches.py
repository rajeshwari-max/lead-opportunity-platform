"""DevelopmentAid brand-level searches and scheduled inclusion."""
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.scrapers import developmentaid as da
from scripts.devaid_urls import EXPECTED_BRAND


def test_every_supplied_brand_search_is_walked_once():
    walked = [url for url, _ in da._BRAND_SEARCHES]
    sections = set(da._SECTION_URLS.values())
    for url in EXPECTED_BRAND:
        assert (url in walked) != (url in sections), url
    assert len(walked) == len(set(walked)) == 4
    assert {kind for _, kind in da._BRAND_SEARCHES} == {"grants"}


def test_brand_search_pagination_keeps_every_filter():
    for url, _ in da._BRAND_SEARCHES:
        original = parse_qs(urlparse(url).query)
        for n in (2, 3, 17):
            paged = parse_qs(urlparse(da._page_url(url, n)).query)
            assert paged.pop("pageNr") == [str(n)]
            assert paged == original


def test_brand_search_parsing_dedupes_and_rejects_other_sites():
    a = "https://www.developmentaid.org/grants/search?locations=5&statuses=3"
    b = "https://www.developmentaid.org/tenders/search?locations=147"
    raw = f"{a}\n{a} | https://example.org/grants/search?x=1  {b}  https://www.developmentaid.org/news"
    assert da._brand_searches(raw, []) == [(a, "grants"), (b, "tenders")]
    assert da._brand_searches(raw, [a]) == [(b, "tenders")]
    assert da._brand_searches("", []) == []


def test_partitioning_stays_inside_the_searchs_own_filters():
    payload = {"filter": {"sectors": [9, 22, 5], "locations": [5], "donors": []}}
    taxonomies = {
        "https://x/api/dictionary/sectors": [{"id": i, "name": f"s{i}"} for i in range(1, 120)],
        "https://x/api/dictionary/locations": [{"id": i, "name": f"l{i}"} for i in range(1, 250)],
        "https://x/api/dictionary/donors": [{"id": i, "name": f"d{i}"} for i in range(1, 6)],
    }
    dims = da.DevelopmentAidScraper()._dimensions(payload, taxonomies, "grants")
    by_label = {label: [v for v, _ in values] for label, _, values in dims}
    assert by_label["sector"] == [5, 9, 22]          # only the chosen sectors
    assert "location" not in by_label               # India only: nothing to split
    assert by_label["donor"] == [1, 2, 3, 4, 5]      # unfiltered axis: all values


def test_scheduled_run_includes_developmentaid(monkeypatch):
    from app.services import run_lock
    from app.services import scraper_manager as mod

    sources = [SimpleNamespace(name="developmentaid", display_name="DevelopmentAid"),
               SimpleNamespace(name="adb", display_name="ADB")]
    monkeypatch.setattr(mod, "get_scrapers", lambda names=None: list(sources))
    monkeypatch.setattr(mod, "disabled_sources", lambda names: {})
    monkeypatch.setattr(mod.ScraperManager, "_least_recent_first",
                        staticmethod(lambda scrapers: list(scrapers)))
    labels = []

    def refuse(label):
        labels.append(label)
        raise run_lock.LeaseNotAcquired("test", None, 0.0)

    monkeypatch.setattr(mod.run_lock, "acquire", refuse)
    with pytest.raises(RuntimeError):
        asyncio.run(mod.ScraperManager()._start(None, None))
    assert labels == ["2 source(s)"]
