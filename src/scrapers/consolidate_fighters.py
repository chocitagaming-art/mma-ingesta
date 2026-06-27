from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from typing import Any

import requests

from .config import get_settings
from .db import connect
from .espn_import_all import (
    _clean_text,
    _extract_headshot_url,
    _inches_to_cm,
    _nested_text,
    _parse_birth_date,
    _pounds_to_grams,
)
from .logging_config import configure_logging
from .models import FighterRecord
from .repositories.fighters import upsert_fighter


LOGGER = logging.getLogger(__name__)

ESPN_SOURCE = "espn"
UFCSTATS_SOURCE = "ufcstats"
SHERDOG_SOURCE = "sherdog"

ESPN_ATHLETE_URL = "https://sports.core.api.espn.com/v2/sports/mma/athletes/{athlete_id}?lang=en&region=us"
ESPN_SEARCH_URL = "https://site.web.api.espn.com/apis/search/v2"
ESPN_REQUEST_DELAY_SECONDS = 0.3
ESPN_REQUEST_RETRIES = 2

# The enrichment fields copied from an ESPN duplicate into the kept UFCStats row.
ENRICHMENT_FIELDS = (
    "headshot_url",
    "nationality",
    "birth_date",
    "height_cm",
    "reach_cm",
    "weight_grams",
    "nickname",
)


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; mma-ingesta/1.0; +https://espn.com)",
        }
    )
    return session


def _get_json(session: requests.Session, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return parsed JSON or None on a 404/persistent error."""
    last_exc: Exception | None = None
    for attempt in range(ESPN_REQUEST_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=30)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - network resiliency
            last_exc = exc
            time.sleep(ESPN_REQUEST_DELAY_SECONDS * (attempt + 1))
    LOGGER.warning("ESPN request failed for %s: %s", url, last_exc)
    return None


# ---------------------------------------------------------------------------
# Dependent-row cleanup helpers
# ---------------------------------------------------------------------------

def _reassign_references(cursor, from_id: int, to_id: int) -> None:
    cursor.execute("UPDATE fights SET fighter_red_id = %s WHERE fighter_red_id = %s", (to_id, from_id))
    cursor.execute("UPDATE fights SET fighter_blue_id = %s WHERE fighter_blue_id = %s", (to_id, from_id))
    cursor.execute("UPDATE fights SET winner_id = %s WHERE winner_id = %s", (to_id, from_id))
    cursor.execute(
        """
        DELETE FROM fight_stats
        WHERE fighter_id = %s
          AND EXISTS (
            SELECT 1 FROM fight_stats existing
            WHERE existing.fight_id = fight_stats.fight_id
              AND existing.fighter_id = %s
          )
        """,
        (from_id, to_id),
    )
    cursor.execute("UPDATE fight_stats SET fighter_id = %s WHERE fighter_id = %s", (to_id, from_id))
    if _table_exists(cursor, "rankings"):
        cursor.execute("UPDATE rankings SET fighter_id = %s WHERE fighter_id = %s", (to_id, from_id))
    cursor.execute("UPDATE news SET fighter_id = %s WHERE fighter_id = %s", (to_id, from_id))


def _delete_fighter(cursor, fighter_id: int) -> None:
    """Delete a fighter and every row that references it."""
    cursor.execute(
        """
        DELETE FROM fight_stats
        WHERE fight_id IN (
            SELECT id FROM fights
            WHERE fighter_red_id = %s OR fighter_blue_id = %s
        )
        """,
        (fighter_id, fighter_id),
    )
    cursor.execute("DELETE FROM fight_stats WHERE fighter_id = %s", (fighter_id,))
    cursor.execute(
        "DELETE FROM fights WHERE fighter_red_id = %s OR fighter_blue_id = %s",
        (fighter_id, fighter_id),
    )
    if _table_exists(cursor, "rankings"):
        cursor.execute("DELETE FROM rankings WHERE fighter_id = %s", (fighter_id,))
    cursor.execute("DELETE FROM news WHERE fighter_id = %s", (fighter_id,))
    cursor.execute("DELETE FROM fighters WHERE id = %s", (fighter_id,))


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        )
        """,
        (table_name,),
    )
    return bool(cursor.fetchone()[0])


