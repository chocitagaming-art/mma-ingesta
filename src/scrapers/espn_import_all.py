from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from difflib import get_close_matches
from typing import Any

import requests

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, normalize_name as _normalize_name, ratio
from .models import FighterRecord
from .repositories.fighters import FighterMatchRecord, get_all_fighters, upsert_fighter


LOGGER = logging.getLogger(__name__)
ESPN_SOURCE = "espn"
ESPN_ATHLETES_URL = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/athletes"
ESPN_REQUEST_DELAY_SECONDS = 0.3
ESPN_PAGE_SIZE = 100
# Identity matching: a false positive welds the wrong fighter's data, so keep the
# strict cutoff. See src/scrapers/matching.py for the threshold policy.
FUZZY_MATCH_THRESHOLD = IDENTITY_THRESHOLD


@dataclass(frozen=True)
class EspnAthlete:
    athlete_id: str
    full_name: str
    nickname: str | None
    nationality: str | None
    birth_date: date | None
    height_cm: float | None
    reach_cm: float | None
    weight_grams: int | None
    stance: str | None
    headshot_url: str | None


def import_all_athletes(max_pages: int | None = None) -> Counter:
    settings = get_settings()
    counts: Counter = Counter()
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": settings.user_agent.replace("ufcstats.com", "espn.com"),
        }
    )
    with connect(settings.database_url) as connection:
        fighters = get_all_fighters(connection)
    exact_name_index = _build_exact_name_index(fighters)
    normalized_name_index = _build_normalized_name_index(fighters)
    total_pages = _get_total_pages(session)
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)
    LOGGER.info("Importing ESPN UFC athletes across %s pages", total_pages)
    for page_number in range(1, total_pages + 1):
        athlete_refs = _fetch_athlete_refs(session, page_number)
        counts["pages"] += 1
        counts["athlete_refs"] += len(athlete_refs)
        for athlete_ref in athlete_refs:
            try:
                athlete = _fetch_athlete(session, athlete_ref)
                counts["athletes_fetched"] += 1
                matched_fighter = _match_fighter(athlete.full_name, exact_name_index, normalized_name_index)
                if matched_fighter is None:
                    with connect(settings.database_url) as connection:
                        fighter_id = upsert_fighter(connection, _to_fighter_record(athlete))
                        connection.commit()
                    counts["inserted"] += 1
                    _index_fighter(
                        FighterMatchRecord(
                            id=fighter_id,
                            name=athlete.full_name,
                            nickname=athlete.nickname,
                            nationality=athlete.nationality,
                            birth_date=athlete.birth_date,
                            height_cm=athlete.height_cm,
                            reach_cm=athlete.reach_cm,
                            weight_grams=athlete.weight_grams,
                            stance=athlete.stance,
                        ),
                        exact_name_index,
                        normalized_name_index,
                    )
                else:
                    counts["already_exists"] += 1
                time.sleep(ESPN_REQUEST_DELAY_SECONDS)
            except Exception as exc:
                counts["errors"] += 1
                LOGGER.exception("Failed to import ESPN athlete ref %s: %s", athlete_ref, exc)
    return counts


def _get_total_pages(session: requests.Session) -> int:
    payload = _get_json(session, ESPN_ATHLETES_URL, {"limit": ESPN_PAGE_SIZE, "page": 1})
    total_count = int(payload["count"])
    return (total_count + ESPN_PAGE_SIZE - 1) // ESPN_PAGE_SIZE


def _fetch_athlete_refs(session: requests.Session, page_number: int) -> list[str]:
    payload = _get_json(session, ESPN_ATHLETES_URL, {"limit": ESPN_PAGE_SIZE, "page": page_number})
    return [item["$ref"] for item in payload.get("items", []) if item.get("$ref")]


def _fetch_athlete(session: requests.Session, athlete_ref: str) -> EspnAthlete:
    payload = _get_json(session, athlete_ref)
    return EspnAthlete(
        athlete_id=str(payload["id"]),
        full_name=(payload.get("fullName") or payload.get("displayName") or "").strip(),
        nickname=_clean_text(payload.get("nickname")),
        nationality=_clean_text(payload.get("citizenship")),
        birth_date=_parse_birth_date(payload.get("dateOfBirth")),
        height_cm=_inches_to_cm(payload.get("height")),
        reach_cm=_inches_to_cm(payload.get("reach")),
        weight_grams=_pounds_to_grams(payload.get("weight")),
        stance=_nested_text(payload.get("stance")),
        headshot_url=_extract_headshot_url(payload),
    )


