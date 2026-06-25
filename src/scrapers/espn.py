from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from difflib import get_close_matches
from typing import Any
from urllib.parse import urlencode

import requests

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, normalize_name as _normalize_name, ratio
from .models import FighterRecord
from .repositories.fighters import FighterMatchRecord, get_all_fighters, update_fighter_enrichment, upsert_fighter


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


def scrape_and_enrich(max_pages: int | None = None) -> Counter:
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
        LOGGER.info("Fetching ESPN UFC athletes across %s pages", total_pages)
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
                        fighter_id = upsert_fighter(connection, _to_fighter_record(athlete))
                        connection.commit()
                        counts["inserted"] += 1
                        new_match = FighterMatchRecord(
                            id=fighter_id,
                            name=athlete.full_name,
                            nickname=athlete.nickname,
                            nationality=athlete.nationality,
                            birth_date=athlete.birth_date,
                            height_cm=athlete.height_cm,
                            reach_cm=athlete.reach_cm,
                            weight_grams=athlete.weight_grams,
                            stance=athlete.stance,
                        )
                        _index_fighter(new_match, exact_name_index, normalized_name_index)
                    else:
                        counts["matched"] += 1
                        updated = update_fighter_enrichment(
                            connection,
                            matched_fighter.id,
                            nickname=athlete.nickname,
                            headshot_url=athlete.headshot_url,
                            nationality=athlete.nationality,
                            birth_date=athlete.birth_date,
                            height_cm=athlete.height_cm,
                            reach_cm=athlete.reach_cm,
                            weight_grams=athlete.weight_grams,
                            stance=athlete.stance,
                        )
                        connection.commit()
                        if updated:
                            counts["updated"] += 1
                    if _is_missing_core_data(athlete):
                        counts["espn_missing_core_data"] += 1
                    time.sleep(ESPN_REQUEST_DELAY_SECONDS)
                except Exception as exc:
                    connection.rollback()
                    counts["errors"] += 1
                    LOGGER.exception("Failed to process ESPN athlete ref %s: %s", athlete_ref, exc)
        counts["still_missing_data"] = _count_fighters_still_missing(connection)
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


def _build_exact_name_index(fighters: list[FighterMatchRecord]) -> dict[str, FighterMatchRecord]:
    return {fighter.name.casefold(): fighter for fighter in fighters if fighter.name}


def _build_normalized_name_index(fighters: list[FighterMatchRecord]) -> dict[str, FighterMatchRecord]:
    return {_normalize_name(fighter.name): fighter for fighter in fighters if fighter.name}


def _index_fighter(
    fighter: FighterMatchRecord,
    exact_name_index: dict[str, FighterMatchRecord],
    normalized_name_index: dict[str, FighterMatchRecord],
) -> None:
    exact_name_index[fighter.name.casefold()] = fighter
    normalized_name_index[_normalize_name(fighter.name)] = fighter


def _match_fighter(
    full_name: str,
    exact_name_index: dict[str, FighterMatchRecord],
    normalized_name_index: dict[str, FighterMatchRecord],
) -> FighterMatchRecord | None:
    exact_match = exact_name_index.get(full_name.casefold())
    if exact_match is not None:
        return exact_match
    normalized_name = _normalize_name(full_name)
    normalized_match = normalized_name_index.get(normalized_name)
    if normalized_match is not None:
        return normalized_match
    candidates = get_close_matches(normalized_name, normalized_name_index.keys(), n=1, cutoff=FUZZY_MATCH_THRESHOLD)
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


_NOPHOTO_RE = re.compile(r"nophoto|silhouette|no-photo", re.IGNORECASE)


def _extract_headshot_url(payload: dict[str, Any]) -> str | None:
    url: str | None = None
    headshot = payload.get("headshot")
    if isinstance(headshot, dict):
        url = _clean_text(headshot.get("href"))
    if url is None:
        images = payload.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            url = _clean_text(images[0].get("href"))
    # ESPN serves a generic "nophoto" silhouette when a real headshot is missing.
    if url and _NOPHOTO_RE.search(url):
        return None
    return url


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


def _is_missing_core_data(athlete: EspnAthlete) -> bool:
    return any(
        value is None
        for value in (
            athlete.nationality,
            athlete.birth_date,
            athlete.height_cm,
            athlete.reach_cm,
            athlete.weight_grams,
        )
    )


def _count_fighters_still_missing(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM fighters
            WHERE NULLIF(nationality, '') IS NULL
               OR birth_date IS NULL
               OR height_cm IS NULL
               OR reach_cm IS NULL
               OR weight_grams IS NULL
            """
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0


def _build_summary(counts: Counter) -> str:
    return json.dumps(
        {
            "pages": counts["pages"],
            "athlete_refs": counts["athlete_refs"],
            "athletes_fetched": counts["athletes_fetched"],
            "matched": counts["matched"],
            "updated": counts["updated"],
            "inserted": counts["inserted"],
            "still_missing_data": counts["still_missing_data"],
            "errors": counts["errors"],
            "espn_missing_core_data": counts["espn_missing_core_data"],
        },
        indent=2,
        sort_keys=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich fighters from ESPN UFC athlete data.")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit ESPN pagination for testing.")
    args = parser.parse_args()
    configure_logging()
    counts = scrape_and_enrich(max_pages=args.max_pages)
    print(_build_summary(counts))


if __name__ == "__main__":
    main()