def _has_shared_fight(cursor, fighter_id: int) -> bool:
    """True when the fighter has any fight whose other corner is a *different*
    fighter. Deleting such a fighter would cascade-delete a bout that belongs to
    someone else, so the caller must protect it instead of purging."""
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM fights
            WHERE (fighter_red_id = %s AND fighter_blue_id IS NOT NULL AND fighter_blue_id <> %s)
               OR (fighter_blue_id = %s AND fighter_red_id IS NOT NULL AND fighter_red_id <> %s)
        )
        """,
        (fighter_id, fighter_id, fighter_id, fighter_id),
    )
    return bool(cursor.fetchone()[0])


def _pair_identity_conflict(espn_vals: dict[str, Any], ufc_vals: dict[str, Any]) -> bool:
    """True when a same-name ESPN/UFCStats pair looks like two different people.

    Conflicting non-null birth dates (or nationalities) are a strong signal that
    the shared name is a coincidence (homonyms), not a duplicate. Merging them
    would fuse two careers, so the caller skips the pair unless --force-homonyms.
    """
    eb, ub = espn_vals.get("birth_date"), ufc_vals.get("birth_date")
    if eb and ub and eb != ub:
        return True
    en, un = espn_vals.get("nationality"), ufc_vals.get("nationality")
    if en and un and en != un:
        return True
    return False


# ---------------------------------------------------------------------------
# Step 1: merge duplicate names across ESPN + UFCStats
# ---------------------------------------------------------------------------

def merge_duplicates(
    connection,
    counts: Counter,
    apply: bool = False,
    force_homonyms: bool = False,
) -> list[dict[str, Any]]:
    merges: list[dict[str, Any]] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                e.id AS espn_id,
                u.id AS ufc_id,
                u.name AS name,
                e.headshot_url, e.nationality, e.birth_date, e.height_cm,
                e.reach_cm, e.weight_grams, e.nickname,
                u.headshot_url, u.nationality, u.birth_date, u.height_cm,
                u.reach_cm, u.weight_grams, u.nickname
            FROM fighters e
            JOIN fighters u
              ON u.source = %s
             AND lower(btrim(regexp_replace(u.name, '\\s+', ' ', 'g')))
               = lower(btrim(regexp_replace(e.name, '\\s+', ' ', 'g')))
            WHERE e.source = %s
            ORDER BY u.name, e.id
            """,
            (UFCSTATS_SOURCE, ESPN_SOURCE),
        )
        rows = cursor.fetchall()

        for row in rows:
            espn_id = int(row[0])
            ufc_id = int(row[1])
            name = row[2]
            espn_vals = {
                "headshot_url": row[3],
                "nationality": row[4],
                "birth_date": row[5],
                "height_cm": row[6],
                "reach_cm": row[7],
                "weight_grams": row[8],
                "nickname": row[9],
            }
            ufc_vals = {
                "headshot_url": row[10],
                "nationality": row[11],
                "birth_date": row[12],
                "height_cm": row[13],
                "reach_cm": row[14],
                "weight_grams": row[15],
                "nickname": row[16],
            }
            if not force_homonyms and _pair_identity_conflict(espn_vals, ufc_vals):
                counts["homonyms_skipped"] += 1
                LOGGER.warning(
                    "HOMONYM skip '%s': espn id=%s vs ufcstats id=%s have conflicting identity",
                    name,
                    espn_id,
                    ufc_id,
                )
                continue

            enriched = [
                field
                for field in ENRICHMENT_FIELDS
                if ufc_vals[field] in (None, "") and espn_vals[field] not in (None, "")
            ]

            if apply:
                cursor.execute(
                    """
                    UPDATE fighters u
                    SET
                        headshot_url = COALESCE(NULLIF(u.headshot_url, ''), NULLIF(e.headshot_url, '')),
                        nationality  = COALESCE(NULLIF(u.nationality, ''), NULLIF(e.nationality, '')),
                        birth_date   = COALESCE(u.birth_date, e.birth_date),
                        height_cm    = COALESCE(u.height_cm, e.height_cm),
                        reach_cm     = COALESCE(u.reach_cm, e.reach_cm),
                        weight_grams = COALESCE(u.weight_grams, e.weight_grams),
                        nickname     = COALESCE(NULLIF(u.nickname, ''), NULLIF(e.nickname, '')),
                        updated_at   = NOW()
                    FROM fighters e
                    WHERE u.id = %s AND e.id = %s
                    """,
                    (ufc_id, espn_id),
                )

                _reassign_references(cursor, espn_id, ufc_id)
                cursor.execute("DELETE FROM fighters WHERE id = %s", (espn_id,))

            counts["duplicates_merged"] += 1
            for field in enriched:
                counts[f"enriched_{field}"] += 1
            merge_entry = {
                "name": name,
                "kept_ufcstats_id": ufc_id,
                "deleted_espn_id": espn_id,
                "enriched_fields": enriched,
            }
            merges.append(merge_entry)
            LOGGER.info(
                "MERGE '%s': kept ufcstats id=%s, deleted espn id=%s, enriched=%s",
                name,
                ufc_id,
                espn_id,
                enriched or "none",
            )

    if apply:
        connection.commit()
    else:
        connection.rollback()
    return merges


