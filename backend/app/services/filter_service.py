"""Filtering Engine — all filtering happens in SQL (never re-scrapes).

Free-text search uses SQLite FTS5 (instant across 100k rows) with a LIKE
fallback if the FTS table is unavailable.
"""
from __future__ import annotations

from datetime import date
from math import ceil

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.models import Category, Opportunity, ScrapeRun, Status
from app.schemas.opportunity import (
    OpportunityFilters,
    OpportunityOut,
    PaginatedOpportunities,
    StatsOut,
)
from app.services.verticals import VERTICALS

_SORTABLE = {
    "deadline": Opportunity.deadline,
    "title": Opportunity.title,
    "organization": Opportunity.organization,
    "category": Opportunity.category,
    "date_scraped": Opportunity.date_scraped,
    "source_website": Opportunity.source_website,
}


class FilterService:
    """Stateless query builder; injected with a per-request Session."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # --------------------------------------------------------------- queries
    def query(self, f: OpportunityFilters) -> PaginatedOpportunities:
        stmt = self._base_statement(f)
        total = self.db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()

        sort_col = _SORTABLE.get(f.sort_by, Opportunity.deadline)
        order = sort_col.desc() if f.sort_dir == "desc" else sort_col.asc()
        stmt = stmt.order_by(order.nullslast())  # ongoing (no deadline) listed after dated ones

        page_size = max(1, min(f.page_size, 200))
        page = max(1, f.page)
        rows = self.db.execute(stmt.offset((page - 1) * page_size).limit(page_size)).scalars().all()

        return PaginatedOpportunities(
            items=[OpportunityOut.model_validate(r) for r in rows],
            total=total, page=page, page_size=page_size,
            pages=max(1, ceil(total / page_size)),
        )

    def rows_for_export(self, f: OpportunityFilters) -> list[Opportunity]:
        stmt = self._base_statement(f).order_by(Opportunity.deadline.asc())
        return list(self.db.execute(stmt).scalars().all())

    def _base_statement(self, f: OpportunityFilters) -> Select:
        if getattr(f, "approved", False):
            # The approved set is a curated hand-off to the retrieval layer, not
            # a view of what's currently biddable, so it deliberately ignores the
            # live/archived split. Filtering it to open deadlines would make the
            # list silently empty itself as those deadlines passed — the one
            # place where a row disappearing is most alarming. Every other
            # filter below still applies, so it composes with country, vertical
            # and search as usual.
            stmt = select(Opportunity).where(Opportunity.approved.is_(True))
        elif getattr(f, "archived", False):
            # Explicit opt-in to the historical archive. It holds tens of
            # thousands of closed calls, so it stays out of the default view and
            # out of the stat cards unless asked for.
            stmt = select(Opportunity).where(Opportunity.status == Status.EXPIRED)
        else:
            stmt = select(Opportunity).where(
                Opportunity.status == Status.ACTIVE,
                # active = deadline still open, or explicitly ongoing (NULL deadline)
                or_(Opportunity.deadline >= date.today(), Opportunity.deadline.is_(None)),
            )
        if getattr(f, "new_today", False):
            # Matches the "New Today" stat card. Clicking it used to only change
            # the sort order, so the table looked identical and the card seemed
            # broken; it now narrows to exactly what the card counted.
            stmt = stmt.where(func.date(Opportunity.date_scraped) == date.today())
        if getattr(f, "work_type", ""):
            stmt = stmt.where(Opportunity.work_type == f.work_type)
        if getattr(f, "study_type", ""):
            stmt = stmt.where(Opportunity.study_type == f.study_type)
        # English-only and classified-only are unconditional, not options. The
        # team works in English and routes work by vertical, so a row that is
        # neither readable nor ownable is not a lead — it is noise in every
        # view, every export and every digest. Making them togglable put the
        # burden on the reader to remember to switch them on.
        if True:
            for lo, hi in (("\u0590", "\u05ff"), ("\u0600", "\u06ff"),
                           ("\u0400", "\u04ff"), ("\u0900", "\u097f"),
                           ("\u0e00", "\u0e7f"), ("\u4e00", "\u9fff"),
                           ("\u3040", "\u30ff"), ("\uac00", "\ud7af")):
                stmt = stmt.where(~Opportunity.title.op("GLOB")(f"*[{lo}-{hi}]*"))
        if True:
            stmt = stmt.where(
                Opportunity.verticals.is_not(None), Opportunity.verticals != ""
            )
        if f.categories:
            stmt = stmt.where(Opportunity.category.in_([Category(c) for c in f.categories]))
        if f.verticals:
            stmt = stmt.where(self._vertical_clause(f.verticals))
        if f.countries:
            stmt = stmt.where(Opportunity.country.in_(f.countries))
        if f.regions:
            stmt = stmt.where(Opportunity.region.in_(f.regions))
        if f.sources:
            stmt = stmt.where(Opportunity.source_website.in_(f.sources))
        if f.organizations:
            stmt = stmt.where(Opportunity.organization.in_(f.organizations))
        if f.deadline_after:
            stmt = stmt.where(Opportunity.deadline >= f.deadline_after)
        if f.deadline_before:
            stmt = stmt.where(Opportunity.deadline <= f.deadline_before)
        if f.search.strip():
            stmt = stmt.where(Opportunity.id.in_(self._search_ids(f.search.strip())))
        return stmt

    @staticmethod
    def _vertical_clause(selected: list[str]):
        """Match any selected canonical vertical.

        `verticals` is comma-separated multi-label ("Health, Climate/Sustainability"),
        so membership is a LIKE test; the legacy free-text `vertical` column is
        matched too for pre-classification rows. OR across selections =
        multi-select union, as the sidebar implies.
        """
        clauses = []
        for s in selected:
            clauses.append(Opportunity.verticals.like(f"%{s}%"))
            clauses.append(func.lower(Opportunity.vertical) == s.lower())
        return or_(*clauses)

    def _search_ids(self, query: str) -> Select | list[int]:
        """FTS5 prefix search ('health' → health*), LIKE fallback."""
        try:
            fts_query = " ".join(f'"{tok}"*' for tok in query.split()[:6])
            rows = self.db.execute(
                text("SELECT rowid FROM opportunities_fts WHERE opportunities_fts MATCH :q"),
                {"q": fts_query},
            ).scalars().all()
            return list(rows)
        except Exception:
            like = f"%{query}%"
            return select(Opportunity.id).where(
                Opportunity.title.ilike(like)
                | Opportunity.organization.ilike(like)
                | Opportunity.summary.ilike(like)
                | Opportunity.vertical.ilike(like)
            )

    # ---------------------------------------------------------------- facets
    def facets(self, f: OpportunityFilters | None = None) -> dict[str, list[str]]:
        """Distinct values powering the filter sidebar (normalized across sources).

        Every step is defensive about NULLs and non-strings. This endpoint feeds
        the entire filter sidebar, so when it raises, the sidebar renders nothing
        at all — the user sees an app with no filters and no error, which is a
        very expensive way to signal one bad row. Rows imported from another
        database can carry NULL in columns the ORM treats as always-string
        (SQLAlchemy defaults apply on insert through the ORM, not at the DB
        level), and `NULL != ''` is NULL in SQL, so such rows slip past the
        obvious guard.
        """
        f = f or OpportunityFilters()

        # Each facet is computed against every OTHER active filter, but not its
        # own. Applying its own filter too would collapse the list to whatever
        # is already picked — choose one source and the source dropdown would
        # offer only that source, leaving no way to add a second or see what
        # else exists. Excluding it is what makes the control still usable
        # after a selection.
        #
        # Statements are cached by the excluded field, because when only one
        # filter is active the other facets all share the same statement and
        # would otherwise repeat an identical scan five times.
        stmt_cache: dict[str, object] = {}

        def scoped(exclude: str):
            if exclude not in stmt_cache:
                narrowed = f.model_copy(update={exclude: []}) if getattr(f, exclude, None) else f
                stmt_cache[exclude] = self._base_statement(narrowed).subquery()
            return stmt_cache[exclude]

        def distinct(col_name: str, exclude: str) -> list[str]:
            col = scoped(exclude).c[col_name]
            rows = self.db.execute(
                select(col).where(col.is_not(None)).where(col != "").distinct()
            ).scalars().all()
            # `category` comes back as the Category enum, whose str() is
            # "Category.GRANT" — unwrap it to the value the UI filters on
            # ("Grant"), or the category list silently comes back empty.
            out = set()
            for r in rows:
                if r is None:
                    continue
                v = r.value if hasattr(r, "value") else str(r)
                if v.strip():
                    out.add(v)
            return sorted(out)

        def narrowed_or_all(col_name: str, exclude: str) -> list[str]:
            """Narrowed values, falling back to the full list when empty.

            A narrowed facet that comes back empty removes the whole section
            from the sidebar, and the user sees filters "disappearing" — which
            is what happens the moment you pick a source whose rows carry no
            country at all (UNDP Procurement and World Bank both do, because
            their detail pages aren't scraped yet). A control that vanishes
            reads as a broken app; showing every value is worse only in that
            some choices return nothing, which is visible and recoverable.
            """
            values = distinct(col_name, exclude)
            if values:
                return values
            return distinct_unfiltered(col_name)

        def distinct_unfiltered(col_name: str) -> list[str]:
            col = getattr(Opportunity, col_name)
            rows = self.db.execute(
                select(col).where(col.is_not(None)).where(col != "").distinct()
            ).scalars().all()
            out = set()
            for r in rows:
                v = r.value if hasattr(r, "value") else str(r)
                if v and v.strip():
                    out.add(v)
            return sorted(out)

        def keep_selected(values: list[str], chosen: list[str]) -> list[str]:
            """Never hide something the user has currently ticked.

            A narrowed list can otherwise strand a selection: pick source X,
            then filter to a vertical X has no rows in, and X disappears from
            the dropdown while still filtering the table — an empty result with
            no visible control to undo it.
            """
            return sorted(set(values) | {c for c in (chosen or []) if c})

        present_sources = set(distinct("source_website", "sources"))
        present_categories = set(distinct("category", "categories"))
        present_verticals = " | ".join(distinct("verticals", "verticals"))

        return {
            # Fixed taxonomies keep their canonical order rather than being
            # re-sorted alphabetically by whatever the data happens to contain.
            "categories": [
                c.value for c in Category
                if c.value in settings.enabled_categories
                and (not present_categories
                     or c.value in present_categories
                     or c.value in (f.categories or []))
            ],
            # The six verticals are a fixed taxonomy and always shown. Hiding
            # one because the current selection has no rows in it removes the
            # only control that could widen the selection again.
            "verticals": list(VERTICALS),
            "countries": keep_selected(narrowed_or_all("country", "countries"), f.countries),
            "regions": keep_selected(narrowed_or_all("region", "regions"), f.regions),
            # Only sources that actually have a row in the current view. The
            # registry baseline is deliberately not merged in here any more: it
            # listed all 86 configured scrapers regardless of whether any of
            # them had produced a single matching row, which is precisely the
            # noise this is meant to remove.
            "sources": keep_selected(sorted(present_sources), f.sources),
            "organizations": distinct("organization", "organizations")[:500],
        }

    # ----------------------------------------------------------------- stats
    def stats(self, f: OpportunityFilters | None = None) -> StatsOut:
        """Dashboard statistics. When filters are passed (vertical, category,
        search, …) every number/chart/deadline reflects the filtered subset —
        with no filters the behaviour is identical to before."""
        f = f or OpportunityFilters()
        # Stats ignore pagination/sort but honour every data filter.
        active = self._base_statement(f).subquery()

        def group_count(col_name: str) -> dict[str, int]:
            col = active.c[col_name]
            rows = self.db.execute(
                select(col, func.count()).where(col != "").group_by(col)
                .order_by(func.count().desc()).limit(12)
            ).all()
            return {(k.value if hasattr(k, "value") else str(k)): v for k, v in rows}

        def vertical_counts() -> dict[str, int]:
            """Counts per canonical vertical (multi-label — one row can count in
            several verticals, like the classification itself)."""
            counts: dict[str, int] = {}
            for s in VERTICALS:
                n = self.db.execute(
                    select(func.count()).select_from(active).where(
                        active.c.verticals.like(f"%{s}%")
                    )
                ).scalar_one()
                if n:
                    counts[s] = n
            return counts

        total = self.db.execute(select(func.count()).select_from(active)).scalar_one()
        todays = self.db.execute(
            select(func.count()).select_from(active).where(
                func.date(active.c.date_scraped) == date.today().isoformat()
            )
        ).scalar_one()
        upcoming_ids = select(active.c.id).where(active.c.deadline >= date.today())
        upcoming = self.db.execute(
            select(Opportunity)
            .where(Opportunity.id.in_(upcoming_ids))
            .order_by(Opportunity.deadline.asc()).limit(8)   # dated only — ongoing has no urgency
        ).scalars().all()
        last = self.db.execute(
            select(func.max(ScrapeRun.finished_at))
        ).scalar_one_or_none()

        return StatsOut(
            total_active=total,
            # Only enabled categories surface in cards/charts (hides Challenge/Other)
            by_category={
                k: v for k, v in group_count("category").items()
                if k in settings.enabled_categories
            },
            by_region=group_count("region"),
            by_vertical=vertical_counts(),
            todays_new=todays,
            upcoming_deadlines=[OpportunityOut.model_validate(o) for o in upcoming],
            last_scraped=last,
        )
