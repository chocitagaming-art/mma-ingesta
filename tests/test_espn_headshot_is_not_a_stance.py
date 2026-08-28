"""A stance shot is NOT a headshot: `images[]` must never fill the face column.

THE BUG THIS PINS DOWN, and it reached a live card. When ESPN has no `headshot`
for an athlete yet, both extractors used to fall back to `images[0]`. For MMA
that array holds ONLY standing full-body poses — tagged rel "leftStance" /
"rightStance" and served from `/players/stance/{left,right}/` — so the fallback
never found "another portrait": it stored a full-length figure in the column the
site renders inside a 48px round frame.

Measured on 2026-08-28 against the live API for six athletes (4569549, 5401062,
5080560, 3151289, 4275487 and 4358252): every `images[]` entry was a stance,
none was ever a portrait. The two files differ in size by a factor of five —
`players/full/5401062.png` is 600x436 (a portrait, ratio 0.73) while
`players/stance/right/5401062.png` is 623x1818 (ratio 2.92).

It happened to Hector Santiago (fighters.id 9123) on 2026-08-27, the day before
the 29-ago card: ESPN had not published his headshot, the fallback grabbed his
rightStance, and his bout page rendered a warped portrait next to a full-body
opponent. ESPN published the real headshot hours later and it changed nothing:
every enrichment pass fills gaps with COALESCE, and the column was no longer
empty. It had to be corrected by hand.

WHY ALL THREE MODULES ARE TESTED. `espn.py` and `espn_import_all.py` each carried
a literal copy of the extractor — the same duplication that let the 0-from-ESPN
bug survive in one file after being fixed in the other (see
test_espn_converters_reject_zero.py). The guard now lives ONLY in `espn.py` and
`espn_import_all` imports it, but the two entry points are still exercised
separately, plus `consolidate_fighters`, which re-exports the bulk importer's
version and would silently re-break thousands of rows rather than one.

Offline: pure functions fed the exact payload shape ESPN serves.
"""

import pytest

from src.scrapers.consolidate_fighters import _extract_headshot_url as consolidate_extract
from src.scrapers.espn import _extract_headshot_url as espn_extract
from src.scrapers.espn import _is_stance_image
from src.scrapers.espn_import_all import _extract_headshot_url as import_all_extract

EXTRACTORS = [
    pytest.param(espn_extract, id="espn"),
    pytest.param(import_all_extract, id="espn_import_all"),
    pytest.param(consolidate_extract, id="consolidate_fighters"),
]

PORTRAIT = "https://a.espncdn.com/i/headshots/mma/players/full/5401062.png"
RIGHT_STANCE = "https://a.espncdn.com/i/headshots/mma/players/stance/right/5401062.png"
LEFT_STANCE = "https://a.espncdn.com/i/headshots/mma/players/stance/left/4569549.png"


@pytest.mark.parametrize("extract", EXTRACTORS)
def test_a_lone_stance_image_is_not_used_as_a_headshot(extract):
    """The exact Santiago payload of 27-ago: no `headshot`, one rightStance."""
    payload = {"images": [{"href": RIGHT_STANCE, "rel": ["rightStance"]}]}
    assert extract(payload) is None


@pytest.mark.parametrize("extract", EXTRACTORS)
def test_both_stance_directions_are_rejected(extract):
    """Most athletes carry two poses; neither is a face."""
    payload = {
        "images": [
            {"href": LEFT_STANCE, "rel": ["leftStance"]},
            {"href": RIGHT_STANCE, "rel": ["rightStance"]},
        ]
    }
    assert extract(payload) is None


@pytest.mark.parametrize("extract", EXTRACTORS)
def test_the_real_headshot_field_still_wins(extract):
    """The happy path must not regress: `headshot` is the portrait, poses ignored."""
    payload = {
        "headshot": {"href": PORTRAIT, "alt": "Hector Santiago"},
        "images": [{"href": RIGHT_STANCE, "rel": ["rightStance"]}],
    }
    assert extract(payload) == PORTRAIT


@pytest.mark.parametrize("extract", EXTRACTORS)
def test_a_non_stance_image_is_still_an_acceptable_fallback(extract):
    """The fallback is narrowed, NOT removed.

    Nothing in today's MMA payloads populates this branch — it exists so that a
    future portrait-shaped `rel` keeps working instead of being dropped with the
    poses. The stance listed first must not shadow it, which is why the fix
    scans the list rather than reading images[0].
    """
    portrait_in_images = "https://a.espncdn.com/i/headshots/mma/players/full/999.png"
    payload = {
        "images": [
            {"href": RIGHT_STANCE, "rel": ["rightStance"]},
            {"href": portrait_in_images, "rel": ["profile"]},
        ]
    }
    assert extract(payload) == portrait_in_images


@pytest.mark.parametrize("extract", EXTRACTORS)
def test_a_stance_without_rel_is_caught_by_its_path(extract):
    """Belt-and-braces: ESPN omitting `rel` must not reopen the hole."""
    payload = {"images": [{"href": RIGHT_STANCE}]}
    assert extract(payload) is None


@pytest.mark.parametrize("extract", EXTRACTORS)
def test_no_images_at_all_is_still_none(extract):
    assert extract({}) is None
    assert extract({"images": []}) is None


def test_the_guard_reads_rel_and_falls_back_to_the_path():
    """`rel` is the semantic field; the path check only covers a missing rel."""
    assert _is_stance_image({"href": "/whatever.png", "rel": ["rightStance"]}) is True
    assert _is_stance_image({"href": "/whatever.png", "rel": ["leftStance"]}) is True
    assert _is_stance_image({"href": RIGHT_STANCE}) is True
    assert _is_stance_image({"href": PORTRAIT, "rel": ["profile"]}) is False
    assert _is_stance_image({}) is False
