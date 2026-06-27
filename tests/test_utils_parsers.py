"""Regression tests for the broken value parsers (#21 height, #61 int)."""

import pytest

from src.scrapers.utils import parse_height_to_cm, parse_int


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5' 11\"", 180.34),   # feet/inches with a space and straight quotes
        ("5'11\"", 180.34),    # no space
        ("5' 11", 180.34),     # missing trailing inch mark
        ("6' 0\"", 182.88),
        ("5′ 11″", 180.34),  # typographic prime / double-prime
        ("5’ 11”", 180.34),  # curly single/double quotes
    ],
)
def test_parse_height_to_cm_parses_feet_inches(value, expected):
    assert parse_height_to_cm(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["--", "---", "", None, "N/A", "abc"])
def test_parse_height_to_cm_returns_none_for_invalid(value):
    assert parse_height_to_cm(value) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12", 12),
        ("  7 ", 7),
        ("1-2", 1),    # a range no longer raises ValueError -> first integer
        ("-5", -5),    # a well-formed negative is preserved
        ("-", None),
        ("--", None),
        ("N/A", None),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_parse_int(value, expected):
    assert parse_int(value) == expected
