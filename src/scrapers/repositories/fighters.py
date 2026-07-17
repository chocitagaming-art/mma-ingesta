from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from psycopg2.extensions import connection as PgConnection

from ..models import FighterRecord


@dataclass(frozen=True)
class FighterMatchRecord:
    id: int
    name: str
    nickname: str | None
    nationality: str | None
    birth_date: date | None
    height_cm: float | None
    reach_cm: float | None
    weight_grams: int | None
    stance: str | None


def upsert_fighter(connection: PgConnection, fighter: FighterRecord) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fighters (
                name, nickname, headshot_url, nationality, birth_date, height_cm, reach_cm, stance,
                weight_grams, wins, losses, draws, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, source_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                nickname = EXCLUDED.nickname,
                headshot_url = EXCLUDED.headshot_url,
                nationality = EXCLUDED.nationality,
                birth_date = EXCLUDED.birth_date,
                height_cm = EXCLUDED.height_cm,
                reach_cm = EXCLUDED.reach_cm,
                stance = EXCLUDED.stance,
                weight_grams = EXCLUDED.weight_grams,
                wins = EXCLUDED.wins,
                losses = EXCLUDED.losses,
                draws = EXCLUDED.draws,
                updated_at = NOW()
            RETURNING id
            """,
            (
                fighter.name,
                fighter.nickname,
                fighter.headshot_url,
                fighter.nationality,
                fighter.birth_date,
                fighter.height_cm,
                fighter.reach_cm,
                fighter.stance,
                fighter.weight_grams,
                fighter.wins,
                fighter.losses,
                fighter.draws,
                fighter.source,
                fighter.source_id,
            ),
        )
        return int(cursor.fetchone()[0])


def get_fighter_id_by_source(connection: PgConnection, source: str, source_id: str) -> int | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM fighters WHERE source = %s AND source_id = %s",
            (source, source_id),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None


def get_all_fighters(connection: PgConnection) -> list[FighterMatchRecord]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, nickname, nationality, birth_date, height_cm, reach_cm, weight_grams, stance
            FROM fighters
            """
        )
        rows = cursor.fetchall()
    return [
        FighterMatchRecord(
            id=int(row[0]),
            name=row[1],
            nickname=row[2],
            nationality=row[3],
            birth_date=row[4],
            height_cm=float(row[5]) if row[5] is not None else None,
            reach_cm=float(row[6]) if row[6] is not None else None,
            weight_grams=int(row[7]) if row[7] is not None else None,
            stance=row[8],
        )
        for row in rows
    ]


