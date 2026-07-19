"""Name-matching of backfill_results onto ufcstats event/fight pages.

Real-world case that motivated these tests (UFC Fight Night: Du Plessis vs.
Usman, 2026-07-18, bout 12859): ufc.com names the fighter "Jose Miguel
Delgado" but ufcstats lists him as "Jose Delgado" — the exact folded-name
match never fired, so the bout stayed unconsolidated forever (referee NULL,
0 fight_stats) while every retry pass logged "no ufcstats fight for". The
fix is a token-subset fallback (NOT fuzzy: difflib ratio on short names
scores a dropped middle name at ~0.77, under every threshold in play):
one name's folded tokens contained in the other's, needing >=2 shared
tokens and a UNIQUE candidate, or the bout/fighter stays unmatched.

Pure helpers only; no network, no DB.
"""

from src.scrapers.backfill_results import _Bout, _match_fight, _winner_id_for
from src.scrapers.matching import token_subset_match
from src.scrapers.parsers.fights import FightPageRecord


def _fight(red_name: str, blue_name: str, source_id: str = "f1") -> FightPageRecord:
    return FightPageRecord(
        red_name=red_name,
        blue_name=blue_name,
        red_source_id=None,
        blue_source_id=None,
        weight_class="Featherweight",
        scheduled_rounds=3,
        winner_corner=None,
        method="U-DEC",
        end_round=3,
        end_time="5:00",
        detail_url=f"http://ufcstats.com/fight-details/{source_id}",
        source_id=source_id,
        is_title_fight=False,
    )


def _bout(red_name: str, blue_name: str, red_id: int = 1, blue_id: int = 2) -> _Bout:
    return _Bout(10, red_id, blue_id, red_name, blue_name)


# ------------------------------------------------------------ token_subset_match


def test_token_subset_match_dropped_middle_name():
    assert token_subset_match("Jose Delgado", "Jose Miguel Delgado")
    assert token_subset_match("Jose Miguel Delgado", "Jose Delgado")


def test_token_subset_match_folds_accents_and_case():
    assert token_subset_match("JOSÉ delgado", "Jose Miguel Delgado")


def test_token_subset_match_rejects_single_shared_token():
    # A lone surname must never claim a fighter.
    assert not token_subset_match("Delgado", "Jose Miguel Delgado")


def test_token_subset_match_rejects_disjoint_and_overlap_only():
    assert not token_subset_match("Jose Delgado", "Jose Martinez")
    # Shares 2 tokens but neither side contains the other.
    assert not token_subset_match("Jose Miguel Delgado", "Jose Delgado Martinez")


def test_token_subset_match_rejects_spelling_variants():
    # Subset is exact on tokens: typos stay for the fuzzy tiers, not this one.
    assert not token_subset_match("Brunno Silva", "Bruno Silva")


def test_token_subset_match_rejects_compound_surnames_and_initials():
    # fold() splits "." and "-" and particles are not identity: none of these
    # carry two SIGNIFICANT tokens, so a bare surname never claims a fighter
    # (hallazgo de la revisión adversarial del 19-jul).
    assert not token_subset_match("Da Silva", "Ariane da Silva")
    assert not token_subset_match("dos Anjos", "Rafael dos Anjos")
    assert not token_subset_match("St-Pierre", "Georges St-Pierre")
    assert not token_subset_match("T.J.", "T.J. Dillashaw")


# ------------------------------------------------------------ _Bout.fighter_id_for


def test_fighter_id_for_exact_still_wins():
    bout = _bout("Austin Bashi", "Jose Miguel Delgado")
    assert bout.fighter_id_for("Austin Bashi") == 1
    assert bout.fighter_id_for("Jose Miguel Delgado") == 2


def test_fighter_id_for_dropped_middle_name():
    bout = _bout("Austin Bashi", "Jose Miguel Delgado")
    assert bout.fighter_id_for("Jose Delgado") == 2


def test_fighter_id_for_ambiguous_subset_returns_none():
    bout = _bout("Jose Miguel Delgado", "Jose Angel Delgado")
    assert bout.fighter_id_for("Jose Delgado") is None


def test_fighter_id_for_unrelated_returns_none():
    bout = _bout("Austin Bashi", "Jose Miguel Delgado")
    assert bout.fighter_id_for("Herb Dean") is None


def test_winner_id_for_dropped_middle_name():
    bout = _bout("Austin Bashi", "Jose Miguel Delgado")
    assert _winner_id_for(bout, "Jose Delgado") == 2
    assert _winner_id_for(bout, None) is None


# ------------------------------------------------------------ _match_fight


def test_match_fight_exact_key():
    bout = _bout("Austin Bashi", "Jose Miguel Delgado")
    fights = [_fight("Jose Miguel Delgado", "Austin Bashi")]
    assert _match_fight(bout, fights) is fights[0]


def test_match_fight_dropped_middle_name_corner_swapped():
    # ufcstats lists the winner first regardless of our red/blue corners.
    bout = _bout("Austin Bashi", "Jose Miguel Delgado")
    fights = [
        _fight("Steve Garcia", "David Onama", source_id="other"),
        _fight("Jose Delgado", "Austin Bashi", source_id="target"),
    ]
    assert _match_fight(bout, fights) is fights[1]


def test_match_fight_requires_both_corners():
    bout = _bout("Austin Bashi", "Jose Miguel Delgado")
    fights = [_fight("Jose Delgado", "Someone Else")]
    assert _match_fight(bout, fights) is None


def test_match_fight_ambiguous_candidates_return_none():
    # DB stores the SHORT name; two page fights both contain it -> no guess.
    bout = _bout("Austin Bashi", "Jose Delgado")
    fights = [
        _fight("Jose Miguel Delgado", "Austin Bashi", source_id="a"),
        _fight("Austin Bashi", "Jose Angel Delgado", source_id="b"),
    ]
    assert _match_fight(bout, fights) is None


def test_match_fight_same_fighter_cannot_cover_both_corners():
    # Both DB corners subset-match the SAME ufcstats fighter -> not a match.
    bout = _bout("Jose Miguel Delgado", "Jose Angel Delgado")
    fights = [_fight("Jose Delgado", "Somebody Unrelated")]
    assert _match_fight(bout, fights) is None


def test_match_fight_with_unlinked_corner_id():
    # A corner whose fighter went unlinked at import (id NULL, designed state
    # for debutants) must still match by NAME: the bout can then consolidate
    # result/referee even though per-fighter stats need a real id.
    bout = _bout("Austin Bashi", "Jose Miguel Delgado", red_id=1, blue_id=None)
    fights = [_fight("Jose Delgado", "Austin Bashi")]
    assert _match_fight(bout, fights) is fights[0]
    # The unlinked corner resolves by name but yields no id for stat rows...
    assert bout.corner_for("Jose Delgado") == "blue"
    assert bout.fighter_id_for("Jose Delgado") is None
    assert _winner_id_for(bout, "Jose Delgado") is None
    # ...while the linked corner keeps working normally.
    assert bout.fighter_id_for("Austin Bashi") == 1
