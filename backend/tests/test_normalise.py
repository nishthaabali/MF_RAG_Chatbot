from datetime import date

import pytest

from ingest import normalise as n


@pytest.mark.parametrize("value", [None, "", "-", "--", "NA", "N/A", "  —  ", "​"])
def test_placeholders_become_none(value):
    assert n.clean_text(value) is None


def test_nil_is_a_real_value_not_a_placeholder():
    assert n.clean_text("Nil") == "Nil"


def test_clean_text_strips_zero_width_and_nbsp():
    assert n.clean_text("0.98​%\xa0 p.a.") == "0.98% p.a."


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0.6958", ("0.70%", 0.7)),
        (0.6958, ("0.70%", 0.7)),
        ("0.98%", ("0.98%", 0.98)),
        ("0.98 %", ("0.98%", 0.98)),
        ("1.005", ("1.01%", 1.01)),
        ("0.98% p.a.", ("0.98%", 0.98)),
    ],
)
def test_expense_ratio_formats(raw, expected):
    assert n.expense_ratio(raw) == expected


@pytest.mark.parametrize("raw", ["98%", "6.17", "-0.5", "0", "abc", None])
def test_implausible_expense_ratio_is_rejected(raw):
    assert n.expense_ratio(raw) == (None, None)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (100, ("₹100", 100.0)),
        ("₹100", ("₹100", 100.0)),
        ("Rs. 1,000", ("₹1,000", 1000.0)),
        ("Rs 500", ("₹500", 500.0)),
        ("INR 5000", ("₹5,000", 5000.0)),
        ("₹1,00,000", ("₹1,00,000", 100000.0)),
        (12345678, ("₹1,23,45,678", 12345678.0)),
    ],
)
def test_amounts_with_indian_grouping(raw, expected):
    assert n.amount(raw) == expected


def test_zero_or_missing_amount_is_none():
    assert n.amount(0) == (None, None)
    assert n.amount("--") == (None, None)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Very High", "Very High"),
        ("Very high risk", "Very High"),
        ("MODERATELY HIGH", "Moderately High"),
        ("Low to Moderate Risk", "Low to Moderate"),
        ("Moderately High Riskometer", None),  # not a recognised level -> never guess
        ("Extreme", None),
    ],
)
def test_riskometer_levels(raw, expected):
    assert n.riskometer(raw) == expected


def test_lock_in_distinguishes_none_from_missing():
    assert n.lock_in({"years": None, "months": None, "days": None}) == "No lock-in"
    assert n.lock_in({"years": 3, "months": None, "days": None}) == "3 years"
    assert n.lock_in({"years": 1, "months": 6}) == "1 year 6 months"
    assert n.lock_in(None) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("29-Sep-2021", date(2021, 9, 29)),
        ("2021-09-29", date(2021, 9, 29)),
        ("2021-09-29T00:00:00", date(2021, 9, 29)),
        ("29 Sep 2021", date(2021, 9, 29)),
        ("Sep 29, 2021", date(2021, 9, 29)),
        ("someday", None),
    ],
)
def test_dates(raw, expected):
    assert n.parse_date(raw) == expected
