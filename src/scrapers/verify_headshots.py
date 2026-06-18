from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

import psycopg2
import requests

from .config import get_settings
from .db import connect


ESPN_ATHLETE_API_TEMPLATE = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/athletes/{athlete_id}"
HEADSHOT_ID_PATTERN = re.compile(r"/players/full/(\d+)\.(?:png|jpg|jpeg)(?:$|\?)", re.IGNORECASE)
MATCH_THRESHOLD = 0.80
REQUEST_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class HeadshotMismatch:
    fighter_id: int
    fighter_name: str
    espn_athlete_id: str
    espn_name: str
    similarity: float
    headshot_url: str


@dataclass(frozen=True)
class VerificationSummary:
    fighters_with_headshots: int
    checked: int
    mismatches: int
    nullified: int
    mismatch_rows: list[dict[str, object]]


def _extract_athlete_id(headshot_url: str) -> str | None:
    match = HEADSHOT_ID_PATTERN.search(headshot_url)
    if not match:
        return None
    return match.group(1)


def _normalize_name(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", name.casefold()).split())


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize_name(left), _normalize_name(right)).ratio()


def _fetch_espn_name(session: requests.Session, athlete_id: str) -> str | None:
    response = session.get(ESPN_ATHLETE_API_TEMPLATE.format(athlete_id=athlete_id), timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    name = str(payload.get("displayName") or payload.get("fullName") or "").strip()
    return name or None


def verify_headshots() -> VerificationSummary:
    settings = get_settings()
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": settings.user_agent.replace("ufcstats.com", "espn.com"),
        }
    )

    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, headshot_url
                FROM fighters
                WHERE headshot_url IS NOT NULL
                ORDER BY id
                """
            )
            fighters = [(int(row[0]), str(row[1]), str(row[2])) for row in cursor.fetchall()]

    mismatches: list[HeadshotMismatch] = []
    checked = 0
    athlete_name_cache: dict[str, str | None] = {}

    for fighter_id, fighter_name, headshot_url in fighters:
        athlete_id = _extract_athlete_id(headshot_url)
        if athlete_id is None:
            continue
        if athlete_id not in athlete_name_cache:
            athlete_name_cache[athlete_id] = _fetch_espn_name(session, athlete_id)
            time.sleep(REQUEST_DELAY_SECONDS)
        espn_name = athlete_name_cache[athlete_id]
        if not espn_name:
            continue
        checked += 1
        similarity = _similarity(fighter_name, espn_name)
        if similarity >= MATCH_THRESHOLD:
            continue
        mismatches.append(
            HeadshotMismatch(
                fighter_id=fighter_id,
                fighter_name=fighter_name,
                espn_athlete_id=athlete_id,
                espn_name=espn_name,
                similarity=round(similarity, 4),
                headshot_url=headshot_url,
            )
        )

    mismatch_ids = [row.fighter_id for row in mismatches]
    if mismatch_ids:
        for attempt in range(2):
            try:
                with connect(settings.database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE fighters
                            SET headshot_url = NULL, updated_at = NOW()
                            WHERE id = ANY(%s)
                            """,
                            (mismatch_ids,),
                        )
                    connection.commit()
                break
            except psycopg2.OperationalError:
                if attempt == 1:
                    raise
                time.sleep(1)

    return VerificationSummary(
        fighters_with_headshots=len(fighters),
        checked=checked,
        mismatches=len(mismatches),
        nullified=len(mismatches),
        mismatch_rows=[asdict(row) for row in mismatches],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify ESPN headshots against fighter names and null mismatches.")
    parser.parse_args()
    print(json.dumps(asdict(verify_headshots()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()