# ---------------------------------------------------------------------------
# Step 2: keep active ESPN-only fighters, delete inactive ones
# ---------------------------------------------------------------------------

def resolve_espn_only_activity(
    connection,
    session: requests.Session,
    counts: Counter,
    apply: bool = False,
) -> dict[str, list]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, name, source_id FROM fighters WHERE source = %s ORDER BY id",
            (ESPN_SOURCE,),
        )
        espn_only = [(int(r[0]), r[1], r[2]) for r in cursor.fetchall()]

    LOGGER.info("Checking ESPN 'active' flag for %s ESPN-only fighters", len(espn_only))
    deleted: list[dict[str, Any]] = []
    kept_active: list[int] = []
    kept_unknown: list[dict[str, Any]] = []
    protected_shared: list[dict[str, Any]] = []

    for fighter_id, name, source_id in espn_only:
        payload = None
        if source_id:
            payload = _get_json(session, ESPN_ATHLETE_URL.format(athlete_id=source_id))
        time.sleep(ESPN_REQUEST_DELAY_SECONDS)

        if payload is None:
            # Could not confirm status -> keep to avoid deleting on transient/404 errors.
            counts["espn_only_kept_unknown"] += 1
            kept_unknown.append({"id": fighter_id, "name": name, "source_id": source_id})
            LOGGER.warning("ESPN status UNKNOWN for '%s' (id=%s, source_id=%s); keeping", name, fighter_id, source_id)
            continue

        active = payload.get("active")
        if active is True:
            counts["espn_only_active_kept"] += 1
            kept_active.append(fighter_id)
            continue

        with connection.cursor() as cursor:
            if _has_shared_fight(cursor, fighter_id):
                counts["protected_shared"] += 1
                protected_shared.append({"id": fighter_id, "name": name, "source_id": source_id})
                LOGGER.warning(
                    "PROTECT inactive ESPN fighter '%s' (id=%s): has a fight shared with another fighter",
                    name,
                    fighter_id,
                )
                continue
            if apply:
                _delete_fighter(cursor, fighter_id)
        if apply:
            connection.commit()
        counts["espn_only_inactive_deleted"] += 1
        deleted.append({"id": fighter_id, "name": name, "source_id": source_id})
        LOGGER.info("DELETE inactive ESPN fighter '%s' (id=%s, source_id=%s)", name, fighter_id, source_id)

    if apply:
        connection.commit()
    else:
        connection.rollback()
    return {
        "deleted": deleted,
        "kept_active": kept_active,
        "kept_unknown": kept_unknown,
        "protected_shared": protected_shared,
    }


# ---------------------------------------------------------------------------
# Step 3: delete Sherdog fighters
# ---------------------------------------------------------------------------

def delete_sherdog_fighters(connection, counts: Counter, apply: bool = False) -> list[int]:
    deleted_ids: list[int] = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM fighters WHERE source = %s", (SHERDOG_SOURCE,))
        sherdog_ids = [int(r[0]) for r in cursor.fetchall()]
        for fighter_id in sherdog_ids:
            if _has_shared_fight(cursor, fighter_id):
                counts["sherdog_protected_shared"] += 1
                LOGGER.warning(
                    "PROTECT Sherdog fighter id=%s: has a fight shared with another fighter",
                    fighter_id,
                )
                continue
            if apply:
                _delete_fighter(cursor, fighter_id)
            deleted_ids.append(fighter_id)
    counts["sherdog_deleted"] = len(deleted_ids)
    if apply:
        connection.commit()
    else:
        connection.rollback()
    LOGGER.info("Deleted %s Sherdog fighters (apply=%s)", len(deleted_ids), apply)
    return deleted_ids


# ---------------------------------------------------------------------------
# Step 4: add Conor McGregor
# ---------------------------------------------------------------------------

def _athlete_record_from_payload(payload: dict[str, Any]) -> FighterRecord:
    return FighterRecord(
        name=(payload.get("fullName") or payload.get("displayName") or "").strip(),
        nickname=_clean_text(payload.get("nickname")),
        headshot_url=_extract_headshot_url(payload),
        nationality=_clean_text(payload.get("citizenship")),
        birth_date=_parse_birth_date(payload.get("dateOfBirth")),
        height_cm=_inches_to_cm(payload.get("height")),
        reach_cm=_inches_to_cm(payload.get("reach")),
        stance=_nested_text(payload.get("stance")),
        weight_grams=_pounds_to_grams(payload.get("weight")),
        wins=0,
        losses=0,
        draws=0,
        source=ESPN_SOURCE,
        source_id=str(payload["id"]),
    )


