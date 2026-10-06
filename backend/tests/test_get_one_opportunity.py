"""GET /api/opportunities/{id} — what the email "Add to Wrike" link opens."""
from __future__ import annotations

from datetime import date, datetime

from test_admin_gating import client, cookie  # noqa: F401  (fixture)


def _add(client_fixture):
    from app.database.models import Category, Opportunity, Status

    # The fixture overrides exactly one dependency, get_db. Looked up by value
    # rather than by the get_db function object, because other test modules
    # reload app.database.db and the key is then a different function.
    (override,) = client_fixture.app.dependency_overrides.values()
    session = override()
    session.add(Opportunity(
        id=55, unique_id="one", title="Supply of office furniture", organization="",
        country="", region="", funding_type="", vertical="", category=Category.TENDER,
        deadline=date(2030, 1, 1), website="", opportunity_url="", summary="",
        location="", eligibility="", funding_amount="", status=Status.ACTIVE,
        source_website="s", date_scraped=datetime(2026, 10, 6)))
    session.commit()


def test_one_row_by_id(client):
    _add(client)
    r = client.get("/api/opportunities/55", cookies=cookie(False))
    assert r.status_code == 200
    assert r.json()["title"] == "Supply of office furniture"
    assert r.json()["miscellaneous"]["label"].startswith("Miscellaneous")


def test_a_missing_id_is_404(client):
    assert client.get("/api/opportunities/99999", cookies=cookie(False)).status_code == 404


def test_the_unclassified_route_is_not_swallowed_by_the_id_route(client):
    """Without the :int convertor, /opportunities/unclassified matches the id
    route first and answers 422."""
    r = client.get("/api/opportunities/unclassified", cookies=cookie(True))
    assert r.status_code == 200
