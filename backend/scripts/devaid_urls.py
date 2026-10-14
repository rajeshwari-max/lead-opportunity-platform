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

from app.scrapers.developmentaid import (  # noqa: E402
    _BRAND_SEARCHES, _SECTION_URLS, _page_url,
)


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

# The team's brand-level searches, as supplied. The last one is the same search
# as EXPECTED["grants"], so the runtime walks it once, not twice.
EXPECTED_BRAND = [
    "https://www.developmentaid.org/grants/search?applicantTypes=1&languages=92"
    "&locations=5&sectors=9,22,5,3,104,7,11,80,30,48,27&statuses=3",
    "https://www.developmentaid.org/grants/search?applicantTypes=1&languages=92"
    "&locations=5&sectors=100,7,5,80,30,44,87,22,85&statuses=3",
    "https://www.developmentaid.org/grants/search?applicantTypes=1&languages=92"
    "&locations=5&sectors=11,7,5,8,9,30,22,48,27&statuses=3",
    "https://www.developmentaid.org/grants/search?applicantTypes=1&locations=147"
    "&sectors=100,3,5,9,8,29,30,85,87,43,22&statuses=3",
    "https://www.developmentaid.org/grants/search?languages=92"
    "&sectors=100,7,3,95,5,6,11,54,8,78,80,30,44,87,85,22,34,48,27&statuses=3",
]
BRAND_OVERRIDE = "LOP_DEVAID_BRAND_SEARCH_URLS"


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

    # Brand searches: every supplied URL is either walked as given or is
    # already one of the section searches above.
    walked = [url for url, _kind in _BRAND_SEARCHES]
    for i, required in enumerate(EXPECTED_BRAND, 1):
        covered_by_section = required in _SECTION_URLS.values()
        effective = required if (required in walked or covered_by_section) else ""
        page_two = _page_url(effective, 2) if effective else ""
        pagination_keeps_filters = bool(effective) and (
            _filters(page_two) == _filters(effective)
            and parse_qs(urlparse(page_two).query).get("pageNr") == ["2"]
        )
        rows.append({
            "section": f"brand-{i}",
            "required": required,
            "effective": effective,
            "identical": bool(effective),
            "page_two": page_two,
            "pagination_keeps_filters": pagination_keeps_filters,
            "override_variable": BRAND_OVERRIDE,
            "override_present": BRAND_OVERRIDE in os.environ,
            "note": "same as the grants section search" if covered_by_section else "",
        })
        passed = passed and bool(effective) and pagination_keeps_filters
    return passed, rows


def main() -> int:
    passed, rows = audit()
    for row in rows:
        print(f"\n{row['section'].upper()}")
        print(f"required  : {row['required']}")
        print(f"effective : {row['effective']}")
        print(f"identical : {'yes' if row['identical'] else 'NO'}")
        if row.get("note"):
            print(f"note      : {row['note']}")
        print(f"page 2    : {row['page_two']}")
        print("paging keeps every filter: "
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
