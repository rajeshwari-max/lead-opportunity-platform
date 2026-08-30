"""Matching Engine — finds active opportunities relevant to a team member.

A member matches an opportunity when:
  * any of their keywords appears in title/summary/vertical/eligibility (case-insensitive), AND
  * the opportunity's category is in their category list (empty list = all categories), AND
  * the opportunity belongs to one of their verticals (empty list = all verticals), AND
  * it hasn't already been sent to them (SentLog), unless include_sent=True.
"""
from __future__ import annotations

import logging

from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database.models import Category, Opportunity, SentLog, Status, TeamMember
from app.services import geo_routing, relevance
from app.services.actionable import actionable_clause
from app.services.vertical_names import normalize_vertical_csv
from app.services.verticals import VERTICALS


log = logging.getLogger("scraper")


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class MatchingService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def matches_for(
        self, member: TeamMember, include_sent: bool = False, limit: int | None = None
    ) -> list[Opportunity]:
        """Every unsent match for this member. `limit=None` means no cap.

        This used to default to 100, which silently truncated both the count
        shown against each member and the digest actually emailed — a member
        with 900 matches saw "100 new" and received 100.
        """
        # Was a third copy of the "still open" rule, and it had drifted from
        # the other two: `deadline IS NULL` counted as open, so every row whose
        # date could not be parsed was emailed as a live opportunity.
        stmt = select(Opportunity).where(actionable_clause())

        keywords = _csv(member.keywords)
        if keywords:
            # SQL narrows; Python decides.
            #
            # This used to be the whole test, and it had no word boundaries:
            # `%ict%` matched District, Conflict and Restricted; `%ai%` matched
            # Maintenance and Training; `%it%` matched almost every row in the
            # database. A member with one short keyword received a digest that
            # was mostly noise, and no ranking model fixes a filter that
            # matches the middle of unrelated words.
            #
            # SQLite has no REGEXP without a registered function, so the LIKE
            # stays as a cheap prefilter and the exact whole-word test runs in
            # Python below. That order is safe in the direction that matters:
            # every word-boundary match is also a substring match, so the
            # prefilter can over-fetch but can never drop a real result.
            clauses = []
            for term in relevance.like_prefilter_terms(keywords):
                like = f"%{term}%"
                clauses.append(func.lower(Opportunity.title).like(like))
                clauses.append(func.lower(Opportunity.summary).like(like))
                clauses.append(func.lower(Opportunity.vertical).like(like))
                clauses.append(func.lower(Opportunity.eligibility).like(like))
            if clauses:
                stmt = stmt.where(or_(*clauses))

        categories = _csv(member.categories)
        if categories:
            valid = [Category(c) for c in categories if c in Category._value2member_map_]
            if valid:
                stmt = stmt.where(Opportunity.category.in_(valid))

        # Vertical routing: only email opportunities in the member's selected
        # verticals (empty = all verticals, preserving pre-vertical behaviour).
        #
        # The saved value is normalised first. One member is stored with both
        # "Climate/Sustainability" and "Climate/Sustainability(ESG)" — the old
        # name and its replacement — and that routes correctly today only
        # because this is a substring test and the old name is a prefix of the
        # new one. Resolving the name here means the filter no longer depends
        # on that coincidence. See services/vertical_names.py.
        normalized, unknown = normalize_vertical_csv(
            getattr(member, "verticals", "") or "")
        if unknown:
            # Not dropped silently: a vertical nobody recognises matches
            # nothing, which looks exactly like a working filter that happens
            # to find nothing.
            log.warning(
                "[matching] %s has unrecognised vertical(s) in their routing: "
                "%s — those are matching nothing. Known verticals: %s",
                member.email, ", ".join(unknown), ", ".join(VERTICALS),
            )
        verticals = _csv(normalized)
        if verticals:
            stmt = stmt.where(
                or_(*[Opportunity.verticals.like(f"%{s}%") for s in verticals])
            )

        # Geography. Empty means everywhere, like every other field here, so
        # this changes nothing for anyone until they choose one.
        #
        # It is the axis that was missing when an Australian council's
        # micro-grant for individuals reached a member whose filter reads
        # Health / E4C / Livelihood: geography existed only as a dashboard
        # filter and the digest never consulted it.
        countries, bad_countries = geo_routing.normalize_countries(
            getattr(member, "countries", "") or "")
        regions, bad_regions = geo_routing.normalize_regions(
            getattr(member, "regions", "") or "")
        if bad_countries or bad_regions:
            log.warning(
                "[matching] %s has unrecognised place name(s) in their "
                "routing: %s — those match nothing until corrected",
                member.email, ", ".join(bad_countries + bad_regions),
            )
        geo = geo_routing.geo_clause(
            geo_routing.parse_csv(countries),
            geo_routing.parse_csv(regions),
            include_unknown=bool(getattr(member, "geo_include_unknown", True)),
        )
        if geo is not None:
            stmt = stmt.where(geo)

        if not include_sent:
            sent = select(SentLog.opportunity_id).where(SentLog.member_id == member.id)
            stmt = stmt.where(Opportunity.id.not_in(sent))

        # The limit is applied AFTER scoring, not in SQL. Truncating in the
        # database would cut the list by deadline and then rank whatever
        # survived — so a member's single best match could be dropped before
        # anything had judged it.
        rows = list(self.db.execute(stmt).scalars().all())
        if not keywords:
            # No keywords means "send me everything", which is the documented
            # behaviour and not something to score. Deadline order, as before.
            rows.sort(key=lambda o: (o.deadline is None, o.deadline or date.max))
            return rows[:limit] if limit is not None else rows

        scored = self.score_rows(rows, keywords)
        kept = [(row, m) for row, m in scored if m.is_match]
        ranked = relevance.rank(kept)
        result = [row for row, _ in ranked]
        return result[:limit] if limit is not None else result

    def score_rows(self, rows, keywords) -> list[tuple[Opportunity, relevance.Match]]:
        """Score every candidate row. Exposed so the reasons can be shown.

        A digest someone distrusts is only fixable if they can see which of
        their keywords pulled a row in; a bare relevance number gives them
        nothing to correct.
        """
        compiled = relevance.compile_keywords(keywords)
        return [
            (
                row,
                relevance.score_opportunity(
                    compiled,
                    title=row.title or "",
                    summary=row.summary or "",
                    vertical=row.vertical or "",
                    eligibility=row.eligibility or "",
                ),
            )
            for row in rows
        ]

    def mark_sent(self, member: TeamMember, opportunities: list[Opportunity]) -> None:
        """Record what went out, refreshing the timestamp on anything resent.

        SentLog has a unique constraint on (member_id, opportunity_id), so a
        plain insert raises IntegrityError the moment a resend includes an
        opportunity the member already had — which is every resend. Existing
        rows get their sent_at bumped instead, keeping "when did they last see
        this" accurate.
        """
        if not opportunities:
            return
        ids = [o.id for o in opportunities]
        already = {
            row.opportunity_id: row
            for row in self.db.execute(
                select(SentLog).where(
                    SentLog.member_id == member.id, SentLog.opportunity_id.in_(ids)
                )
            ).scalars()
        }
        now = datetime.now(timezone.utc)
        for opp in opportunities:
            existing = already.get(opp.id)
            if existing is not None:
                existing.sent_at = now
            else:
                self.db.add(SentLog(member_id=member.id, opportunity_id=opp.id, sent_at=now))
