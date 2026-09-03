# Parser fixtures

Sanitized fragments of what each priority source returns, so a parser has a
contract that fails when the source changes shape.

**What may live here:** the smallest fragment that exercises the parser — a
listing row, an API record, the "no results" container, the login wall. Values
are replaced with plausible substitutes.

**What must never live here**, per the brief: credentials, cookies, session
tokens, full private pages, or anything a logged-in account can see that an
anonymous visitor cannot. Every file here is safe to publish, and this
repository is public. `test_parser_fixtures.py` enforces that rather than
trusting it.

---

## Provenance — read this before believing a green test

A fixture has one of two provenances, and they buy different things:

* **CAPTURED** — taken from a real response and sanitized. A test against it
  fails when the SOURCE changes shape. This is the one that catches drift.
* **SYNTHETIC** — hand-built from the field names and selectors the parser's
  own code documents. A test against it fails when OUR parser regresses. It
  cannot catch drift at the source, because it was derived from the parser, not
  from the site. Testing a parser against a fixture built from that parser is
  circular, and pretending otherwise is how a suite goes green while every
  scrape returns nothing.

Every synthetic fixture below is a placeholder for a captured one. Replace it
the first time that source completes a run:

```bash
python scripts/check_scraper.py <source> --pages 1 --json /tmp/<source>.json
# then take ONE representative row out of /tmp/<source>.json,
# strip anything account-specific, and overwrite the fixture
```

| Fixture | Source | Provenance | Exercises |
|---|---|---|---|
| `worldbank_procnotice.json` | World Bank | CAPTURED | field names, the award record, the project-titled record |
| `worldbank_empty.json` | World Bank | CAPTURED | `total: 0` as positive proof of emptiness |
| `adb_listing_row.html` | ADB Tenders | CAPTURED | the Label: value spans; an award whose status still says Active |
| `devnet_listing_row.html` | DevNetJobsIndia | CAPTURED (grid) + DERIVED (sidebar) | the direct-link row and the postback-only row. The sidebar block below them is DERIVED, not captured: it is the shape the recovery path matches against, with the `&nbsp;` an ASP.NET GridView really emits. So the NBSP defect it pins is real; the exact sidebar markup is reconstructed, and would not catch DevNet restructuring that widget. |
| `developmentaid_tender_records.json` | DevelopmentAid | SYNTHETIC | `donorIds` never winning the URL; `Id` vs `id`; the 9999-12-31 sentinel |
| `unpp_open_projects.json` | UN Partner Portal | SYNTHETIC | `count` as the coverage basis; a record with no deadline |
| `unpp_empty.json` | UN Partner Portal | SYNTHETIC | `count: 0` as positive proof of emptiness |
| `fundsforngos_posts.json` | FundsForNGOs | SYNTHETIC | the ambiguous `09-01-2027`; a money parenthetical that is not a country |
| `ngobox_listing_card.html` | NGOBOX | SYNTHETIC | `p.p_balck` (their typo), the deadline run, a nav card that must not be stored |
| `bond_opportunity_card.html` | Bond UK | SYNTHETIC | a card with an apply link, and one without — the index-anchor defect |
| `cleanairfund_grants_listing.html` | Clean Air Fund | SYNTHETIC | an open call mixed in with grants already awarded |
| `undp_procurement_listing.html` | UNDP Procurement | SYNTHETIC | the notice table and its published total |
| `devex_paywall.html` | Devex | SYNTHETIC | the wall itself — AUTH_REQUIRED, never "empty source" |
| `adb_pagination_bar.html` | ADB Tenders | CAPTURED (page 1) + DERIVED (last page) | the Next control as ADB really renders it: no numbered buttons, the label `Next >`, and "disabled" expressed as a class plus inline `pointer-events:none`. The first bar is verbatim from `logs/adb_no_results.html`; the second is the same bar with the disabled class moved to Next, because the capture only covers page 1 — so the "end of list" test is derived, not observed, and would not catch ADB changing how it marks the last page. |

`test_parser_fixtures.py` asserts this table lists every file in this directory,
so a fixture cannot be added without declaring where it came from.

---

## Why a fixture at all

The outcome taxonomy can already say "the page loaded and the parser found
nothing" (`PARSE_ZERO`). It cannot say WHY, and without a fixture the only way
to find out is to re-run the scraper against a live site that may have changed
again. A fixture turns "PARSE_ZERO, cause unknown" into a named failure with a
diff, and it fails in CI at the moment the parser stops matching what the
source sends — not three weeks later when someone notices the source went
quiet.
