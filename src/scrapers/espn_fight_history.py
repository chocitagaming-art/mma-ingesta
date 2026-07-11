"""Importa el historial COMPLETO de peleas (Bellator/regionales) desde ESPN.

Segundo paso del pipeline S3-G. Para cada luchador con fighters.espn_id, un
solo GET al endpoint common/v3 devuelve su carrera entera (eventsMap). Las
peleas NO-UFC se upsertan en la tabla APARTE fight_history_espn (migración
016) que la ficha fusiona solo al pintar el historial. `fights` no se toca.

GUARDAS (ver cabecera de la migración 016):
  - La liga UFC de ESPN (l:3321) se SALTA SIEMPRE: esas peleas ya viven en
    `fights` vía ufcstats y duplicarlas rompería stats/ML. Defensa extra: un
    evento cuyo nombre empiece por UFC/TUF se salta aunque venga con otra liga.
  - Guard de identidad: si el displayName del atleta que devuelve ESPN no
    matchea el nombre en BD (umbral de identidad 0.92), NO se importa nada
    (un espn_id mal resuelto no puede inyectar la carrera de un extraño).
  - Solo entran peleas con resultado (W/L/D o no-contest); las programadas o
    sin datos se ignoran.
  - Métodos normalizados al formato canónico de ufcstats que ya viaja por la
    app ("U-DEC", "KO/TKO - Punches", "SUB - Armbar", "CNC", "DQ"...).
  - fighters.espn_history_checked_at se sella tras cada barrido con éxito:
    el cron semanal solo re-visita fichas nuevas o en cartelera próxima.

Usage:
    python -m src.scrapers.espn_fight_history --probe-id 4275020   # sin BD
    python -m src.scrapers.espn_fight_history --dry-run --limit 5
    python -m src.scrapers.espn_fight_history            # nuevos + upcoming
    python -m src.scrapers.espn_fight_history --all      # barrido completo
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .config import get_settings
from .db import connect
from .enrich_ranked import _build_session
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, fold_ratio
from .repositories.espn_history import (
    EspnHistoryRecord,
    get_espn_id_to_fighter_map,
    stamp_history_checked,
    upsert_espn_history,
)

LOGGER = logging.getLogger(__name__)

ATHLETE_CAREER_URL = "https://site.web.api.espn.com/apis/common/v3/sports/mma/athletes/{espn_id}"
REQUEST_DELAY_SECONDS = 0.35
PROGRESS_EVERY = 50

# Convención de fecha-de-evento del repo (ver candidate_event_dates en
# espn_live_results.py): un main card del sábado noche US llega de ESPN como
# domingo en UTC; la fecha "del evento" es la del Este de EE.UU.
US_EASTERN = ZoneInfo("America/New_York")

LEAGUE_UFC = "3321"
LEAGUE_LABELS = {"3323": "Bellator"}
_UID_RE = re.compile(r"l:(\d+)~e:(\d+)~c:(\d+)")
_TRAILING_NUMBER_RE = re.compile(r"\s+\d+$")
_PARENS_RE = re.compile(r"\(([^)]+)\)")

# Prefijos de evento que delatan una pelea UFC aunque ESPN la liste bajo otra
# liga (defensa extra contra duplicados; el filtro principal es l:3321).
# CON LÍMITE DE PALABRA (\b): "TUF 33" sí es UFC, pero "Tuff-N-Uff 143" es una
# promoción regional real de Las Vegas y "UFCF" una noventera — sin \b ambas
# se descartaban en silencio (hallazgo de la revisión adversarial).
_UFC_NAME_RE = re.compile(r"^(?:UFC|THE ULTIMATE FIGHTER|TUF|DANA WHITE)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedBout:
    """Una pelea no-UFC de la carrera ESPN, ya normalizada para persistir."""

    espn_competition_id: str
    espn_event_id: str
    league_id: str
    promotion: str
    event_name: str | None
    event_date: date | None
    opponent_name: str | None
    opponent_espn_id: str | None
    result: str  # win / loss / draw / nc
    method: str | None
    end_round: int | None
    end_time: str | None
    is_title_fight: bool


# fetcher(session, espn_id) -> payload dict | None (404). Inyectable en tests.
Fetcher = Callable[[requests.Session, str], "dict | None"]

# Target: (fighter_id, name, espn_id, seeded). seeded=True cuando el espn_id
# proviene del source_id de una fila source='espn': el enlace id->atleta es
# correcto POR CONSTRUCCIÓN, así que el guard de nombre no aplica (ESPN
# renombra atletas — caso real "Zachary Reese" -> "Zach Reese" — y el guard
# solo puede producir falsos positivos ahí).
Target = tuple[int, str, str, bool]


def promotion_label(league_id: str, event_name: str | None, short_name: str | None) -> str:
    """Etiqueta corta para el badge: liga conocida > prefijo del nombre del
    evento (sin el ordinal final) > shortName > 'Regional'."""
    known = LEAGUE_LABELS.get(league_id)
    if known:
        return known
    base = ""
    if event_name and ":" in event_name:
        base = event_name.split(":", 1)[0]
    elif short_name:
        base = short_name
    elif event_name:
        base = event_name
    # Sub-marcas tras " - " no son la promoción ("Tech-Krep FC - Ermak Prime
    # Challenge" -> "Tech-Krep FC"); además desbordan el badge de la ficha.
    base = base.split(" - ", 1)[0]
    base = _TRAILING_NUMBER_RE.sub("", base.strip()).strip()
    return base or "Regional"


def canonical_method(result_token: str | None, display_name: str | None) -> str | None:
    """Token de ESPN (status.result.name) -> formato canónico de ufcstats.

    Los tokens reales observados: decision---unanimous, unanimous-decision,
    decision---split, decision---majority, ko, tko, kotko,
    tko---doctors-stoppage, tko-doctor-stoppage, submission,
    submission-anaconda-choke, no-contest. Lo desconocido cae al displayName
    legible de ESPN (nunca se inventa un código)."""
    token = (result_token or "").strip().lower()
    display = (display_name or "").strip()
    if not token and not display:
        return None
    if token == "no-contest":
        return "CNC"
    if token in ("dq", "disqualification"):
        return "DQ"
    if "decision" in token:
        if "unanimous" in token:
            return "U-DEC"
        if "split" in token:
            return "S-DEC"
        if "majority" in token:
            return "M-DEC"
        return display or "Decision"
    if token.startswith("submission"):
        detail = ""
        parens = _PARENS_RE.search(display)
        if parens:
            detail = parens.group(1).strip()
        else:
            detail = token[len("submission"):].strip("-").replace("-", " ").title()
        return f"SUB - {detail}" if detail else "SUB"
    if token in ("ko", "tko", "kotko"):
        return "KO/TKO"
    if token.startswith(("kotko", "tko", "ko")):
        parens = _PARENS_RE.search(display)
        if parens:
            return f"KO/TKO - {parens.group(1).strip()}"
        detail = re.sub(r"^(kotko|tko|ko)", "", token).strip("-").replace("-", " ").title()
        return f"KO/TKO - {detail}" if detail else "KO/TKO"
    return display or None


def _parse_game_date(raw: object) -> date | None:
    """gameDate es un timestamp UTC del inicio de la cartelera. Truncarlo tal
    cual fechaba +1 día las carteleras nocturnas americanas (un sábado 21:00 ET
    llega como domingo 02:00Z). Misma convención que candidate_event_dates en
    espn_live_results: la fecha 'del evento' es la del Este de EE.UU."""
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    if len(raw) == 10:
        # Solo fecha, sin hora: no hay nada que convertir.
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(US_EASTERN).date()


def parse_career(payload: dict) -> tuple[list[ParsedBout], Counter]:
    """eventsMap -> peleas no-UFC con resultado. Devuelve (bouts, contadores
    de lo saltado) para que el resumen del run explique cada descarte."""
    counts: Counter = Counter()
    bouts: list[ParsedBout] = []
    events = payload.get("events")
    events_map = payload.get("eventsMap") or {}
    uids = [uid for uid in (events or []) if isinstance(uid, str)] or list(events_map.keys())

    for uid in uids:
        entry = events_map.get(uid)
        if not isinstance(entry, dict):
            counts["missing_entry"] += 1
            continue
        uid_match = _UID_RE.search(uid)
        if not uid_match:
            counts["bad_uid"] += 1
            continue
        league_id, espn_event_id, espn_competition_id = uid_match.groups()
        if league_id == LEAGUE_UFC:
            counts["skipped_ufc"] += 1
            continue
        event_name = str(entry.get("name") or "").strip() or None
        short_name = str(entry.get("shortName") or "").strip() or None
        promotion = promotion_label(league_id, event_name, short_name)
        if _UFC_NAME_RE.match(promotion) or _UFC_NAME_RE.match(event_name or ""):
            counts["skipped_ufc_named"] += 1
            continue

        status = entry.get("status") or {}
        result_obj = status.get("result") or {}
        result_token = str(result_obj.get("name") or "").strip().lower()
        game_result = str(entry.get("gameResult") or "").strip().upper()
        if result_token == "no-contest":
            result = "nc"
        elif game_result == "W":
            result = "win"
        elif game_result == "L":
            result = "loss"
        elif game_result == "D":
            result = "draw"
        else:
            counts["skipped_no_result"] += 1
            continue

        period = status.get("period")
        end_round = int(period) if isinstance(period, int) and period > 0 else None
        clock = str(status.get("displayClock") or "").strip()
        end_time = clock if re.fullmatch(r"\d+:\d{2}", clock) else None
        opponent = entry.get("opponent") or {}
        opponent_espn_id = str(opponent.get("id") or "").strip() or None
        opponent_name = str(opponent.get("displayName") or "").strip() or None

        bouts.append(
            ParsedBout(
                espn_competition_id=espn_competition_id,
                espn_event_id=espn_event_id,
                league_id=league_id,
                promotion=promotion,
                event_name=event_name,
                event_date=_parse_game_date(entry.get("gameDate")),
                opponent_name=opponent_name,
                opponent_espn_id=opponent_espn_id,
                result=result,
                method=canonical_method(result_token, str(result_obj.get("displayName") or "")),
                end_round=end_round,
                end_time=end_time,
                is_title_fight=bool(entry.get("titleFight")),
            )
        )
    counts["parsed"] = len(bouts)
    return bouts, counts


def fetch_career(session: requests.Session, espn_id: str) -> dict | None:
    """GET de la carrera completa. None en 404 (atleta sin página, esperado);
    el resto de errores HTTP/red suben al caller (cuentan como fetch_error)."""
    response = session.get(ATHLETE_CAREER_URL.format(espn_id=espn_id), timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _athlete_page_name(payload: dict) -> str | None:
    athlete = payload.get("athlete") or {}
    name = str(athlete.get("displayName") or athlete.get("fullName") or "").strip()
    return name or None


def _get_target_fighters(
    connection, limit: int | None = None, all_scope: bool = False
) -> list[Target]:
    """Luchadores con espn_id. Default (cron): nunca barridos O en cartelera
    próxima (los recién resueltos entran por el sello NULL). --all: todos,
    priorizados como enrich_facts para que un pase parcial cubra lo visible."""
    if all_scope:
        sql = """
            SELECT f.id, f.name, f.espn_id,
                (f.source = 'espn' AND f.espn_id = f.source_id) AS seeded
            FROM fighters f
            WHERE f.espn_id IS NOT NULL
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
        sql = """
            SELECT f.id, f.name, f.espn_id,
                (f.source = 'espn' AND f.espn_id = f.source_id) AS seeded
            FROM fighters f
            WHERE f.espn_id IS NOT NULL
              AND (
                f.espn_history_checked_at IS NULL
                OR EXISTS (
                    SELECT 1 FROM fights fi
                    JOIN events e ON e.id = fi.event_id
                    WHERE e.status = 'upcoming'
                      AND (fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id)
                )
              )
            ORDER BY f.name
        """
    params: list = []
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [
            (int(row[0]), str(row[1]), str(row[2]), bool(row[3]))
            for row in cursor.fetchall()
        ]


