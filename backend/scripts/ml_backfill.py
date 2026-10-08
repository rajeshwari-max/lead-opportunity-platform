"""Preview or apply the current hierarchy model to automatic DB rows."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import and_, func, or_, select  # noqa: E402

from app.database.db import session_scope  # noqa: E402
from app.database.models import Opportunity  # noqa: E402
from app.services.actionable import strict_actionable_clause  # noqa: E402
from app.services.ml_hierarchy import backfill_hierarchy, model_status  # noqa: E402
from app.services.verticals import HUMAN  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true", help="include archived rows")
    args = parser.parse_args()
    status = model_status()
    version = status["version"]
    with session_scope() as db:
        clause = and_(
            or_(Opportunity.verticals_source.is_(None), Opportunity.verticals_source != HUMAN),
            or_(Opportunity.classification_version.is_(None), Opportunity.classification_version != version),
        )
        if not args.all:
            clause = and_(clause, strict_actionable_clause())
        pending = db.execute(select(func.count()).select_from(Opportunity).where(clause)).scalar_one()
    if not args.apply:
        print(json.dumps({"mode": "preview", "classifier": status, "pending": pending, "active_only": not args.all}, indent=2))
        return 0
    updated = backfill_hierarchy(active_only=not args.all)
    print(json.dumps({"mode": "applied", "classifier": model_status(), "pending_before": pending, "updated": updated, "active_only": not args.all}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
