"""A 0 from ESPN is a HOLE, not a measurement: the converters must return None.

THE BUG THIS PINS DOWN. The ESPN API sends `0` for `height`, `reach` and `weight` when
it does not know the value. `_inches_to_cm(0)` used to return `0.0` and
`_pounds_to_grams(0)` used to return `0`, and that got stored verbatim. The web hides it
(a card showing "0 cm" reads as a gap), but the MODEL does not: `reach_cm_diff` is the
2nd of the 20 FEATURE_COLUMNS and `features/metrics.py::diff()` only nulls on None/NaN,
so a 0.0 enters as if the fighter had zero reach and injects a ~180 cm difference that
never happened.

And it sticks: `update_fighter_enrichment` writes `height_cm = COALESCE(height_cm, %s)`,
so a stored 0.0 is never overwritten by a later good value. It had to be cleaned by hand
on 2026-08-22 (33 reach_cm and 2 height_cm set to NULL), after it reached three bouts of
a live card.

Both modules carry literal copies of the same two converters, so all four are tested:
fixing one and forgetting the other is exactly how this comes back.
`consolidate_fighters` imports the pair from `espn_import_all`.

Offline: these are pure functions fed the value ESPN would send.
"""

import pytest

from src.scrapers.espn import _inches_to_cm as espn_inches
from src.scrapers.espn import _pounds_to_grams as espn_pounds
from src.scrapers.espn_import_all import _inches_to_cm as import_all_inches
from src.scrapers.espn_import_all import _pounds_to_grams as import_all_pounds

LENGTH_CONVERTERS = [
    pytest.param(espn_inches, id="espn._inches_to_cm"),
    pytest.param(import_all_inches, id="espn_import_all._inches_to_cm"),
]

WEIGHT_CONVERTERS = [
    pytest.param(espn_pounds, id="espn._pounds_to_grams"),
    pytest.param(import_all_pounds, id="espn_import_all._pounds_to_grams"),
]

ALL_CONVERTERS = LENGTH_CONVERTERS + WEIGHT_CONVERTERS

# The ways ESPN says "I don't know": absent, empty string and ZERO. Zero was the one
# slipping through. Negatives are nonsense from any source and must not survive either.
HOLES = [None, "", 0, 0.0, "0", "0.0", -1, -12.5, "-3"]


@pytest.mark.parametrize("convert", ALL_CONVERTERS)
@pytest.mark.parametrize("hole", HOLES)
def test_a_hole_from_espn_never_becomes_a_number(convert, hole):
    assert convert(hole) is None, (
        f"{convert.__module__}.{convert.__name__}({hole!r}) returned a number. "
        "A 0 from ESPN means 'no data' and enters FEATURE_COLUMNS as if it were real."
    )


@pytest.mark.parametrize("convert", LENGTH_CONVERTERS)
def test_real_measurements_still_convert(convert):
    """The guard must not swallow good data: 78 in is Anthony Wint's actual reach."""
    assert convert(78) == pytest.approx(198.12)
    assert convert("70.5") == pytest.approx(179.07)


@pytest.mark.parametrize("convert", WEIGHT_CONVERTERS)
def test_real_weights_still_convert(convert):
    """265 lb is the heavyweight limit; 115 lb is the strawweight limit."""
    assert convert(265) == 120202
    assert convert("115") == 52163


def test_both_copies_behave_identically():
    """The two modules hold duplicated source. If they ever drift, this fails."""
    for value in HOLES + [78, "70.5", 145, 265]:
        assert espn_inches(value) == import_all_inches(value)
        assert espn_pounds(value) == import_all_pounds(value)