def backfill(
    connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    all_scope: bool = False,
    fetcher: Fetcher = fetch_career,
    sleeper: Callable[[float], None] = time.sleep,
) -> Counter:
    session = _build_session(get_settings())
    counts: Counter = Counter()
    targets = _get_target_fighters(connection, limit=limit, all_scope=all_scope)
    counts["targets"] = len(targets)
    LOGGER.info(
        "Fighters with espn_id to sweep (scope=%s): %d",
        "all" if all_scope else "new+upcoming", len(targets),
    )
    espn_to_fighter = get_espn_id_to_fighter_map(connection)

    for idx, (fighter_id, name, espn_id, seeded) in enumerate(targets, 1):
        try:
            payload = fetcher(session, espn_id)
        except (requests.RequestException, ValueError) as exc:
            counts["fetch_error"] += 1
            LOGGER.warning("Career fetch failed for id=%d %r (espn %s): %s", fighter_id, name, espn_id, exc)
            sleeper(REQUEST_DELAY_SECONDS)
            continue
        sleeper(REQUEST_DELAY_SECONDS)

        if payload is None:
            counts["no_page"] += 1
            if not dry_run:
                stamp_history_checked(connection, fighter_id)
                connection.commit()
            continue

        page_name = _athlete_page_name(payload)
        if page_name is not None and fold_ratio(name, page_name) < IDENTITY_THRESHOLD:
            if seeded:
                # El id vino del source_id de la propia fila ESPN: el enlace es
                # correcto por construcción y la divergencia es un renombrado
                # del lado de ESPN ("Zachary Reese" -> "Zach Reese"). Importar.
                LOGGER.info(
                    "Seeded id=%d %r renamed on ESPN to %r — importing anyway",
                    fighter_id, name, page_name,
                )
            else:
                counts["name_mismatch"] += 1
                LOGGER.warning(
                    "Identity mismatch for id=%d %r: ESPN %s renders %r — skipping import",
                    fighter_id, name, espn_id, page_name,
                )
                continue

        bouts, parse_counts = parse_career(payload)
        for key in ("skipped_ufc", "skipped_ufc_named", "skipped_no_result", "missing_entry", "bad_uid"):
            counts[key] += parse_counts[key]

        if dry_run:
            counts["would_write"] += len(bouts)
            LOGGER.info(
                "[dry-run] id=%d %r: %d peleas no-UFC (de %d en ESPN) | %s",
                fighter_id, name, len(bouts), len(payload.get("eventsMap") or {}),
                ", ".join(sorted({bout.promotion for bout in bouts})) or "-",
            )
        else:
            written = 0
            for bout in bouts:
                opponent_fighter_id = (
                    espn_to_fighter.get(bout.opponent_espn_id)
                    if bout.opponent_espn_id
                    else None
                )
                if opponent_fighter_id == fighter_id:
                    opponent_fighter_id = None
                record = EspnHistoryRecord(
                    fighter_id=fighter_id,
                    espn_competition_id=bout.espn_competition_id,
                    espn_event_id=bout.espn_event_id,
                    league_id=bout.league_id,
                    promotion=bout.promotion,
                    event_name=bout.event_name,
                    event_date=bout.event_date,
                    opponent_name=bout.opponent_name,
                    opponent_espn_id=bout.opponent_espn_id,
                    opponent_fighter_id=opponent_fighter_id,
                    result=bout.result,
                    method=bout.method,
                    end_round=bout.end_round,
                    end_time=bout.end_time,
                    is_title_fight=bout.is_title_fight,
                )
                if upsert_espn_history(connection, record):
                    written += 1
            stamp_history_checked(connection, fighter_id)
            connection.commit()
            counts["written"] += written
            if bouts:
                counts["fighters_with_history"] += 1

        if idx % PROGRESS_EVERY == 0:
            LOGGER.info(
                "Progress %d/%d — written=%d fighters_with_history=%d no_page=%d "
                "fetch_error=%d name_mismatch=%d",
                idx, len(targets), counts["written"], counts["fighters_with_history"],
                counts["no_page"], counts["fetch_error"], counts["name_mismatch"],
            )
    if dry_run:
        connection.rollback()
    return counts


