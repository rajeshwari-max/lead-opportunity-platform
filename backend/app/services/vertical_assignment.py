"""Assigning verticals by hand, in bulk, so a person's judgement sticks.

Why this is the other half of a change already made
---------------------------------------------------
`verticals_source` and the backfill guard were added so a human label survives
a restart. That protection is meaningless on its own: nothing could set a human
label. This is the half that produces the labels the guard protects.

It also answers the measured problem. 34% of actionable rows carry no vertical
at all, and the dashboard's `has_vertical` filter defaults to ON — so a third of
the database is invisible in the working view, not because anyone decided it was
irrelevant but because the keyword rules had nothing to say about it. Those rows
are exactly the ones a person can label in seconds and the classifier cannot
label at all.

The rules
---------
* A label is a decision, so it records who made it and when.
* Assigning nothing is a decision too — "this belongs to none of our six" —
  and it is stored as a human label with empty verticals rather than as an
  unlabelled row. Otherwise the next backfill re-tags it and the reviewer's
  work is undone, which is the exact failure `is_human_labeled` exists to stop.
* Only canonical vertical names are accepted. A typo stored here would sit in
  the database forever, matching no filter, looking exactly like a correctly
  labelled row.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.database.models import Opportunity
from app.services.actionable import actionable_clause
from app.services.vertical_names import canonical_vertical
from app.services.verticals import HUMAN, VERTICALS

# A cap on one call. Not a technical limit — a review limit. A "bulk assign"
# that silently accepted 10,000 ids would let one mis-click relabel a third of
# the database with no way to tell which rows had been touched.
MAX_BULK = 500


class AssignmentError(ValueError):
    """A request that cannot be applied, with a reason meant for a person."""


@dataclass(frozen=True)
class Unclassified:
    id: int
    title: str
    organization: str
    source_website: str
    opportunity_url: str
    summary: str
    country: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "organization": self.organization,
            "source_website": self.source_website,
            "opportunity_url": self.opportunity_url,
            "summary": (self.summary or "")[:400],
            "country": self.country,
        }


@dataclass(frozen=True)
class UnclassifiedQuery:
    """The filters the brief specifies for the Unclassified section.

    A dedicated model rather than reusing OpportunityFilters: that one carries
    `has_vertical`, whose whole job is to EXCLUDE these rows, and a filter
    object that can contradict the section it filters is a trap.
    """

    search: str = ""
    sources: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    deadline_before: object = None
    deadline_after: object = None
    scraped_after: object = None
    page: int = 1
    page_size: int = 25
    sort_by: str = "date_scraped"
    sort_dir: str = "desc"


def _apply(stmt, q: "UnclassifiedQuery"):
    """Every filter, applied server-side.

    Server-side because select-all has to mean "everything matching this
    filter", not "everything on this page". A bulk action whose scope is the
    visible page is the one that silently does a fraction of what the person
    asked for.
    """
    from sqlalchemy import and_, or_

    if q.search:
        # Each term must appear SOMEWHERE — AND across terms, OR across
        # fields. "solar irrigation" should find a row titled "Solar Pump
        # Scheme" whose summary mentions irrigation; OR across terms would
        # return every solar row and every irrigation row instead.
        for term in [t for t in q.search.split() if t.strip()][:8]:
            like = f"%{term.lower()}%"
            stmt = stmt.where(or_(
                func.lower(Opportunity.title).like(like),
                func.lower(Opportunity.summary).like(like),
                func.lower(Opportunity.organization).like(like),
                func.lower(Opportunity.country).like(like),
            ))
    if q.sources:
        stmt = stmt.where(Opportunity.source_website.in_(list(q.sources)))
    if q.countries:
        stmt = stmt.where(Opportunity.country.in_(list(q.countries)))
    if q.organizations:
        stmt = stmt.where(Opportunity.organization.in_(list(q.organizations)))
    if q.categories:
        from app.database.models import Category
        valid = [Category(c) for c in q.categories
                 if c in Category._value2member_map_]
        if valid:
            stmt = stmt.where(Opportunity.category.in_(valid))
    if q.deadline_after:
        stmt = stmt.where(Opportunity.deadline >= q.deadline_after)
    if q.deadline_before:
        stmt = stmt.where(Opportunity.deadline <= q.deadline_before)
    if q.scraped_after:
        stmt = stmt.where(Opportunity.date_scraped >= q.scraped_after)
    return stmt


def _order(stmt, q: "UnclassifiedQuery"):
    col = {"date_scraped": Opportunity.date_scraped,
           "deadline": Opportunity.deadline,
           "title": Opportunity.title,
           "source_website": Opportunity.source_website}.get(
               q.sort_by, Opportunity.date_scraped)
    return stmt.order_by(col.desc() if q.sort_dir == "desc" else col.asc(),
                         Opportunity.id.desc())


def search_unclassified(db: Session, q: "UnclassifiedQuery") -> dict:
    """One page of the section, plus the total the filter matches.

    `total` is what select-all would act on, and it is returned with every
    page so the UI can say "select all 1,284 matching" honestly instead of
    implying the 25 on screen.
    """
    base = select(Opportunity).where(unclassified_clause())
    filtered = _apply(base, q)
    total = int(db.execute(
        select(func.count()).select_from(filtered.subquery())
    ).scalar_one() or 0)

    page = max(1, q.page)
    size = max(1, min(q.page_size, 200))
    rows = db.execute(
        _order(filtered, q).limit(size).offset((page - 1) * size)
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": size,
        "pages": (total + size - 1) // size,
        "items": [_as_item(o) for o in rows],
    }


def matching_ids(db: Session, q: "UnclassifiedQuery", cap: int = MAX_BULK) -> list[int]:
    """Every id the current filter matches, for select-all.

    Capped at the same limit a bulk assignment accepts, so the UI cannot offer
    a selection the write path will refuse. Returning 5,000 ids and then
    rejecting the request is a worse experience than saying up front that the
    filter is too broad.
    """
    stmt = _order(_apply(select(Opportunity.id).where(unclassified_clause()), q), q)
    return list(db.execute(stmt.limit(cap + 1)).scalars())


def _as_item(o) -> dict:
    """One row, with the model's suggestion attached.

    The suggestion is computed on read rather than stored for these rows,
    because a row is only in this queue when the classifier declined to assert
    anything — there is nothing stored to show. Recomputing gives the reviewer
    the near-misses and the terms behind them, which is the difference between
    "no idea" and "0.42 Health, on the word nutrition".
    """
    from app.services.classification_model import classify

    body = " ".join(filter(None, [o.summary, o.vertical, o.eligibility]))
    c = classify(o.title or "", body)
    suggestions = sorted(
        ({"vertical": v, "score": round(s, 3),
          "evidence": c.evidence.get(v, [])[:4]}
         for v, s in c.scores.items() if s > 0),
        key=lambda d: -d["score"])[:3]
    return {
        "id": o.id,
        "title": o.title or "",
        "organization": o.organization or "",
        "source_website": o.source_website or "",
        "opportunity_url": o.opportunity_url or "",
        "summary": (o.summary or "")[:400],
        "country": o.country or "",
        "category": getattr(o.category, "value", o.category) or "",
        "deadline": o.deadline.isoformat() if o.deadline else None,
        "date_scraped": o.date_scraped.isoformat() if o.date_scraped else None,
        "classification_status": c.status,
        "suggestions": suggestions,
    }


def unclassified_clause():
    """Actionable rows the classifier could not place in any vertical.

    Deliberately excludes rows a person already ruled on — including the ones
    they deliberately left empty. Re-offering those would make the queue
    refill with work someone had already done.
    """
    from sqlalchemy import and_, or_

    return and_(
        actionable_clause(),
        or_(Opportunity.verticals.is_(None), Opportunity.verticals == ""),
        or_(Opportunity.verticals_source.is_(None),
            Opportunity.verticals_source != HUMAN),
    )


def count_unclassified(db: Session) -> int:
    return int(db.execute(
        select(func.count()).select_from(Opportunity).where(unclassified_clause())
    ).scalar_one() or 0)


def by_source(db: Session) -> list[dict]:
    """Where the unclassified rows come from.

    Same reasoning as the review queue: a backlog concentrated in one source is
    a keyword gap for that source's vocabulary, which is fixed once in the
    rules rather than a thousand times by hand.
    """
    rows = db.execute(
        select(Opportunity.source_website, func.count(Opportunity.id))
        .where(unclassified_clause())
        .group_by(Opportunity.source_website)
        .order_by(func.count(Opportunity.id).desc())
    ).all()
    return [{"source_website": r[0] or "(unknown)", "count": int(r[1])} for r in rows]


def fetch_unclassified(db: Session, limit: int = 50, offset: int = 0,
                       source_website: str = "") -> list[Unclassified]:
    """Newest first — the opposite of the deadline review queue, on purpose.

    An unassessed DEADLINE ages into irrelevance, so those are reviewed oldest
    first. An unclassified row is a routing gap: labelling the newest ones puts
    live opportunities in front of the right team this week, while the oldest
    are mostly rows that were never going to be bid on anyway.
    """
    stmt = select(Opportunity).where(unclassified_clause())
    if source_website:
        stmt = stmt.where(Opportunity.source_website == source_website)
    stmt = (stmt.order_by(Opportunity.date_scraped.desc(), Opportunity.id.desc())
                .limit(max(1, min(limit, 200))).offset(max(0, offset)))
    return [
        Unclassified(
            id=o.id, title=o.title or "", organization=o.organization or "",
            source_website=o.source_website or "",
            opportunity_url=o.opportunity_url or "", summary=o.summary or "",
            country=o.country or "",
        )
        for o in db.execute(stmt).scalars()
    ]


def validate(verticals) -> list[str]:
    """Canonical names only, duplicates collapsed, order preserved.

    Raises rather than dropping. A silently discarded vertical would look
    exactly like a successful assignment that happens to match nothing.
    """
    out: list[str] = []
    for name in verticals or []:
        canon = canonical_vertical(name)
        if not canon:
            raise AssignmentError(
                f"{name!r} is not one of the verticals. Known: "
                f"{', '.join(VERTICALS)}")
        if canon not in out:
            out.append(canon)
    return out


def assign(db: Session, opportunity_ids, verticals, reviewer: str = "") -> dict:
    """Set these verticals on these rows, as a human decision.

    An empty `verticals` list is accepted and meaningful: it records "none of
    our six", which the backfill must then leave alone. That is why the row is
    marked `human` even when the value it stores is empty — an empty value with
    no marker is indistinguishable from a row nobody has looked at.
    """
    ids = [int(i) for i in (opportunity_ids or [])]
    if not ids:
        raise AssignmentError("No opportunities were selected.")
    if len(ids) > MAX_BULK:
        raise AssignmentError(
            f"{len(ids)} rows in one request; the limit is {MAX_BULK}. "
            f"That cap exists so a mis-click cannot relabel the database.")

    canonical = validate(verticals)
    value = ", ".join(canonical)

    found = set(db.execute(
        select(Opportunity.id).where(Opportunity.id.in_(ids))
    ).scalars())
    missing = [i for i in ids if i not in found]
    if missing:
        raise AssignmentError(
            f"{len(missing)} of the selected rows no longer exist "
            f"(ids: {', '.join(str(i) for i in missing[:5])}"
            f"{'…' if len(missing) > 5 else ''}).")

    db.execute(
        update(Opportunity)
        .where(Opportunity.id.in_(ids))
        .values(
            verticals=value,
            verticals_source=HUMAN,
            verticals_labeled_by=reviewer or "",
            verticals_labeled_at=datetime.now(timezone.utc),
            # The classification record, kept separate from the model's own.
            # "human" here is what a later re-classification checks before
            # touching a row, and what an audit reads to tell a person's
            # judgement from a threshold's output.
            classification_status=("classified" if canonical else "unclassified"),
            classification_source=HUMAN,
            classification_version=None,   # a person is not a model version
            classified_at=datetime.now(timezone.utc),
        )
    )
    return {
        "updated": len(ids),
        "verticals": canonical,
        "cleared": not canonical,
        "labeled_by": reviewer,
    }


def revert_to_auto(db: Session, opportunity_ids) -> dict:
    """Undo a human label so the classifier owns the row again.

    Needed because "I labelled the wrong batch" has to be recoverable. Without
    it a mis-click is permanent: the backfill skips human rows, so nothing
    would ever re-derive them.
    """
    ids = [int(i) for i in (opportunity_ids or [])]
    if not ids:
        raise AssignmentError("No opportunities were selected.")
    db.execute(
        update(Opportunity)
        .where(Opportunity.id.in_(ids))
        .values(verticals_source=None, verticals_labeled_by=None,
                verticals_labeled_at=None, classification_source=None,
                classified_at=None)
    )
    return {"reverted": len(ids),
            "note": "the classifier will re-derive these at the next backfill"}
