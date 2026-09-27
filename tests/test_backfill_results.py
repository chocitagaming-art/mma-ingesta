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
from src.scrapers.matching import given_name_diminutive_match, token_subset_match
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


# ------------------------------------------------- given_name_diminutive_match


def test_diminutive_match_zach_zachary():
    # Caso real: la pelea 12850 del UFC 329 llevaba 14 dias sin arbitro ni
    # stats porque nuestra ficha dice "Zachary Reese" y ufcstats "Zach Reese".
    assert given_name_diminutive_match("Zach Reese", "Zachary Reese")
    assert given_name_diminutive_match("Zachary Reese", "Zach Reese")


def test_diminutive_match_requires_identical_surnames():
    # LA GUARDA QUE JUSTIFICA TODA LA FUNCION. Los diminutivos ocurren en el
    # nombre de PILA; un prefijo libre casaria apellidos de personas distintas.
    assert not given_name_diminutive_match("Marcos Silva", "Marcos Silveira")
    assert not given_name_diminutive_match("Bruno Santos", "Bruno Santana")


def test_diminutive_match_rejects_short_prefixes():
    # Suelo de 4 caracteres: por debajo, un nombre completo y corto seria
    # prefijo de otro distinto. Perder un match es barato; inventarlo no.
    assert not given_name_diminutive_match("Jon Jones", "Jonathan Jones")
    assert not given_name_diminutive_match("Ali Bagov", "Alistair Bagov")


def test_diminutive_match_rejects_different_given_names():
    assert not given_name_diminutive_match("Zachary Reese", "Marcus Reese")
    # Comparte apellido y nada mas: un apellido solo nunca reclama a nadie.
    assert not given_name_diminutive_match("Reese", "Zachary Reese")


def test_diminutive_match_rejects_different_token_counts():
    # No es la funcion del nombre intermedio: de eso ya se ocupa
    # token_subset_match, y mezclar las dos tolerancias multiplica el riesgo.
    assert not given_name_diminutive_match("Zach Reese", "Zachary Miguel Reese")


def test_diminutive_match_folds_accents_and_case():
    assert given_name_diminutive_match("ZACH reese", "Zachary Reese")


def test_corner_for_maps_diminutive_to_its_corner():
    # El tier nuevo llega hasta corner_for, que es lo que usa el consolidador.
    bout = _bout("Ryan Gandra", "Zachary Reese")
    assert bout.corner_for("Zach Reese") == "blue"
    assert bout.corner_for("Ryan Gandra") == "red"


def test_corner_for_refuses_ambiguous_diminutive():
    # "Zach" es prefijo de las DOS esquinas y no coincide exacto con ninguna:
    # el nivel de diminutivo no puede elegir, y no elegir es la respuesta.
    bout = _bout("Zachary Reese", "Zacharias Reese")
    assert bout.corner_for("Zach Reese") is None


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


# ------------------------------------- alternative (ufc.com) name per corner
#
# Bouts 16351/16352 (event 1091, 26-sep-2026): once a corner is linked,
# _get_bouts names it after fighters.name (ESPN), but ufcstats writes the
# ufc.com spelling that the fight row keeps in fighter_*_name. Each corner
# must answer to both, or the bout is "no ufcstats fight" / its stats go
# "unmatched" forever.


def _linked(red_name, blue_name, red_alt, blue_alt, red_id=9130, blue_id=9129,
            red_nickname=None, blue_nickname=None) -> _Bout:
    return _Bout(
        16352, red_id, blue_id, red_name, blue_name,
        red_alt_name=red_alt, blue_alt_name=blue_alt,
        red_nickname=red_nickname, blue_nickname=blue_nickname,
    )


def test_corner_for_matches_the_ufc_com_spelling_of_a_linked_corner():
    bout = _linked("Mehemmedeli Osmanli", "Ilimbek Akylbek Uulu",
                   "Mahammadali Osmanli", "Ilimbek Akylbek")
    assert bout.corner_for("Mahammadali Osmanli") == "red"
    assert bout.corner_for("Ilimbek Akylbek") == "blue"
    # Stats rows resolve to the linked ids, which is what fills fight_stats.
    assert bout.fighter_id_for("Mahammadali Osmanli") == 9130
    assert bout.fighter_id_for("Ilimbek Akylbek") == 9129
    # The ESPN names keep working too.
    assert bout.corner_for("Mehemmedeli Osmanli") == "red"


def test_match_fight_by_the_ring_name_stored_on_the_fight_row():
    # 16351: fighter 9132 is "Valesca Machado"; ufc.com and ufcstats say
    # "Tina Black". Before: "no ufcstats fight for Melissa Amaya vs Valesca Machado".
    bout = _linked("Melissa Amaya", "Valesca Machado", "Melissa Amaya", "Tina Black",
                   red_id=9131, blue_id=9132, blue_nickname="Tina Black")
    fights = [
        _fight("Raul Rosas Jr.", "Raoni Barcelos", source_id="other"),
        _fight("Tina Black", "Melissa Amaya", source_id="target"),
    ]
    assert _match_fight(bout, fights) is fights[1]
    assert _winner_id_for(bout, "Tina Black") == 9132


