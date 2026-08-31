"""Prove the effective DevelopmentAid listing URLs used by this runtime.

This intentionally keeps the expected strings independent from application
settings. A test that obtains both sides from the same setting cannot detect a
mistyped or overridden URL.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scrapers.developmentaid import _SECTION_URLS, _page_url  # noqa: E402


EXPECTED = {
    "grants": (
        "https://www.developmentaid.org/grants/search"
        "?languages=92"
        "&sectors=100,7,3,95,5,6,11,54,8,78,80,30,44,87,85,22,34,48,27"
        "&statuses=3"
    ),
    "tenders": (
        "https://www.developmentaid.org/tenders/search"
        "?sectors=100,5,95,3,6,7,78,8,29,9,11,54,80,16,30,44,20,85,87,60,22,43,34,48,27"
        "&statuses=3"
        "&languages=92"
    ),
}

OVERRIDES = {
    "grants": "LOP_DEVAID_GRANTS_URL",
    "tenders": "LOP_DEVAID_TENDERS_URL",
}


def _filters(url: str) -> dict[str, list[str]]:
    parsed = urlparse(url)
    return {
        key: sorted(values)
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        if key != "pageNr"
    }


def audit() -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    passed = True
    for section in ("grants", "tenders"):
        required = EXPECTED[section]
        effective = _SECTION_URLS.get(section, "")
        page_two = _page_url(effective, 2) if effective else ""
        identical = effective == required
        pagination_keeps_filters = (
            _filters(page_two) == _filters(effective)
            and parse_qs(urlparse(page_two).query).get("pageNr") == ["2"]
        )
        row = {
            "section": section,
            "required": required,
            "effective": effective,
            "identical": identical,
            "page_two": page_two,
            "pagination_keeps_filters": pagination_keeps_filters,
            "override_variable": OVERRIDES[section],
            "override_present": OVERRIDES[section] in os.environ,
        }
        rows.append(row)
        passed = passed and identical and pagination_keeps_filters
    return passed, rows


def main() -> int:
    passed, rows = audit()
    for row in rows:
        print(f"\n{row['section'].upper()}")
        print(f"required  : {row['required']}")
        print(f"effective : {row['effective']}")
        print(f"identical : {'yes' if row['identical'] else 'NO'}")
        print(f"page 2    : {row['page_two']}")
        print("paging keeps sectors, statuses and languages: "
              f"{'yes' if row['pagination_keeps_filters'] else 'NO'}")
        if row["override_present"]:
            # Name the cause without echoing arbitrary environment contents.
            print(f"override  : {row['override_variable']} is set")
    print("\nVERDICT: " + (
        "the required filtered searches are what will be requested"
        if passed else
        "FAILED - runtime URLs differ or pagination loses required filters"
    ))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
