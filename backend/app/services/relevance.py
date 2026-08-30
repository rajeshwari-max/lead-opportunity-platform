"""Does this opportunity actually match what this person asked for?

The complaint
-------------
"The mail relevance is not good." It is not, and the cause is not the choice of
algorithm — it is that the keyword test has no word boundaries.

`matching_service` built its filter as::

    func.lower(Opportunity.title).like(f"%{kw}%")

so a member whose keyword is **ict** is matched by *District*, *Conflict* and
*Restricted*; **ai** is matched by *Maintenance* and *Training*; **it** matches
almost every listing in the database. Measured on twelve representative
titles:

    keyword   substring (today)   word-boundary   false positives
    ict                       3               0                 3
    ai                        2               0                 2
    it                        4               0                 4

Every one of those is a wrong email, and a member with one short keyword
receives a digest that is nearly all noise. No amount of embedding quality
fixes a filter that matches the middle of unrelated words, which is why this
comes before any model.

Two more things were wrong with the old rule
--------------------------------------------
**Any single hit was a match, and all matches were equal.** One keyword
appearing once in a long eligibility paragraph counted exactly as much as three
keywords in the title. Results were then ordered by deadline, so the most
relevant item could sit anywhere in the list.

**Eligibility text was searched like content.** It is boilerplate — "NGOs
registered in India with three years of audited accounts may apply" — and the
words in it describe who may bid, not what the work is. It still contributes,
but weakly, because occasionally it is the only place a sector is named.

What this module does NOT do
----------------------------
It does not decide the ranking is good. Scores are comparable within one
member's list, not across members, and the threshold is a starting point chosen
to be conservative rather than a calibrated value. Calibration needs labelled
examples, which is what `scripts/label_relevance.py` collects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Field weights. A keyword in the title is what the opportunity IS; the same
# word in a paragraph of eligibility boilerplate usually is not.
WEIGHT_TITLE = 3.0
WEIGHT_SUMMARY = 1.0
WEIGHT_VERTICAL = 1.0
WEIGHT_ELIGIBILITY = 0.5

# One title hit, or two hits anywhere else. Deliberately not 1.0: a single
# keyword appearing once in a long summary is the weakest possible evidence and
# was a large share of what made digests noisy.
MIN_SCORE = 2.0


@dataclass(frozen=True)
class Match:
    """Why this row matched, not just that it did.

    The reason travels with the score because a digest someone distrusts is
    only fixable if they can see WHICH of their keywords pulled a row in. A
    bare relevance number gives them nothing to correct.
    """

    score: float
    matched_keywords: tuple[str, ...]
    where: tuple[str, ...]          # the fields that hit, strongest first

    @property
    def is_match(self) -> bool:
        return self.score >= MIN_SCORE

    def explain(self) -> str:
        if not self.matched_keywords:
            return "no keyword matched"
        kws = ", ".join(self.matched_keywords)
        return f"{kws} (in {', '.join(self.where)})"


def compile_keyword(keyword: str) -> re.Pattern[str] | None:
    """A keyword as a whole-word pattern.

    `\\b` is not used at the edges because a keyword may begin or end with a
    non-word character — "M&E" and "C4D" are both real entries in this team's
    inventory, and `\\bm&e\\b` does not mean what it looks like it means.
    Lookarounds for a word character are unambiguous in every case.

    Internal whitespace is made flexible so "health system" also matches
    "health  systems" and "health-system": the person typing a keyword is
    naming a concept, not a byte sequence.
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return None
    parts = [re.escape(p) for p in kw.split()]
    body = r"[\s\-/]+".join(parts)
    # Allow a trailing plural/inflection on the last word only — "farmer"
    # should find "farmers", but not "farmerville".
    body += r"(?:e?s)?"
    try:
        return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)
    except re.error:
        # A keyword that cannot compile must not take the whole digest down.
        return None


def compile_keywords(keywords) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for kw in keywords:
        pat = compile_keyword(kw)
        if pat is not None:
            out.append((kw.strip(), pat))
    return out


def score_opportunity(
    compiled: list[tuple[str, re.Pattern[str]]],
    title: str = "",
    summary: str = "",
    vertical: str = "",
    eligibility: str = "",
) -> Match:
    """How well this row answers this member's keywords.

    A keyword counts ONCE per field, however many times it appears there. A
    long document repeating one word is not more relevant than a short one
    naming it in the title — and rewarding repetition is how a page of
    boilerplate outranks the actual call.
    """
    fields = (
        ("title", title, WEIGHT_TITLE),
        ("summary", summary, WEIGHT_SUMMARY),
        ("vertical", vertical, WEIGHT_VERTICAL),
        ("eligibility", eligibility, WEIGHT_ELIGIBILITY),
    )
    score = 0.0
    hit_keywords: list[str] = []
    hit_fields: list[str] = []

    for kw, pat in compiled:
        kw_scored = False
        for name, text, weight in fields:
            if text and pat.search(text):
                score += weight
                kw_scored = True
                if name not in hit_fields:
                    hit_fields.append(name)
        if kw_scored:
            hit_keywords.append(kw)

    # Strongest field first, so the explanation leads with the best evidence.
    order = {name: i for i, (name, _, _) in enumerate(fields)}
    hit_fields.sort(key=lambda n: order[n])
    return Match(round(score, 2), tuple(hit_keywords), tuple(hit_fields))


def rank(matches: list[tuple[object, Match]]) -> list[tuple[object, Match]]:
    """Most relevant first, deadline breaking ties.

    The old query ordered by deadline alone, so the best match in a digest
    could be anywhere in it — including below where someone stops reading.
    Deadline still matters, but as the tie-break: among rows that answer the
    question equally well, the one closing soonest is the more urgent.
    """
    def key(pair):
        row, m = pair
        deadline = getattr(row, "deadline", None)
        return (-m.score, deadline is None, deadline or _FAR_FUTURE)

    return sorted(matches, key=key)


class _FarFuture:
    """Sorts after every real date without pretending to be one.

    A sentinel date like 9999-12-31 is what put a 2.9-million-day countdown on
    the dashboard, so undated rows are ordered by a flag instead — see the
    `deadline is None` term in the sort key above.
    """

    def __lt__(self, other) -> bool:  # pragma: no cover - ordering guard
        return False

    def __gt__(self, other) -> bool:  # pragma: no cover - ordering guard
        return True


_FAR_FUTURE = _FarFuture()


def like_prefilter_terms(keywords) -> list[str]:
    """Substrings for the SQL prefilter.

    SQLite has no REGEXP without a registered function, so the database does a
    cheap substring narrowing and the exact word-boundary test happens in
    Python on the survivors. That order is safe in the one direction that
    matters: every word-boundary match IS a substring match, so the prefilter
    can only ever over-fetch, never drop a real result.
    """
    terms = []
    for kw in keywords:
        kw = (kw or "").strip().lower()
        if not kw:
            continue
        # Only the first word: "health system" is stored with any separator, so
        # narrowing on the whole phrase would miss "health-system".
        terms.append(kw.split()[0])
    return terms
