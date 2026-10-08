"""Preview or apply approved review-workbook labels to the opportunity DB."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.db import init_db  # noqa: E402
from app.services.review_labels import apply_hierarchy_reviews, preview_hierarchy_reviews  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewer", default="completed-review-workbook")
    args = parser.parse_args()

    import json

    init_db()
    preview = preview_hierarchy_reviews(args.labels)
    if not args.apply:
        print(json.dumps({"mode": "preview", **preview}, indent=2))
        return 0
    updated = apply_hierarchy_reviews(args.labels, args.reviewer)
    print(json.dumps({"mode": "applied", **preview, "updated": updated}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