def update_fighter_enrichment(
    connection: PgConnection,
    fighter_id: int,
    *,
    nickname: str | None = None,
    headshot_url: str | None = None,
    nationality: str | None = None,
    birth_date: date | None = None,
    height_cm: float | None = None,
    reach_cm: float | None = None,
    weight_grams: int | None = None,
    stance: str | None = None,
    full_body_url: str | None = None,
    leg_reach_cm: float | None = None,
    trains_at: str | None = None,
    birth_place: str | None = None,
    octagon_debut: date | None = None,
) -> bool:
    """Fill ONLY empty columns (COALESCE keeps existing data; a NULL argument can
    never overwrite a populated value). Returns True if a row was updated."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters
            SET
                nickname = COALESCE(NULLIF(nickname, ''), %s),
                headshot_url = COALESCE(NULLIF(headshot_url, ''), %s),
                nationality = COALESCE(NULLIF(nationality, ''), %s),
                birth_date = COALESCE(birth_date, %s),
                height_cm = COALESCE(height_cm, %s),
                reach_cm = COALESCE(reach_cm, %s),
                weight_grams = COALESCE(weight_grams, %s),
                stance = COALESCE(NULLIF(stance, ''), %s),
                full_body_url = COALESCE(NULLIF(full_body_url, ''), %s),
                leg_reach_cm = COALESCE(leg_reach_cm, %s),
                trains_at = COALESCE(NULLIF(trains_at, ''), %s),
                birth_place = COALESCE(NULLIF(birth_place, ''), %s),
                octagon_debut = COALESCE(octagon_debut, %s),
                updated_at = NOW()
            WHERE id = %s
              AND (
                (NULLIF(nickname, '') IS NULL AND %s IS NOT NULL)
                OR (NULLIF(headshot_url, '') IS NULL AND %s IS NOT NULL)
                OR (NULLIF(nationality, '') IS NULL AND %s IS NOT NULL)
                OR (birth_date IS NULL AND %s IS NOT NULL)
                OR (height_cm IS NULL AND %s IS NOT NULL)
                OR (reach_cm IS NULL AND %s IS NOT NULL)
                OR (weight_grams IS NULL AND %s IS NOT NULL)
                OR (NULLIF(stance, '') IS NULL AND %s IS NOT NULL)
                OR (NULLIF(full_body_url, '') IS NULL AND %s IS NOT NULL)
                OR (leg_reach_cm IS NULL AND %s IS NOT NULL)
                OR (NULLIF(trains_at, '') IS NULL AND %s IS NOT NULL)
                OR (NULLIF(birth_place, '') IS NULL AND %s IS NOT NULL)
                OR (octagon_debut IS NULL AND %s IS NOT NULL)
              )
            """,
            (
                nickname,
                headshot_url,
                nationality,
                birth_date,
                height_cm,
                reach_cm,
                weight_grams,
                stance,
                full_body_url,
                leg_reach_cm,
                trains_at,
                birth_place,
                octagon_debut,
                fighter_id,
                nickname,
                headshot_url,
                nationality,
                birth_date,
                height_cm,
                reach_cm,
                weight_grams,
                stance,
                full_body_url,
                leg_reach_cm,
                trains_at,
                birth_place,
                octagon_debut,
            ),
        )
        return cursor.rowcount > 0


def update_fighter_facts(
    connection: PgConnection,
    fighter_id: int,
    *,
    facts: list[str] | None = None,
    qa: list[dict[str, str]] | None = None,
) -> bool:
    """Store the Spanish-translated Fighter Facts / Q&A (JSONB, migration 015).

    Additive-only like update_fighter_enrichment, but for JSONB (no NULLIF):
    COALESCE(column, %s::jsonb) keeps whatever is already stored, so a fighter
    is NEVER re-translated once populated. Empty lists are treated as NULL and
    never written. Returns True if a row was updated.
    """
    facts_json = json.dumps(facts, ensure_ascii=False) if facts else None
    qa_json = json.dumps(qa, ensure_ascii=False) if qa else None
    if facts_json is None and qa_json is None:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters
            SET
                fighter_facts = COALESCE(fighter_facts, %s::jsonb),
                fighter_qa = COALESCE(fighter_qa, %s::jsonb),
                updated_at = NOW()
            WHERE id = %s
              AND (
                (fighter_facts IS NULL AND %s::jsonb IS NOT NULL)
                OR (fighter_qa IS NULL AND %s::jsonb IS NOT NULL)
              )
            """,
            (facts_json, qa_json, fighter_id, facts_json, qa_json),
        )
        return cursor.rowcount > 0


def set_fighter_espn_id(connection: PgConnection, fighter_id: int, espn_id: str) -> bool:
    """Store the resolved ESPN athlete id (migration 016). Additive-only: an
    already-populated espn_id is never overwritten. The partial UNIQUE index
    (fighters_espn_id_key) rejects attaching one athlete to two DB rows — the
    caller must handle psycopg2.errors.UniqueViolation (rollback + skip).
    Returns True if a row was updated."""
    if not espn_id:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters
            SET espn_id = %s, updated_at = NOW()
            WHERE id = %s AND espn_id IS NULL
            """,
            (espn_id, fighter_id),
        )
        return cursor.rowcount > 0


