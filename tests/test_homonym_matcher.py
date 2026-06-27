"""The ESPN name matcher must not silently collapse homonyms (issue #6).

Two distinct fighters sharing a name used to overwrite each other in a dict
indexed by name, so enrichment got welded onto whichever was processed last.
The index now marks ambiguous keys with a tombstone (None) instead.
"""

from src.scrapers.espn import (
    _build_exact_name_index,
    _build_normalized_name_index,
    _index_fighter,
    _match_fighter,
)
from src.scrapers.repositories.fighters import FighterMatchRecord


def _fighter(fighter_id: int, name: str) -> FighterMatchRecord:
    return FighterMatchRecord(
        id=fighter_id,
        name=name,
        nickname=None,
        nationality=None,
        birth_date=None,
        height_cm=None,
        reach_cm=None,
        weight_grams=None,
        stance=None,
    )


def test_exact_index_drops_homonym_key():
    fighters = [_fighter(1, "John Smith"), _fighter(2, "John Smith"), _fighter(3, "Jane Doe")]
    index = _build_exact_name_index(fighters)
    # ambiguous -> tombstoned (None), not last-write-wins; never resolves to a fighter
    assert index.get("john smith") is None
    assert index["jane doe"].id == 3


def test_normalized_index_drops_homonym_key():
    fighters = [_fighter(1, "John Smith"), _fighter(2, "John Smith"), _fighter(3, "Jane Doe")]
    index = _build_normalized_name_index(fighters)
    assert index.get("john smith") is None
    assert index["jane doe"].id == 3


def test_match_fighter_returns_none_for_homonym():
    fighters = [_fighter(1, "John Smith"), _fighter(2, "John Smith")]
    exact = _build_exact_name_index(fighters)
    normalized = _build_normalized_name_index(fighters)
    assert _match_fighter("John Smith", exact, normalized) is None


def test_match_fighter_still_resolves_unique_name():
    fighters = [_fighter(1, "John Smith"), _fighter(2, "John Smith"), _fighter(3, "Jane Doe")]
    exact = _build_exact_name_index(fighters)
    normalized = _build_normalized_name_index(fighters)
    match = _match_fighter("Jane Doe", exact, normalized)
    assert match is not None and match.id == 3


def test_index_fighter_does_not_overwrite_into_a_homonym():
    fighters = [_fighter(1, "Jane Doe")]
    exact = _build_exact_name_index(fighters)
    normalized = _build_normalized_name_index(fighters)
    # A freshly inserted second "Jane Doe" must not weld onto fighter 1.
    _index_fighter(_fighter(2, "Jane Doe"), exact, normalized)
    assert _match_fighter("Jane Doe", exact, normalized) is None


def test_live_insert_does_not_resurrect_a_burned_homonym_key():
    # El índice estático ya quemó "John Smith" (dos homónimos). Una inserción en
    # vivo de un TERCER "John Smith" no debe resucitar la clave (regresión #6).
    fighters = [_fighter(1, "John Smith"), _fighter(2, "John Smith")]
    exact = _build_exact_name_index(fighters)
    normalized = _build_normalized_name_index(fighters)
    assert _match_fighter("John Smith", exact, normalized) is None
    _index_fighter(_fighter(3, "John Smith"), exact, normalized)
    assert _match_fighter("John Smith", exact, normalized) is None