def probe(espn_ids: list[str]) -> None:
    """Sin BD: descarga y parsea carreras y muestra el resultado (QA manual)."""
    from .resolve_espn_ids import get_settings_for_probe

    session = _build_session(get_settings_for_probe())
    for espn_id in espn_ids:
        payload = fetch_career(session, espn_id)
        time.sleep(REQUEST_DELAY_SECONDS)
        if payload is None:
            print(json.dumps({"espn_id": espn_id, "resolved": False}))
            continue
        bouts, counts = parse_career(payload)
        print(json.dumps(
            {
                "espn_id": espn_id,
                "athlete": _athlete_page_name(payload),
                "total_in_espn": len(payload.get("eventsMap") or {}),
                "skipped_ufc": counts["skipped_ufc"] + counts["skipped_ufc_named"],
                "skipped_no_result": counts["skipped_no_result"],
                "non_ufc_bouts": [
                    {
                        "date": bout.event_date.isoformat() if bout.event_date else None,
                        "promotion": bout.promotion,
                        "event": bout.event_name,
                        "opponent": bout.opponent_name,
                        "result": bout.result,
                        "method": bout.method,
                        "round": bout.end_round,
                        "time": bout.end_time,
                        "title": bout.is_title_fight,
                    }
                    for bout in bouts
                ],
            },
            ensure_ascii=False, indent=2,
        ))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import full non-UFC fight history from ESPN into fight_history_espn."
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch + parse + report, no writes.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters.")
    parser.add_argument("--all", action="store_true", dest="all_scope", help="Whole-table scope (default: never-swept fighters + upcoming cards).")
    parser.add_argument("--probe-id", nargs="+", default=None, dest="probe_ids", help="No DB: fetch these ESPN athlete ids and print parsed careers as JSON.")
    args = parser.parse_args()
    configure_logging()

    if args.probe_ids:
        probe(args.probe_ids)
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
        "targets", "written", "would_write", "fighters_with_history", "no_page",
        "skipped_ufc", "skipped_ufc_named", "skipped_no_result",
        "name_mismatch", "fetch_error", "missing_entry", "bad_uid",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
