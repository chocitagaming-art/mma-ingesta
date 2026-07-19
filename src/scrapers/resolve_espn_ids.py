"""Resuelve y persiste el id de atleta ESPN de cada luchador (fighters.espn_id).

Primer paso del pipeline S3-G (historial completo vía ESPN): para pedir la
carrera de un luchador al endpoint common/v3 hace falta su athlete id. Los
luchadores originados en ESPN ya lo llevan en source_id; los de ufcstats no
(espn.py matchea por nombre y DESCARTA el id). Este pase lo generaliza en la
columna fighters.espn_id (migración 016):

  1. Volcado SQL directo: source='espn' -> espn_id = source_id (sin red).
  2. Search API de ESPN para el resto, con el umbral de IDENTIDAD (0.92,
     política de matching.py: enlazar un id equivocado corrompe la ficha).

GUARDAS DE IDENTIDAD (lecciones Bruno Silva / S2):
  - Homónimos exactos en BD (dos filas con el mismo nombre) se EXCLUYEN de la
    selección: la búsqueda no puede saber cuál de los dos es.
  - Ambigüedad en ESPN: si DOS atletas distintos superan el umbral y quedan a
    menos de AMBIGUITY_MARGIN entre sí, se salta (no se adivina).
  - Un espn_id ya asignado a otra fila dispara UniqueViolation -> rollback y
    skip contabilizado (nunca se roba el id a otro luchador).
  - Escritura aditiva: espn_id poblado nunca se pisa (set_fighter_espn_id).

Los no resueltos quedan NULL y se reintentan en pases futuros (el scope del
cron es pequeño; --all es el barrido completo).

Usage:
    python -m src.scrapers.resolve_espn_ids --probe "Yaroslav Amosov"  # sin BD
    python -m src.scrapers.resolve_espn_ids --dry-run --limit 20
    python -m src.scrapers.resolve_espn_ids            # upcoming + recientes
    python -m src.scrapers.resolve_espn_ids --all      # barrido completo
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable

import psycopg2
import requests

from .config import get_settings
from .db import connect
from .enrich_ranked import ESPN_SEARCH_URL, UID_ID_PATTERN, _build_session
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, fold_ratio
from .repositories.fighters import set_fighter_espn_id

LOGGER = logging.getLogger(__name__)

PROGRESS_EVERY = 50
REQUEST_DELAY_SECONDS = 0.35
# Si el 2º mejor candidato queda a menos de este margen del 1º (y ambos sobre
# el umbral), el nombre es ambiguo en ESPN y no se asigna nada.
AMBIGUITY_MARGIN = 0.03

# candidates(session, name) -> [(score, espn_id, espn_name), ...]. Inyectable en tests.
CandidateSearch = Callable[[requests.Session, str], list[tuple[float, str, str]]]

# Target: (fighter_id, name)
Target = tuple[int, str]


def search_athlete_candidates(
    session: requests.Session, name: str
) -> list[tuple[float, str, str]]:
    """Todos los candidatos MMA de la Search API con su similitud, orden desc.

    A diferencia de enrich_ranked._search_espn_athlete (que devuelve solo el
    mejor), aquí se conservan todos para poder detectar ambigüedad."""
    response = session.get(ESPN_SEARCH_URL, params={"query": name, "limit": 10}, timeout=30)
    response.raise_for_status()
    data = response.json()
    seen: dict[str, tuple[float, str, str]] = {}
    for group in data.get("results", []):
        if group.get("type") != "player":
            continue
        for item in group.get("contents", []):
            if item.get("sport") != "mma":
                continue
            uid = str(item.get("uid") or "")
            id_match = UID_ID_PATTERN.search(uid)
            if not id_match:
                continue
            espn_name = str(item.get("displayName") or "").strip()
            if not espn_name:
                continue
            espn_id = id_match.group(1)
            score = fold_ratio(name, espn_name)
            current = seen.get(espn_id)
            if current is None or score > current[0]:
                seen[espn_id] = (score, espn_id, espn_name)
    return sorted(seen.values(), key=lambda item: item[0], reverse=True)


def pick_unambiguous_athlete(
    candidates: list[tuple[float, str, str]]
) -> tuple[str, str] | None:
    """(espn_id, espn_name) del mejor candidato SOLO si supera el umbral de
    identidad y no hay un segundo atleta distinto pisándole los talones."""
    if not candidates:
        return None
    best_score, best_id, best_name = candidates[0]
    if best_score < IDENTITY_THRESHOLD:
        return None
    for score, espn_id, espn_name in candidates[1:]:
        if espn_id == best_id:
            continue
        if score >= IDENTITY_THRESHOLD and best_score - score < AMBIGUITY_MARGIN:
            LOGGER.info(
                "Ambiguous on ESPN: %r (%.2f) vs %r (%.2f) — skipping",
                best_name, best_score, espn_name, score,
            )
            return None
        break  # ordenados desc: el resto queda aún más lejos
    return best_id, best_name


def seed_from_source_ids(connection, dry_run: bool = False) -> int:
    """Paso 1 (sin red): los luchadores source='espn' llevan el athlete id en
    source_id. UNIQUE(source, source_id) garantiza que no hay duplicados entre
    ellos; NOT EXISTS evita chocar con un espn_id ya asignado a otra fila."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters f
            SET espn_id = f.source_id, updated_at = NOW()
            WHERE f.source = 'espn'
              AND f.espn_id IS NULL
              AND f.source_id ~ '^[0-9]+$'
              AND NOT EXISTS (
                SELECT 1 FROM fighters other
                WHERE other.espn_id = f.source_id AND other.id <> f.id
              )
            """
        )
        seeded = cursor.rowcount
    if dry_run:
        connection.rollback()
    else:
        connection.commit()
    return seeded


def _get_target_fighters(
    connection, limit: int | None = None, all_scope: bool = False
) -> list[Target]:
    """Luchadores sin espn_id, excluyendo homónimos exactos en BD.

    Scope por defecto (cron): luchadores sin espn_id en cartelera próxima
    (los debutantes se resuelven al entrar en un cartel). --all: toda la tabla,
    priorizada igual que enrich_facts (ránking actual > cartelera próxima > con
    foto > resto) para que un pase parcial cubra primero a los más visibles.
    """
    homonym_free = """
              AND NOT EXISTS (
                SELECT 1 FROM fighters dup
                WHERE dup.id <> f.id AND lower(dup.name) = lower(f.name)
              )
    """
    if all_scope:
        sql = f"""
            SELECT f.id, f.name
            FROM fighters f
            WHERE f.name IS NOT NULL AND f.name <> ''
              AND f.espn_id IS NULL
              {homonym_free}
            ORDER BY
                EXISTS (
                    SELECT 1 FROM rankings r
                    WHERE r.fighter_id = f.id
                      AND r.snapshot_date = (SELECT MAX(snapshot_date) FROM rankings)
                ) DESC,
                EXISTS (
                    SELECT 1 FROM fights fi
                    JOIN events e ON e.id = fi.event_id
                    WHERE e.status = 'upcoming'
                      AND (fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id)
                ) DESC,
                (NULLIF(f.headshot_url, '') IS NOT NULL) DESC,
                f.name
        """
    else:
        sql = f"""
            SELECT f.id, f.name
            FROM fighters f
            WHERE f.name IS NOT NULL AND f.name <> ''
              AND f.espn_id IS NULL
              AND EXISTS (
                    SELECT 1 FROM fights fi
                    JOIN events e ON e.id = fi.event_id
                    WHERE e.status = 'upcoming'
                      AND (fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id)
              )
              {homonym_free}
            ORDER BY f.name
        """
    params: list = []
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def backfill(
    connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    all_scope: bool = False,
    candidate_search: CandidateSearch = search_athlete_candidates,
    sleeper: Callable[[float], None] = time.sleep,
) -> Counter:
    counts: Counter = Counter()
    counts["seeded_from_source"] = seed_from_source_ids(connection, dry_run=dry_run)
    LOGGER.info("Seeded espn_id from source_id: %d", counts["seeded_from_source"])

    session = _build_session(get_settings())
    targets = _get_target_fighters(connection, limit=limit, all_scope=all_scope)
    counts["targets"] = len(targets)
    LOGGER.info(
        "Fighters without espn_id (scope=%s): %d",
        "all" if all_scope else "upcoming", len(targets),
    )

    for idx, (fighter_id, name) in enumerate(targets, 1):
        try:
            candidates = candidate_search(session, name)
        except requests.RequestException as exc:
            counts["search_error"] += 1
            LOGGER.warning("Search failed for id=%d %r: %s", fighter_id, name, exc)
            sleeper(REQUEST_DELAY_SECONDS)
            continue
        sleeper(REQUEST_DELAY_SECONDS)
        picked = pick_unambiguous_athlete(candidates)
        if picked is None:
            counts["unresolved"] += 1
            continue
        espn_id, espn_name = picked
        if dry_run:
            counts["would_update"] += 1
            LOGGER.info("[dry-run] id=%d %r -> espn %s (%r)", fighter_id, name, espn_id, espn_name)
            continue
        try:
            if set_fighter_espn_id(connection, fighter_id, espn_id):
                connection.commit()
                counts["updated"] += 1
            else:
                connection.rollback()
                counts["already_set"] += 1
        except psycopg2.errors.UniqueViolation:
            # El id ya pertenece a otra fila (duplicado sin fusionar): nunca robar.
            connection.rollback()
            counts["conflict"] += 1
            LOGGER.warning(
                "espn_id %s already attached to another fighter — skipping id=%d %r",
                espn_id, fighter_id, name,
            )
        if idx % PROGRESS_EVERY == 0:
            LOGGER.info(
                "Progress %d/%d — updated=%d unresolved=%d conflict=%d",
                idx, len(targets), counts["updated"], counts["unresolved"], counts["conflict"],
            )
    if dry_run:
        connection.rollback()
    return counts


def probe(names: list[str]) -> None:
    """Sin BD: busca cada nombre y muestra candidatos + decisión (QA manual)."""
    session = _build_session(get_settings_for_probe())
    for name in names:
        candidates = search_athlete_candidates(session, name)
        time.sleep(REQUEST_DELAY_SECONDS)
        picked = pick_unambiguous_athlete(candidates)
        print(json.dumps(
            {
                "name": name,
                "candidates": [
                    {"score": round(score, 3), "espn_id": espn_id, "espn_name": espn_name}
                    for score, espn_id, espn_name in candidates[:5]
                ],
                "picked": {"espn_id": picked[0], "espn_name": picked[1]} if picked else None,
            },
            ensure_ascii=False, indent=2,
        ))


def get_settings_for_probe():
    """--probe no necesita BD: settings mínimos aunque falte DATABASE_URL."""
    try:
        return get_settings()
    except RuntimeError:
        from .config import Settings

        return Settings(database_url="", anthropic_api_key=None)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve and store the ESPN athlete id for each fighter (fighters.espn_id)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Search + report, no writes.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters.")
    parser.add_argument("--all", action="store_true", dest="all_scope", help="Whole-table scope (default: upcoming cards + fighters created <45 days ago).")
    parser.add_argument("--probe", nargs="+", default=None, help="No DB: search these names and print candidates as JSON.")
    args = parser.parse_args()
    configure_logging()

    if args.probe:
        probe(args.probe)
        return

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            dry_run=args.dry_run,
            limit=args.limit,
            all_scope=args.all_scope,
        )

    keys = [
        "seeded_from_source", "targets", "updated", "would_update",
        "already_set", "unresolved", "conflict", "search_error",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