def test_match_fight_linked_transliteration_both_corners():
    # 16352 once relinked: both corners differ from ufcstats; only the alt
    # names pair the bout (exact key over the alternatives).
    bout = _linked("Mehemmedeli Osmanli", "Ilimbek Akylbek Uulu",
                   "Mahammadali Osmanli", "Ilimbek Akylbek")
    fights = [_fight("Ilimbek Akylbek", "Mahammadali Osmanli")]
    assert _match_fight(bout, fights) is fights[0]


def test_alt_name_never_makes_a_name_claim_both_corners():
    # A stale alt name equal to the OTHER corner's name is dropped, so the
    # page fighter maps to his real corner and never to both.
    bout = _linked("Jose Aldo", "Max Holloway", "Max Holloway", None, red_id=1, blue_id=2)
    assert bout.corner_for("Max Holloway") == "blue"
    assert bout.corner_for("Jose Aldo") == "red"


def test_without_alt_names_matching_is_unchanged():
    # ufcstats-sourced bouts carry no fighter_*_name: same answers as before.
    bout = _Bout(10, 1, 2, "Austin Bashi", "Jose Miguel Delgado")
    assert bout.keys() == [bout.key()]
    assert bout.corner_for("Jose Delgado") == "blue"
    assert bout.corner_for("Tina Black") is None


def test_alt_name_equal_to_primary_adds_no_extra_key():
    bout = _linked("Rodolfo Vieira", "Robert Bryczek", "RODOLFO VIEIRA", "Robert Bryczek",
                   red_id=6269, blue_id=7265)
    assert bout.keys() == [bout.key()]


def test_stale_alt_name_equal_to_the_other_corner_is_ignored():
    # Corners relisted the other way round: red's alt is blue's name. It must
    # not become an alias of red, or blue's stats would land on red's id.
    bout = _linked("Mehemmedeli Osmanli", "Ilimbek Akylbek Uulu",
                   "Ilimbek Akylbek Uulu", "Mehemmedeli Osmanli")
    assert bout.corner_for("Ilimbek Akylbek Uulu") == "blue"
    assert bout.corner_for("Mehemmedeli Osmanli") == "red"


def test_alt_name_that_does_not_look_like_the_linked_fighter_is_ignored():
    # A wrong link (scoreboard link made while ESPN still listed the withdrawn
    # Michael Chiesa, id 30, for the slot of debutant substitute Michael
    # Johnson): the ufc.com name must not become an alias of fighter 30, or
    # Johnson's win and stats would be written onto Chiesa.
    bout = _linked("Kyle Nelson", "Michael Chiesa", None, "Michael Johnson",
                   red_id=6434, blue_id=30, blue_nickname="Maverick")
    assert bout.corner_for("Michael Johnson") is None
    assert bout.fighter_id_for("Michael Johnson") is None
    fights = [_fight("Michael Johnson", "Kyle Nelson")]
    assert _match_fight(bout, fights) is None


def test_ring_name_without_the_nickname_is_not_an_alias():
    # "Tina Black" only counts as Valesca Machado because it IS her nickname.
    bout = _linked("Melissa Amaya", "Valesca Machado", None, "Tina Black",
                   red_id=9131, blue_id=9132)
    assert bout.corner_for("Tina Black") is None


def test_no_alias_for_a_fighter_imported_from_ufcstats():
    # Two different Silvas pass any name filter. A withdrawn veteran (imported
    # from ufcstats, so ufcstats already knows him by fighters.name) must never
    # answer to the substitute's ufc.com name: that would only mean a wrong link.
    bout = _Bout(
        17000, 7001, 7002, "Natalia Silva", "Someone Else",
        red_source_id="/fighter-details/aaaaaaaaaaaaaaaa", blue_source_id="5000000",
        red_alt_name="Jessica Silva", blue_alt_name=None,
    )
    assert bout.corner_for("Jessica Silva") is None
    assert bout.corner_for("Natalia Silva") == "red"


def test_alias_still_works_for_espn_imported_debutants():
    bout = _Bout(
        16352, 9130, 9129, "Mehemmedeli Osmanli", "Ilimbek Akylbek Uulu",
        red_source_id="5345640", blue_source_id="5345639",
        red_alt_name="Mahammadali Osmanli", blue_alt_name="Ilimbek Akylbek",
    )
    assert bout.fighter_id_for("Mahammadali Osmanli") == 9130
    assert bout.fighter_id_for("Ilimbek Akylbek") == 9129
