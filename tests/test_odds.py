import pytest

from src.scrapers.odds import FightRow, best_match, consensus_prices


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
