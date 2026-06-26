"""Fetch MMA moneyline (h2h) odds from The Odds API and populate
``fights.odds_red`` / ``fights.odds_blue`` for UPCOMING bouts, so the UI can show
the market favorite + implied probability (#41).

The Odds API gives upcoming MMA events across all promotions with decimal odds per
bookmaker. We average each fighter's price across bookmakers (consensus), match the
two priced fighters to one of our upcoming UFC fights by name (diacritic-insensitive,
either corner orientation), and write the consensus odds onto the right corner.
Only UFC fights in our DB will match; everything else is skipped.

Usage::

    python -m src.scrapers.odds --dry-run   # show matches, write nothing
    python -m src.scrapers.odds             # apply odds to matched fights
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

import requests

from .config import get_settings
from .db import connect, cursor
from .logging_config import configure_logging
from .matching import fold_ratio

LOGGER = logging.getLogger(__name__)

ODDS_API_URL = (
    "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds/"
)
# Both fighters of a bout must match, so a comparatively loose per-name cutoff is
# still safe (two unrelated names rarely both coincide).
MATCH_THRESHOLD = 0.85


@dataclass
class FightRow:
    id: int
    red_name: str
    blue_name: str


def fetch_odds(api_key: str, *, timeout: int = 30) -> list[dict]:
    """Fetch upcoming MMA h2h odds (decimal) from The Odds API."""
    response = requests.get(
        ODDS_API_URL,
        params={
            "apiKey": api_key,
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "decimal",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def consensus_prices(event: dict) -> dict[str, float]:
    """Average decimal price per fighter across every bookmaker's h2h market."""
    totals: dict[str, list[float]] = {}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if name and isinstance(price, (int, float)):
                    totals.setdefault(name, []).append(float(price))
    return {name: sum(values) / len(values) for name, values in totals.items() if values}


def best_match(
    prices: dict[str, float], fights: list[FightRow]
) -> tuple[int, float, float, float] | None:
    """Map the two priced fighters onto an upcoming fight (either corner order).

    Returns ``(fight_id, odds_red, odds_blue, score)`` for the best-matching fight
    at or above :data:`MATCH_THRESHOLD`, or ``None`` if nothing matches.
    """
    names = list(prices.keys())
    if len(names) != 2:
        return None
    first, second = names

    best: tuple[int, float, float, float] | None = None
    for fight in fights:
        # Orientation A: first->red, second->blue. Orientation B: swapped.
        score_a = min(
            fold_ratio(first, fight.red_name), fold_ratio(second, fight.blue_name)
        )
        score_b = min(
            fold_ratio(first, fight.blue_name), fold_ratio(second, fight.red_name)
        )
        if score_a >= score_b:
            score, odds_red, odds_blue = score_a, prices[first], prices[second]
        else:
            score, odds_red, odds_blue = score_b, prices[second], prices[first]
        if best is None or score > best[3]:
            best = (fight.id, odds_red, odds_blue, score)

    if best is not None and best[3] >= MATCH_THRESHOLD:
        return best
    return None


def _upcoming_fights(database_url: str) -> list[FightRow]:
    fights: list[FightRow] = []
    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute(
                """
                select fi.id, r.name as red_name, b.name as blue_name
                from fights fi
                join events e on e.id = fi.event_id
                join fighters r on r.id = fi.fighter_red_id
                join fighters b on b.id = fi.fighter_blue_id
                where e.status = 'upcoming'
                """
            )
            for row in db_cursor.fetchall():
                fights.append(
                    FightRow(
                        id=row["id"],
                        red_name=row["red_name"],
                        blue_name=row["blue_name"],
                    )
                )
    return fights


def _apply_odds(
    database_url: str, updates: list[tuple[int, float, float]]
) -> None:
    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            for fight_id, odds_red, odds_blue in updates:
                db_cursor.execute(
                    "update fights set odds_red = %s, odds_blue = %s where id = %s",
                    (round(odds_red, 3), round(odds_blue, 3), fight_id),
                )
        connection.commit()


def run(*, dry_run: bool = False) -> int:
    settings = get_settings()
    if not settings.odds_api_key:
        raise SystemExit("ODDS_API_KEY is not set in the environment or .env file.")

    events = fetch_odds(settings.odds_api_key)
    fights = _upcoming_fights(settings.database_url)

    updates: list[tuple[int, float, float]] = []
    seen: set[int] = set()
    for event in events:
        prices = consensus_prices(event)
        match = best_match(prices, fights)
        if match is None:
            continue
        fight_id, odds_red, odds_blue, score = match
        if fight_id in seen:
            continue
        seen.add(fight_id)
        updates.append((fight_id, odds_red, odds_blue))
        LOGGER.info(
            "matched fight %s: odds_red=%.2f odds_blue=%.2f (score %.2f)",
            fight_id,
            odds_red,
            odds_blue,
            score,
        )

    print(
        f"{len(events)} odds events, {len(fights)} upcoming fights, "
        f"{len(updates)} matched"
    )
    if dry_run:
        for fight_id, odds_red, odds_blue in updates[:25]:
            print(f"  fight {fight_id}: odds_red={odds_red:.2f} odds_blue={odds_blue:.2f}")
        return len(updates)

    _apply_odds(settings.database_url, updates)
    print(f"applied odds to {len(updates)} fights")
    return len(updates)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show matches without writing to the database",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