def update_fighter_standing_photo(
    connection: PgConnection,
    fighter_id: int,
    standing_body_url: str | None,
) -> bool:
    """Store the ufc.com standing full-body photo (migration 009).

    Unlike update_fighter_enrichment, the NEW value WINS: UFC refreshes this
    photo after every fight, so the freshest scraped URL is authoritative and
    replaces whatever is stored. Two guards remain: a NULL/empty new value
    never wipes an existing one (early return, no SQL), and an identical value
    skips the row churn (IS DISTINCT FROM). Returns True if a row was updated.
    """
    if not standing_body_url:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters
            SET standing_body_url = %s, updated_at = NOW()
            WHERE id = %s
              AND standing_body_url IS DISTINCT FROM %s
            """,
            (standing_body_url, fighter_id, standing_body_url),
        )
        return cursor.rowcount > 0


def update_fighter_standing_variant(
    connection: PgConnection,
    fighter_id: int,
    standing_body_url: str,
    direction: str,
) -> bool:
    """Store the directional standing photo (migration 019, F1 Tanda 4).

    direction is 'L' or 'R' (the token embedded in the ufc.com card URL, which
    encodes the corner the fighter fought in: red -> _L_, blue -> _R_). Writes
    standing_body_url_l for 'L' and standing_body_url_r for 'R'.

    Unlike update_fighter_standing_photo (legacy column, new-value-wins), this is
    FIRST-WRITER-WINS PER DIRECTION: it only fills the column while it is NULL
    (WHERE ... IS NULL). Callers sweep events newest-first, so the first URL seen
    for a direction is the freshest one — and re-running only fills the gaps
    (idempotent), which is exactly what the coverage cron needs. Returns True if
    a row was updated.
    """
    if not standing_body_url or direction not in ("L", "R"):
        return False
    column = "standing_body_url_l" if direction == "L" else "standing_body_url_r"
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE fighters
            SET {column} = %s, updated_at = NOW()
            WHERE id = %s
              AND {column} IS NULL
            """,
            (standing_body_url, fighter_id),
        )
        return cursor.rowcount > 0


def update_fighter_record(
    connection: PgConnection,
    fighter_id: int,
    *,
    wins: int,
    losses: int,
    draws: int,
) -> bool:
    """Fill a fighter's W-L-D only when the stored record is empty (0-0-0 / NULL).

    Never overwrites an already-populated record, and never writes an all-zero
    record (which would be a no-op anyway). Returns True if a row was updated.
    """
    if wins == 0 and losses == 0 and draws == 0:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters
            SET wins = %s, losses = %s, draws = %s, updated_at = NOW()
            WHERE id = %s
              AND COALESCE(wins, 0) = 0
              AND COALESCE(losses, 0) = 0
              AND COALESCE(draws, 0) = 0
            """,
            (wins, losses, draws, fighter_id),
        )
        return cursor.rowcount > 0


def bump_fighter_record(
    connection: PgConnection,
    fighter_id: int,
    *,
    wins: int,
    losses: int,
    draws: int,
) -> bool:
    """Refresh a fighter's stored W-L-D to a fresher authoritative record (ESPN
    'overall' = total career), but ONLY when the incoming record has MORE total
    bouts than the stored one.

    Unlike update_fighter_record (fill 0-0-0 only), this UPDATES a populated
    record to reflect fights that were sealed after the last roster scrape (the
    stored W-L-D is never incremented on result-seal, so it freezes). It is
    strictly MONOTONIC on the bout count: it never lowers wins+losses+draws, so a
    transient bad read or a homonym mismatch (fewer total bouts) can never regress
    a record, and a fighter with a gap in our data (prospect ESPN can't resolve)
    is left untouched rather than shrunk. Fills 0-0-0 too (any positive total
    beats zero). Returns True if a row was updated.
    """
    if wins < 0 or losses < 0 or draws < 0:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters
            SET wins = %s, losses = %s, draws = %s, updated_at = NOW()
            WHERE id = %s
              AND (%s + %s + %s) > (COALESCE(wins, 0) + COALESCE(losses, 0) + COALESCE(draws, 0))
            """,
            (wins, losses, draws, fighter_id, wins, losses, draws),
        )
        return cursor.rowcount > 0