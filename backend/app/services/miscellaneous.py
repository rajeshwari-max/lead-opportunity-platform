"""Miscellaneous — what an unclassified opportunity is *closest* to.

An opportunity lands in Unclassified when no CMS vertical and no brand clears
its threshold. That says nothing about how close it came: a row with half a
Health signal and half a Setu signal looks exactly like a row about nothing at
all. This puts the near misses on the row, as a percentage of each label's own
threshold, and combines them the way the owner asked (2026-10-05):

    1. Score every vertical and every brand. pct = score / that label's
       threshold, so 100% means "would have been assigned on its own".
    2. Sort strongest first. Take the top 1, then the top 2, then the top 3 …
       (n ascending) and add their percentages.
    3. The first n whose sum reaches 100% names the row:
           "Miscellaneous — Health + Setu"
       If even all of them together stay under 100%, it is plain
           "Miscellaneous"
       and the near misses are still listed with their percentages.

What this deliberately is NOT
-----------------------------
It is a DISPLAY label. It never writes `verticals` or `brands`, so routing,
digests and the dashboard's vertical filters behave exactly as before. A label
built by adding up signals that were each too weak to count is a pointer for a
person deciding the row, not a classification the system should act on.

Two things a reader of these numbers should know
------------------------------------------------
* Percentages from different labels can share evidence. A brand and a vertical
  whose keyword lists both contain "social protection" each score that one
  phrase, so "Worker Wellbeing + Setu" can reach 100% from a single word. Each
  sector carries the words that scored it, so this is visible on the row.
* The vertical percentage is measured against the rule that actually decides
  `verticals` (verticals._THRESHOLD, re-applied to every row at startup by
  backfill_verticals), not against classification_model's per-label cut-offs.
  That is the rule that put the row in Unclassified, so it is the right
  yardstick for "how close did it come".
"""
from __future__ import annotations

from dataclasses import dataclass, field

LABEL = "Miscellaneous"
# Enough to read on a row; the sum still uses every candidate.
MAX_SHOWN = 5


@dataclass(frozen=True)
class Sector:
    name: str
    kind: str          # "vertical" | "brand"
    score: float       # in the label's own units
    threshold: float
    evidence: tuple[str, ...] = ()

    @property
    def pct(self) -> float:
        return 100.0 * self.score / self.threshold if self.threshold else 0.0

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "pct": round(self.pct),
                "evidence": list(self.evidence[:4])}


@dataclass
class Miscellaneous:
    label: str = LABEL
    reached: bool = False           # did some top-n combination reach 100%?
    combined: list[Sector] = field(default_factory=list)   # the top-n that did
    combined_pct: float = 0.0
    near_misses: list[Sector] = field(default_factory=list)  # every candidate

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "reached": self.reached,
            "n": len(self.combined),
            "combined": [s.as_dict() for s in self.combined],
            "combined_pct": round(self.combined_pct),
            "sectors": [s.as_dict() for s in self.near_misses[:MAX_SHOWN]],
        }


def _vertical_sectors(title: str, body: str) -> list[Sector]:
    from app.services import verticals as V

    span = V._span_scoring_enabled()
    out: list[Sector] = []
    for vertical in V.VERTICALS:
        if span:
            score = float(V._span_score(vertical, title, body))
        else:
            score = 0.0
        words: list[str] = []
        for pat in V._COMPILED[vertical]:
            for text, weight in ((title, V._TITLE_WEIGHT), (body, V._BODY_WEIGHT)):
                m = pat.search(text) if text else None
                if m is None:
                    continue
                if not span:
                    score += weight
                word = m.group(0).strip().lower()
                if word and word not in words:
                    words.append(word)
        if score > 0:
            out.append(Sector(vertical, "vertical", score, float(V._THRESHOLD),
                              tuple(words)))
    return out


def _brand_sectors(title: str, body: str) -> list[Sector]:
    from app.services.brands import ASSIGNMENT_THRESHOLD, classify_brands

    result = classify_brands(title, body)
    return [Sector(brand, "brand", score, ASSIGNMENT_THRESHOLD,
                   tuple(result.evidence.get(brand, ())))
            for brand, score in result.scores.items() if score > 0]


def describe(title: str, body: str = "") -> Miscellaneous:
    """The Miscellaneous label for one row's text.

    `body` must be built the way ingest builds it — summary, the source's own
    vertical field, eligibility — or the percentages describe different text
    from the one the classifiers judged. body_for() does that.
    """
    title, body = title or "", body or ""
    sectors = _vertical_sectors(title, body) + _brand_sectors(title, body)
    # Strongest first; name breaks ties so the label is stable between calls.
    sectors.sort(key=lambda s: (-s.pct, s.name))

    out = Miscellaneous(near_misses=sectors)
    running = 0.0
    for n, sector in enumerate(sectors, start=1):     # n = 1, 2, 3 … ascending
        running += sector.pct
        if running >= 100.0 - 1e-9:
            out.reached = True
            out.combined = sectors[:n]
            out.combined_pct = running
            out.label = f"{LABEL} — " + " + ".join(s.name for s in out.combined)
            break
    if not out.reached:
        out.combined_pct = running
    return out


def body_for(summary: str | None, vertical: str | None, eligibility: str | None) -> str:
    """The same text ingest and the backfills classify (scraper_manager)."""
    return " ".join(filter(None, [summary, vertical, eligibility]))


def for_row(row) -> dict | None:
    """The label for a stored or serialised opportunity, or None when it is
    classified. A row with ANY vertical or ANY brand already has an owner and
    is never Miscellaneous."""
    if (getattr(row, "verticals", "") or "").strip():
        return None
    if (getattr(row, "brands", "") or "").strip():
        return None
    body = body_for(getattr(row, "summary", ""), getattr(row, "vertical", ""),
                    getattr(row, "eligibility", ""))
    return describe(getattr(row, "title", "") or "", body).as_dict()
