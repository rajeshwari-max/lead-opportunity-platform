from app.services.amounts import clean_amount


def test_bare_numeric_amount_is_hidden_when_currency_is_missing():
    assert clean_amount("7651177") == ""
    assert clean_amount("15,000") == ""


def test_separate_recognised_currency_is_joined_to_numeric_amount():
    assert clean_amount("7651177", "EUR") == "EUR 7,651,177"


def test_unknown_currency_field_is_not_trusted():
    assert clean_amount("7651177", "budget") == ""


def test_already_labelled_unknown_currency_is_removed():
    assert clean_amount("7,651,177 (currency not listed)") == ""


def test_structured_api_payload_is_never_shown_as_an_amount():
    payload = '{"budgetYearsColumns":["2026"],"maxContribution":15000000}'
    assert clean_amount(payload) == ""