def _search_espn_athlete_id(session: requests.Session, query: str) -> str | None:
    payload = _get_json(session, ESPN_SEARCH_URL, params={"query": query, "limit": 20})
    if not payload:
        return None
    for result in payload.get("results", []):
        if result.get("type") not in ("player", "athlete"):
            continue
        for content in result.get("contents", []):
            link = (content.get("link") or {}).get("web") or ""
            if "/mma/" in link:
                athlete_id = content.get("id")
                if athlete_id:
                    return str(athlete_id)
    return None


def add_mcgregor(connection, session: requests.Session, counts: Counter, apply: bool = False) -> dict[str, Any]:
    # The id in the legacy ESPN URL (2335451) 404s on the core API; his live
    # ESPN athlete id is 3022677 (resolved via ESPN search). Try the legacy id
    # first, then the known live id, then fall back to a fresh search.
    candidate_ids = ["2335451", "3022677"]
    payload: dict[str, Any] | None = None
    used_id: str | None = None

    for athlete_id in candidate_ids:
        payload = _get_json(session, ESPN_ATHLETE_URL.format(athlete_id=athlete_id))
        if payload:
            used_id = athlete_id
            break

    if payload is None:
        resolved_id = _search_espn_athlete_id(session, "Conor McGregor")
        if resolved_id:
            payload = _get_json(session, ESPN_ATHLETE_URL.format(athlete_id=resolved_id))
            used_id = resolved_id

    if payload is None:
        LOGGER.warning("Could not locate Conor McGregor on ESPN")
        counts["mcgregor_added"] = 0
        return {"added": False, "reason": "not_found_on_espn"}

    record = _athlete_record_from_payload(payload)
    if not apply:
        counts["mcgregor_added"] = 0
        LOGGER.info("[DRY-RUN] Would add Conor McGregor (espn id=%s)", used_id)
        return {
            "added": False,
            "dry_run": True,
            "espn_source_id": used_id,
            "name": record.name,
            "active": payload.get("active"),
        }
    fighter_id = upsert_fighter(connection, record)
    connection.commit()
    counts["mcgregor_added"] = 1
    LOGGER.info("Added Conor McGregor (espn id=%s) as fighter id=%s", used_id, fighter_id)
    return {
        "added": True,
        "espn_source_id": used_id,
        "fighter_id": fighter_id,
        "name": record.name,
        "active": payload.get("active"),
    }


# ---------------------------------------------------------------------------
# Step 5: report
# ---------------------------------------------------------------------------

def build_report(connection, counts: Counter) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM fighters")
        total = int(cursor.fetchone()[0])

        cursor.execute("SELECT source, COUNT(*) FROM fighters GROUP BY source ORDER BY source")
        by_source = {row[0]: int(row[1]) for row in cursor.fetchall()}

        cursor.execute(
            """
            SELECT COUNT(DISTINCT f.id)
            FROM fighters f
            JOIN fights ft ON ft.fighter_red_id = f.id OR ft.fighter_blue_id = f.id
            """
        )
        with_fights = int(cursor.fetchone()[0])

        # active ESPN-only (no fights) vs UFCStats-enriched vs UFCStats-only
        cursor.execute(
            """
            SELECT COUNT(*) FROM fighters f
            WHERE f.source = %s
              AND NOT EXISTS (
                SELECT 1 FROM fights ft
                WHERE ft.fighter_red_id = f.id OR ft.fighter_blue_id = f.id
              )
            """,
            (ESPN_SOURCE,),
        )
        active_espn_only = int(cursor.fetchone()[0])

        cursor.execute(
            """
            SELECT COUNT(*) FROM fighters f
            WHERE f.source = %s
              AND EXISTS (
                SELECT 1 FROM fights ft
                WHERE ft.fighter_red_id = f.id OR ft.fighter_blue_id = f.id
              )
            """,
            (UFCSTATS_SOURCE,),
        )
        ufcstats_with_fights = int(cursor.fetchone()[0])

    report = {
        "final_fighter_count": total,
        "by_source": by_source,
        "breakdown": {
            "active_espn_only": active_espn_only,
            "ufcstats_enriched": int(counts["duplicates_merged"]),
            "ufcstats_with_fights": ufcstats_with_fights,
        },
        "duplicates_merged": int(counts["duplicates_merged"]),
        "homonyms_skipped": int(counts["homonyms_skipped"]),
        "espn_only_active_kept": int(counts["espn_only_active_kept"]),
        "espn_only_inactive_deleted": int(counts["espn_only_inactive_deleted"]),
        "espn_only_kept_unknown": int(counts["espn_only_kept_unknown"]),
        "protected_shared": int(counts["protected_shared"]),
        "sherdog_deleted": int(counts["sherdog_deleted"]),
        "sherdog_protected_shared": int(counts["sherdog_protected_shared"]),
        "mcgregor_added": int(counts["mcgregor_added"]),
        "fighters_with_fights": with_fights,
        "fighters_without_fights": total - with_fights,
        "enrichment_field_counts": {
            field: int(counts[f"enriched_{field}"])
            for field in ENRICHMENT_FIELDS
            if counts[f"enriched_{field}"]
        },
    }
    return report


