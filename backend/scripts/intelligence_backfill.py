"""Compute explainable company-intelligence snapshots for active opportunities."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.database.db import SessionLocal, init_db  # noqa: E402
from app.database.models import ModelRegistry, Opportunity  # noqa: E402
from app.services.actionable import strict_actionable_clause  # noqa: E402
from app.services.company_intelligence import (  # noqa: E402
    MODEL_VERSION, HistoricalIndex, analyze_opportunity,
)
from app.services.ml_hierarchy import model_status  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        query = (select(Opportunity).where(strict_actionable_clause())
                 .order_by(Opportunity.deadline, Opportunity.id).limit(max(1, args.limit)))
        rows = list(db.scalars(query))
        index = HistoricalIndex.from_db(db)
        if args.apply:
            classifier = model_status()
            db.merge(ModelRegistry(
                version=MODEL_VERSION,
                kind="company_recommendation",
                status="active",
                metrics=json.dumps({"method": "explainable_weighted_rules"}),
                artifact_path="backend/app/services/company_intelligence.py",
            ))
            db.merge(ModelRegistry(
                version=classifier["version"],
                kind="hierarchy_classifier",
                status="active" if classifier["loaded"] else "fallback",
                metrics=json.dumps({
                    "training_rows": classifier["training_rows"],
                    "gold_rows": classifier["gold_rows"],
                    "unresolved_rows_excluded": classifier["unresolved_rows_excluded"],
                }),
                artifact_path=classifier["path"],
            ))
            db.commit()
            for number, row in enumerate(rows, 1):
                analyze_opportunity(db, row, persist=True, index=index, commit=False)
                if number % 100 == 0:
                    db.commit()
            db.commit()
        print(json.dumps({
            "mode": "apply" if args.apply else "dry-run",
            "eligible_opportunities": len(rows),
            "historical_leads": len(index.rows),
        }, indent=2))


if __name__ == "__main__":
    main()
