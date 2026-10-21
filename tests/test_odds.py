import pytest

from src.scrapers.matching import fold
from src.scrapers.odds import NAME_ALIASES, FightRow, best_match, consensus_prices


def _event(prices_by_book):
    """Build a The Odds API-shaped event from {book: {fighter: price}}."""
    return {
        "bookmakers": [
            {
                "key": book,
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": name, "price": price}
                            for name, price in fighters.items()
                        ],
                    }
                ],
            }
            for book, fighters in prices_by_book.items()
        ]
    }


def test_consensus_prices_averages_decimal_odds_across_bookmakers():
    event = _event(
        {
            "draftkings": {"Jon Jones": 1.5, "Stipe Miocic": 2.6},
            "fanduel": {"Jon Jones": 1.6, "Stipe Miocic": 2.4},
        }
    )
    prices = consensus_prices(event)
    assert prices["Jon Jones"] == pytest.approx(1.55)
    assert prices["Stipe Miocic"] == pytest.approx(2.5)


def test_best_match_assigns_each_price_to_the_correct_corner():
    prices = {"Jon Jones": 1.55, "Stipe Miocic": 2.5}
    fights = [FightRow(id=10, red_name="Stipe Miocic", blue_name="Jon Jones")]
    match = best_match(prices, fights)
    assert match is not None
    fight_id, odds_red, odds_blue, score = match
    assert fight_id == 10
    assert odds_red == pytest.approx(2.5)  # red corner is Stipe
    assert odds_blue == pytest.approx(1.55)  # blue corner is Jon
    assert score > 0.9


def test_best_match_folds_accents_and_rejects_non_matches():
    prices = {"Jiří Procházka": 1.8, "Alex Pereira": 2.0}
    fights = [FightRow(id=5, red_name="Jiri Prochazka", blue_name="Alex Pereira")]
    assert best_match(prices, fights)[0] == 5  # accents folded -> matches

    no_match = best_match({"Random Guy": 1.5, "Nobody Here": 2.0}, fights)
    assert no_match is None


# Real bouts of event 1089 (17-oct-2026) that The Odds API spells differently
# from our fighters table. Cuamba: "Timothy" vs our "Timmy" (fold_ratio 0.846
# < 0.85). Cerminara: the API uses her maiden name, Chookagian (0.606).
# (fight_id, (our red, price), (our blue, price), the API prices in its order)
_ALIAS_BOUTS = {
    "cuamba": (
        15560,
        ("Louis Jourdain", 1.553),
        ("Timmy Cuamba", 2.487),
        {"Louis Jourdain": 1.553, "Timothy Cuamba": 2.487},
    ),
    "cerminara": (
        19431,
        ("Katlyn Cerminara", 1.357),
        ("Mackenzie Stiller", 3.238),
        {"Mackenzie Stiller": 3.238, "Katlyn Chookagian": 1.357},
    ),
    # Event 1093 (UFC 333, 24-oct-2026), names read from The Odds API's free
    # /events list on 21-oct: "Ramazonbek" (0.848) and "Abusupyian" (0.824).
    # Made-up prices: only the corner they land on matters here.
    "temirov": (
        15988,
        ("Lone'er Kavanagh", 1.45),
        ("Ramazan Temirov", 2.80),
        {"Lone'er Kavanagh": 1.45, "Ramazonbek Temirov": 2.80},
    ),
    "magomedov": (
        15991,
        ("Abus Magomedov", 1.65),
        ("Cam Rowston", 2.30),
        {"Abusupyian Magomedov": 1.65, "Cam Rowston": 2.30},
    ),
}


@pytest.mark.parametrize("swap_corners", [False, True], ids=["as-stored", "swapped"])
@pytest.mark.parametrize("case", list(_ALIAS_BOUTS))
def test_best_match_puts_aliased_names_on_the_right_corner(case, swap_corners):
    fight_id, red, blue, prices = _ALIAS_BOUTS[case]
    if swap_corners:
        red, blue = blue, red
    fights = [FightRow(id=fight_id, red_name=red[0], blue_name=blue[0])]
    match = best_match(prices, fights)
    assert match is not None
    matched_id, odds_red, odds_blue, _score = match
    assert matched_id == fight_id
    # Each price lands on its own fighter's corner, whatever the API's order.
    assert odds_red == pytest.approx(red[1])
    assert odds_blue == pytest.approx(blue[1])


def test_an_alias_never_hides_the_literal_name():
    # If the fighters table ever stores the API's own spelling, the literal name
    # must still match: the alias is tried IN ADDITION to it, never instead
    # ("Timmy Cuamba" against "Timothy Cuamba" is 0.846, below the threshold).
    fights = [FightRow(id=15560, red_name="Louis Jourdain", blue_name="Timothy Cuamba")]
    match = best_match({"Louis Jourdain": 1.553, "Timothy Cuamba": 2.487}, fights)
    assert match is not None
    assert match[0] == 15560
    assert match[1:3] == pytest.approx((1.553, 2.487))


def test_alias_keys_are_folded():
    # The lookup is NAME_ALIASES.get(fold(api_name)): a key with capitals or
    # accents would never be hit, silently.
    assert all(key == fold(key) for key in NAME_ALIASES)


def test_alias_lookup_folds_the_api_name():
    # The API name goes through fold() too, accents and case included.
    fights = [
        FightRow(id=19431, red_name="Katlyn Cerminara", blue_name="Mackenzie Stiller")
    ]
    match = best_match({"KATLYN Chookagián": 1.357, "Mackenzie Stiller": 3.238}, fights)
    assert match is not None
    assert match[0] == 19431
    assert match[1:3] == pytest.approx((1.357, 3.238))


def test_an_alias_never_matches_a_bout_on_one_name_alone():
    fights = [
        FightRow(id=15560, red_name="Louis Jourdain", blue_name="Timmy Cuamba"),
        FightRow(id=19431, red_name="Katlyn Cerminara", blue_name="Mackenzie Stiller"),
    ]
    # The alias is live: with the right opponent the bout matches...
    match = best_match({"Louis Jourdain": 1.55, "Timothy Cuamba": 2.49}, fights)
    assert match is not None and match[0] == 15560
    # ...but with an opponent who is not in that bout, nothing matches: neither
    # a stranger nor a fighter of the same card who is in another bout.
    for prices in (
        {"Timothy Cuamba": 2.49, "Random Guy": 1.55},
        {"Katlyn Chookagian": 1.36, "Nobody Here": 3.24},
        {"Timothy Cuamba": 2.49, "Mackenzie Stiller": 3.24},
    ):
        assert best_match(prices, fights) is None, prices