def consolidate(apply: bool = False, force_homonyms: bool = False) -> dict[str, Any]:
    settings = get_settings()
    counts: Counter = Counter()
    session = _build_session()

    with connect(settings.database_url) as connection:
        if apply:
            # Defensive: if this process is interrupted mid-step, do not leave an
            # orphaned "idle in transaction" connection holding row locks.
            with connection.cursor() as cursor:
                cursor.execute("SET idle_in_transaction_session_timeout = '30s'")
            connection.commit()

        LOGGER.info("Step 1: merging duplicate ESPN/UFCStats fighters (apply=%s)", apply)
        merges = merge_duplicates(connection, counts, apply=apply, force_homonyms=force_homonyms)

        LOGGER.info("Step 2: resolving ESPN-only fighter activity")
        espn_only_result = resolve_espn_only_activity(connection, session, counts, apply=apply)

        LOGGER.info("Step 3: deleting Sherdog fighters")
        delete_sherdog_fighters(connection, counts, apply=apply)

        LOGGER.info("Step 4: adding Conor McGregor")
        mcgregor_result = add_mcgregor(connection, session, counts, apply=apply)

        LOGGER.info("Step 5: building report")
        report = build_report(connection, counts)

    report["applied"] = apply
    report["dry_run"] = not apply
    report["force_homonyms"] = force_homonyms
    report["mcgregor"] = mcgregor_result
    report["merges_sample"] = merges[:10]
    report["inactive_espn_deleted_sample"] = espn_only_result["deleted"][:10]
    report["kept_unknown_count"] = len(espn_only_result["kept_unknown"])
    report["protected_shared_sample"] = espn_only_result["protected_shared"][:10]
    return report


def merge_only(apply: bool = False, force_homonyms: bool = False) -> dict[str, Any]:
    settings = get_settings()
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        if apply:
            with connection.cursor() as cursor:
                cursor.execute("SET idle_in_transaction_session_timeout = '30s'")
            connection.commit()
        merges = merge_duplicates(connection, counts, apply=apply, force_homonyms=force_homonyms)
        report = build_report(connection, counts)
    report["applied"] = apply
    report["dry_run"] = not apply
    report["force_homonyms"] = force_homonyms
    report["merges_sample"] = merges[:10]
    return report


def add_mcgregor_only(apply: bool = False) -> dict[str, Any]:
    settings = get_settings()
    counts: Counter = Counter()
    session = _build_session()
    with connect(settings.database_url) as connection:
        result = add_mcgregor(connection, session, counts, apply=apply)
        report = build_report(connection, counts)
    report["applied"] = apply
    report["dry_run"] = not apply
    report["mcgregor"] = result
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidate the fighters table (merge, prune, enrich).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to the DB (default: dry-run preview, no writes).",
    )
    parser.add_argument(
        "--force-homonyms",
        action="store_true",
        help="Also merge same-name fighters that look like distinct people (conflicting birth date/nationality).",
    )
    parser.add_argument(
        "--mcgregor-only",
        action="store_true",
        help="Only run Step 4 (add Conor McGregor) without re-running the full consolidation.",
    )
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="Only run Step 1 (merge duplicate ESPN/UFCStats fighters). Safe to re-run.",
    )
    args = parser.parse_args()
    configure_logging()
    if args.mcgregor_only:
        report = add_mcgregor_only(apply=args.apply)
    elif args.merge_only:
        report = merge_only(apply=args.apply, force_homonyms=args.force_homonyms)
    else:
        report = consolidate(apply=args.apply, force_homonyms=args.force_homonyms)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