def _get_json(session: requests.Session, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _add_to_index(
    index: dict[str, FighterMatchRecord | None],
    key: str,
    fighter: FighterMatchRecord,
) -> None:
    if not key:
        return
    if key in index:
        existing = index[key]
        if existing is None:
            # Key already burned as ambiguous: do NOT resurrect it, or the
            # homonym would resolve to this writer.
            return
        if existing.id != fighter.id:
            index[key] = None  # burn the homonym key
        return
    index[key] = fighter


def _build_name_index(
    fighters: list[FighterMatchRecord],
    key_func: Callable[[str], str],
) -> dict[str, FighterMatchRecord | None]:
    """Index fighters by ``key_func(name)``, neutralising homonym collisions.

    Two distinct fighters sharing a key (homonyms) would silently overwrite each
    other in a plain dict, welding ESPN data onto whichever was processed last
    (issue #6). The colliding key is tombstoned with ``None`` so it falls through
    to "no match". Mirrors the policy in src/scrapers/espn.py.
    """
    index: dict[str, FighterMatchRecord | None] = {}
    for fighter in fighters:
        if not fighter.name:
            continue
        _add_to_index(index, key_func(fighter.name), fighter)
    return index


def _build_exact_name_index(fighters: list[FighterMatchRecord]) -> dict[str, FighterMatchRecord | None]:
    return _build_name_index(fighters, str.casefold)


def _build_normalized_name_index(fighters: list[FighterMatchRecord]) -> dict[str, FighterMatchRecord | None]:
    return _build_name_index(fighters, _normalize_name)


def _index_fighter(
    fighter: FighterMatchRecord,
    exact_name_index: dict[str, FighterMatchRecord | None],
    normalized_name_index: dict[str, FighterMatchRecord | None],
) -> None:
    _add_to_index(exact_name_index, fighter.name.casefold(), fighter)
    _add_to_index(normalized_name_index, _normalize_name(fighter.name), fighter)


def _match_fighter(
    full_name: str,
    exact_name_index: dict[str, FighterMatchRecord | None],
    normalized_name_index: dict[str, FighterMatchRecord | None],
) -> FighterMatchRecord | None:
    exact_match = exact_name_index.get(full_name.casefold())
    if exact_match is not None:
        return exact_match
    normalized_name = _normalize_name(full_name)
    normalized_match = normalized_name_index.get(normalized_name)
    if normalized_match is not None:
        return normalized_match
    live_keys = [key for key, value in normalized_name_index.items() if value is not None]
    candidates = get_close_matches(normalized_name, live_keys, n=1, cutoff=FUZZY_MATCH_THRESHOLD)
    if not candidates:
        return None
    candidate_name = candidates[0]
    similarity = ratio(normalized_name, candidate_name)
    if similarity < FUZZY_MATCH_THRESHOLD:
        return None
    return normalized_name_index[candidate_name]


def _parse_birth_date(value: str | None) -> date | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).date()


def _inches_to_cm(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return round(float(value) * 2.54, 2)


def _pounds_to_grams(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(round(float(value) * 453.592))


def _nested_text(value: Any) -> str | None:
    if isinstance(value, dict):
        return _clean_text(value.get("text"))
    return None


def _extract_headshot_url(payload: dict[str, Any]) -> str | None:
    headshot = payload.get("headshot")
    if isinstance(headshot, dict):
        return _clean_text(headshot.get("href"))
    images = payload.get("images")
    if isinstance(images, list) and images:
        first_image = images[0]
        if isinstance(first_image, dict):
            return _clean_text(first_image.get("href"))
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_fighter_record(athlete: EspnAthlete) -> FighterRecord:
    return FighterRecord(
        name=athlete.full_name,
        nickname=athlete.nickname,
        headshot_url=athlete.headshot_url,
        nationality=athlete.nationality,
        birth_date=athlete.birth_date,
        height_cm=athlete.height_cm,
        reach_cm=athlete.reach_cm,
        stance=athlete.stance,
        weight_grams=athlete.weight_grams,
        wins=0,
        losses=0,
        draws=0,
        source=ESPN_SOURCE,
        source_id=athlete.athlete_id,
    )


def _build_summary(counts: Counter) -> str:
    return json.dumps(
        {
            "pages": counts["pages"],
            "athlete_refs": counts["athlete_refs"],
            "athletes_fetched": counts["athletes_fetched"],
            "inserted": counts["inserted"],
            "already_exists": counts["already_exists"],
            "errors": counts["errors"],
        },
        indent=2,
        sort_keys=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import all ESPN UFC athletes into fighters.")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit ESPN pagination for testing.")
    args = parser.parse_args()
    configure_logging()
    counts = import_all_athletes(max_pages=args.max_pages)
    print(_build_summary(counts))


if __name__ == "__main__":
    main()