from app.services.amounts import clean_amount


def test_bare_numeric_amount_is_readable_and_does_not_guess_currency():
    assert clean_amount("7651177") == "7,651,177 (currency not listed)"
    assert clean_amount("15,000") == "15,000 (currency not listed)"


def test_separate_recognised_currency_is_joined_to_numeric_amount():
    assert clean_amount("7651177", "EUR") == "EUR 7,651,177"


def test_unknown_currency_field_is_not_trusted():
    assert clean_amount("7651177", "budget") == "7,651,177 (currency not listed)"


def test_already_labelled_unknown_currency_is_idempotent():
    value = "7,651,177 (currency not listed)"
    assert clean_amount(value) == value
