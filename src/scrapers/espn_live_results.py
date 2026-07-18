"""Casi-en-vivo (BE1): fill fight results from ESPN's public scoreboard.

ufcstats publishes results hours-to-a-day after an event, so on fight night the
app shows finished bouts as still upcoming. ESPN's public scoreboard JSON
updates within a minute of each fight ending:

    https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard
    (verified 2026-07; accepts ?dates=YYYYMMDD and YYYYMMDD-YYYYMMDD ranges,
    INDEXED BY US LOCAL DATE — a Saturday-night card past midnight UTC still
    lives under Saturday, hence the default today+yesterday US window.)

Structure per events[].competitions[]: competitors[2] with {id (ESPN athlete
id, same id space as fighters source='espn'), winner (bool), athlete
.displayName, linescores (judges, decisions only)}, status {period,
displayClock, type.completed/state}, and the finish method comes from
competition.details[] entries whose type.text is "Unofficial Winner Decision" |
"Unofficial Winner Submission" | "Unofficial Winner Kotko" (sic, = KO/TKO).

Method codes written (PROVISIONAL, see below): 'Decision' / 'Submission' /
'KO/TKO'. Chosen against the frontend's formatMethod + win-method buckets
(mma-app src/lib/format.ts, fighters.detail.ts): all three render a proper
Spanish label AND bucket correctly ('SUB' or 'U-DEC' would fall to raw text /
'other'). ESPN never distinguishes unanimous/split, so the honest generic
'Decision' is used even when linescores exist.

Data-quality contract with ufcstats (the better source):
  - fill_fight_result COALESCEs with the STORED value winning, so ESPN can
    never overwrite anything ufcstats already wrote;
  - conversely backfill_results treats these three provisional codes as
    upgradeable, so the next daily run replaces them with ufcstats detail
    ('U-DEC', 'KO/TKO - Punches', ...). Draws/NCs are skipped here entirely
    (no winner flag -> nothing written) and left to ufcstats.

Cheap guard for the 10-minute cron: the scoreboard is fetched FIRST and the
process exits without even connecting to the DB when no event of the window
has a fight in/post; it exits after read-only queries when none matches a DB
event (find_existing_event_id, name+date tolerant).

T3-A fase B (2026-07-11): además de los resultados, cada pasada escribe la
fila VIVA de las peleas in/post en live_fight_stats (migración 017, tabla
aparte): estado fino (Walkouts/In Progress/End of Round), asalto, reloj y
las stats de ambos luchadores desde la core API (espn_live_stats). Las
peleas en curso se re-escriben en cada pasada; las terminadas una sola vez
(state='post' con stats == final provisional guardado). Nada de esto toca
fights/fight_stats: la única lectora es la página /en-vivo.

Usage:
    python -m src.scrapers.espn_live_results --dry-run           # report only
    python -m src.scrapers.espn_live_results                     # today+yesterday US
    python -m src.scrapers.espn_live_results --dates 20250628    # explicit date/range
    python -m src.scrapers.espn_live_results --loop              # local: poll every 300s
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import get_settings
from .db import connect
from .espn import _build_exact_name_index, _build_normalized_name_index, _match_fighter
from .espn_live_stats import collect_fight_stats
from .logging_config import configure_logging
from .models import EventRecord
from .repositories.events import find_existing_event_id
from .repositories.fighters import get_all_fighters, get_fighter_id_by_source
from .repositories.fights import fill_fight_result, find_fight_id_by_fighters
from .repositories.live_stats import (
    delete_live_fight_stats,
    get_final_stats_fight_ids,
    prune_live_fight_stats,
    upsert_live_fight_stats,
)

LOGGER = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
ESPN_SOURCE = "espn"
# The scoreboard indexes by US local date; Eastern is ESPN's home timezone and
# every UFC broadcast window is defined against it.
US_EASTERN = ZoneInfo("America/New_York")
LOOP_SLEEP_SECONDS = 300

# competition.details[].type.text -> provisional method code. Codes picked to
# both render and bucket correctly in the frontend (see module docstring) and
# to be recognizably generic so backfill_results upgrades them with ufcstats
# detail on its next run.
METHOD_BY_DETAIL = {
    "unofficial winner decision": "Decision",
    "unofficial winner submission": "Submission",
    "unofficial winner kotko": "KO/TKO",
}
_UNOFFICIAL_WINNER_PREFIX = "unofficial winner"

# The provisional codes this module writes; backfill_results treats a stored
# method equal to one of these as still-fillable from ufcstats.
ESPN_PROVISIONAL_METHODS = tuple(sorted(set(METHOD_BY_DETAIL.values())))

# ESPN marca una pelea cancelada/aplazada a mitad de evento con state='post'
# (completed=False) y un status_type.name como STATUS_CANCELED/STATUS_POSTPONED.
# El endpoint de statistics sirve 42 métricas A CERO antes de que la pelea
# empiece, así que sin este guard una cancelada se sellaría como "Final 0/0".
# Marcadores por subcadena para tolerar variantes (CANCELED/CANCELLED...).
_CANCELLED_STATUS_MARKERS = ("CANCEL", "POSTPON", "FORFEIT", "ABANDON", "SUSPEND")


def is_cancelled_status(status_name: str | None) -> bool:
    """¿El estado fino de ESPN indica una pelea cancelada/aplazada/suspendida?"""
    if not status_name:
        return False
    upper = status_name.upper()
    return any(marker in upper for marker in _CANCELLED_STATUS_MARKERS)


@dataclass(frozen=True)
class LiveFight:
    competition_id: str
    red_espn_id: str | None
    blue_espn_id: str | None
    red_name: str
    blue_name: str
    winner_espn_id: str | None
    completed: bool
    state: str | None
    method: str | None
    end_round: int | None
    end_time: str | None
    # T3-A fase B: estado fino de ESPN para la fila viva (live_fight_stats).
    # En una pelea en curso period/displayClock (arriba, como end_round/
    # end_time) son el asalto y el reloj ACTUALES, no los finales.
    status_name: str | None = None
    status_detail: str | None = None


@dataclass(frozen=True)
class LiveEvent:
    espn_id: str
    name: str
    start_utc: datetime | None
    fights: tuple[LiveFight, ...]


# ----------------------------------------------------------------- parsing


def parse_scoreboard(payload: dict[str, Any]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for event in payload.get("events", []) or []:
        fights = tuple(
            fight
            for competition in event.get("competitions", []) or []
            if (fight := _parse_competition(competition)) is not None
        )
        events.append(
            LiveEvent(
                espn_id=str(event.get("id", "")),
                name=str(event.get("name") or "").strip(),
                start_utc=_parse_utc(event.get("date")),
                fights=fights,
            )
        )
    return events


def _parse_competition(competition: dict[str, Any]) -> LiveFight | None:
    competitors = competition.get("competitors") or []
    if len(competitors) < 2:
        return None
    status = competition.get("status") or {}
    status_type = status.get("type") or {}
    winners = [c for c in competitors[:2] if c.get("winner") is True]
    return LiveFight(
        competition_id=str(competition.get("id", "")),
        red_espn_id=_competitor_id(competitors[0]),
        blue_espn_id=_competitor_id(competitors[1]),
        red_name=_competitor_name(competitors[0]),
        blue_name=_competitor_name(competitors[1]),
        # Exactly one flagged winner, or None (draw / NC / not yet scored).
        winner_espn_id=_competitor_id(winners[0]) if len(winners) == 1 else None,
        completed=bool(status_type.get("completed")),
        state=status_type.get("state"),
        method=method_from_details(competition.get("details") or []),
        end_round=status.get("period") if isinstance(status.get("period"), int) else None,
        end_time=str(status.get("displayClock")) if status.get("displayClock") else None,
        status_name=str(status_type.get("name")) if status_type.get("name") else None,
        status_detail=(
            str(status_type.get("shortDetail") or status_type.get("detail"))
            if (status_type.get("shortDetail") or status_type.get("detail"))
            else None
        ),
    )


def method_from_details(details: list[dict[str, Any]]) -> str | None:
    """Provisional method from the "Unofficial Winner <X>" details entry."""
    for detail in details:
        text = ((detail.get("type") or {}).get("text") or "").strip().lower()
        if not text.startswith(_UNOFFICIAL_WINNER_PREFIX):
            continue
        method = METHOD_BY_DETAIL.get(text)
        if method is None:
            LOGGER.warning("Unmapped ESPN winner detail %r; leaving method NULL", text)
        return method
    return None


def _competitor_id(competitor: dict[str, Any]) -> str | None:
    value = competitor.get("id")
    return str(value) if value not in (None, "") else None


def _competitor_name(competitor: dict[str, Any]) -> str:
    athlete = competitor.get("athlete") or {}
    return str(athlete.get("displayName") or athlete.get("fullName") or "").strip()


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------- matching


def default_dates_window(now: datetime | None = None) -> str:
    """Today AND yesterday in US Eastern, covering the midnight-UTC window."""
    now_us = (now or datetime.now(timezone.utc)).astimezone(US_EASTERN)
    today = now_us.date()
    yesterday = today - timedelta(days=1)
    return f"{yesterday:%Y%m%d}-{today:%Y%m%d}"


def candidate_event_dates(start_utc: datetime | None) -> list[date]:
    """DB event_date candidates for an ESPN event start: US-Eastern then UTC.

    A 03:00Z Sunday main card is Saturday's event for ufc.com/ufcstats, so the
    Eastern date is tried first; both are tried because our sources are not
    perfectly consistent around midnight.
    """
    if start_utc is None:
        return []
    eastern_date = start_utc.astimezone(US_EASTERN).date()
    utc_date = start_utc.astimezone(timezone.utc).date()
    return [eastern_date] if eastern_date == utc_date else [eastern_date, utc_date]


def match_db_event(connection, event: LiveEvent, promotion_id: int) -> int | None:
    for event_date in candidate_event_dates(event.start_utc):
        event_id = find_existing_event_id(
            connection,
            EventRecord(
                name=event.name,
                event_date=event_date,
                location=None,
                promotion_id=promotion_id,
            ),
        )
        if event_id is not None:
            return event_id
    return None


def events_worth_processing(events: list[LiveEvent]) -> list[LiveEvent]:
    """Events with at least one fight in progress or finished (guard, step 1)."""
    return [
        event
        for event in events
        if any(fight.state in ("in", "post") or fight.completed for fight in event.fights)
    ]


# ---------------------------------------------------------------- pipeline


def _resolve_fighter(connection, espn_id, name, exact_index, normalized_index, counts) -> int | None:
    """DB fighter id: (source='espn', source_id) first, fuzzy name @0.92 second."""
    if espn_id:
        fighter_id = get_fighter_id_by_source(connection, ESPN_SOURCE, espn_id)
        if fighter_id is not None:
            counts["fighters_by_source"] += 1
            return fighter_id
    if name:
        match = _match_fighter(name, exact_index, normalized_index)
        if match is not None:
            counts["fighters_by_name"] += 1
            return match.id
    return None


def _resolve_bout(
    connection, event_id, fight: LiveFight, exact_index, normalized_index, counts
) -> tuple[int, int, int] | None:
    """(red_db_id, blue_db_id, fight_id) de una pelea ESPN, o None sin match."""
    red_id = _resolve_fighter(
        connection, fight.red_espn_id, fight.red_name, exact_index, normalized_index, counts
    )
    blue_id = _resolve_fighter(
        connection, fight.blue_espn_id, fight.blue_name, exact_index, normalized_index, counts
    )
    if red_id is None or blue_id is None:
        counts["fighters_unresolved"] += 1
        LOGGER.info(
            "  unresolved fighters for %s vs %s (red_id=%s blue_id=%s)",
            fight.red_name, fight.blue_name, red_id, blue_id,
        )
        return None
    fight_id = find_fight_id_by_fighters(connection, event_id, red_id, blue_id)
    if fight_id is None:
        counts["fights_unmatched"] += 1
        LOGGER.info("  no DB fight on event %s for %s vs %s", event_id, fight.red_name, fight.blue_name)
        return None
    return red_id, blue_id, fight_id


def _process_fight(
    connection, event_id, fight: LiveFight, exact_index, normalized_index, counts, *, dry_run: bool
) -> None:
    bout = _resolve_bout(connection, event_id, fight, exact_index, normalized_index, counts)
    if bout is None:
        return
    red_id, blue_id, fight_id = bout
    if fight.winner_espn_id == fight.red_espn_id:
        winner_id, winner_name = red_id, fight.red_name
    elif fight.winner_espn_id == fight.blue_espn_id:
        winner_id, winner_name = blue_id, fight.blue_name
    else:
        # Draw / NC / winner not scored yet: writing method with a NULL winner
        # would fabricate a draw in the app, so leave the bout to ufcstats.
        counts["fights_no_winner"] += 1
        LOGGER.info("  no winner flagged for %s vs %s; skipping", fight.red_name, fight.blue_name)
        return
    LOGGER.info(
        "  LIVE %s def. %s — %s R%s %s (fight %s%s)",
        winner_name,
        fight.blue_name if winner_id == red_id else fight.red_name,
        fight.method or "method TBD",
        fight.end_round,
        fight.end_time,
        fight_id,
        ", dry-run" if dry_run else "",
    )
    if dry_run:
        counts["fights_updated"] += 1
        return
    if fill_fight_result(connection, fight_id, winner_id, fight.method, fight.end_round, fight.end_time):
        counts["fights_updated"] += 1
    else:
        counts["fights_already_filled"] += 1


def _process_live_stats(
    connection, session, event: LiveEvent, event_id, exact_index, normalized_index,
    counts, *, dry_run: bool
) -> None:
    """T3-A fase B: fila viva (estado fino + stats) por pelea in/post.

    Verificado con muestras reales (UFC 329): las stats se actualizan EN
    DIRECTO durante el asalto. Las peleas en curso se piden en cada pasada;
    una terminada deja de pedirse solo cuando su total queda SELLADO (is_final,
    con stats frescas de ambos lados) — get_final_stats_fight_ids. Si el fetch
    de cierre falla o llega parcial, is_final=False y se reintenta barato. Las
    canceladas/aplazadas se borran y se saltan. El matching reutiliza el mismo
    _resolve_bout que los resultados, con un Counter aparte para no inflar
    los contadores de la pasada de resultados."""
    candidates = [
        fight for fight in event.fights
        if fight.completed or fight.state in ("in", "post")
    ]
    if not candidates:
        return
    resolve_counts: Counter = Counter()
    resolved: list[tuple[LiveFight, int, int, int]] = []
    for fight in candidates:
        bout = _resolve_bout(
            connection, event_id, fight, exact_index, normalized_index, resolve_counts
        )
        if bout is None:
            counts["live_stats_unresolved"] += 1
            continue
        resolved.append((fight, *bout))
    if not resolved:
        return
    final_ids = get_final_stats_fight_ids(connection, [item[3] for item in resolved])
    for fight, red_id, blue_id, fight_id in resolved:
        # Cancelada/aplazada a mitad de evento: ni la sellamos ni la pintamos.
        # Borramos cualquier fila (p. ej. walkouts escritos antes de anularla).
        if is_cancelled_status(fight.status_name):
            counts["live_stats_cancelled"] += 1
            if not dry_run:
                delete_live_fight_stats(connection, fight_id)
            continue
        if fight_id in final_ids:
            counts["live_stats_skipped_final"] += 1
            continue
        is_post = fight.completed or fight.state == "post"
        state = "post" if is_post else "in"
        athletes = [(fight.red_espn_id, red_id), (fight.blue_espn_id, blue_id)]
        stats = collect_fight_stats(
            session, event.espn_id, fight.competition_id, athletes,
        )
        # is_final: solo sellamos el total cuando, ya en 'post', tenemos stats
        # FRESCAS de cada lado que ESPN puede servir en ESTA pasada (con espn
        # id). Un fallo de red o un lado que llega tarde deja is_final=False y
        # se reintenta barato; un lado sin espn id nunca llega y no bloquea.
        fetchable_ids = [db_id for espn_id, db_id in athletes if espn_id]
        have_all_fresh = stats is not None and all(
            str(db_id) in stats for db_id in fetchable_ids
        )
        is_final = is_post and bool(fetchable_ids) and have_all_fresh
        LOGGER.info(
            "  LIVE-STATS %s vs %s [%s %s R%s %s] sides=%d final=%s (fight %s%s)",
            fight.red_name, fight.blue_name, state, fight.status_detail,
            fight.end_round, fight.end_time, len(stats or {}), is_final, fight_id,
            ", dry-run" if dry_run else "",
        )
        if dry_run:
            counts["live_stats_written"] += 1
            continue
        upsert_live_fight_stats(
            connection, fight_id, state, fight.status_name, fight.status_detail,
            fight.end_round, fight.end_time, stats, is_final,
        )
        counts["live_stats_written"] += 1


def process_events(
    connection, events: list[LiveEvent], promotion_id: int, *, dry_run: bool,
    stats_session: requests.Session | None = None,
) -> Counter:
    """Map live ESPN events onto DB events/fights and fill finished results.

    Con stats_session (T3-A fase B) añade, por evento, la pasada de stats en
    vivo sobre live_fight_stats; sin ella (tests, llamadas antiguas) se
    comporta exactamente como antes."""
    counts: Counter = Counter()
    fighters = get_all_fighters(connection)
    exact_index = _build_exact_name_index(fighters)
    normalized_index = _build_normalized_name_index(fighters)
    for event in events:
        event_id = match_db_event(connection, event, promotion_id)
        if event_id is None:
            counts["events_unmatched"] += 1
            LOGGER.info("No DB event for ESPN %r (%s)", event.name, event.start_utc)
            continue
        counts["events_matched"] += 1
        LOGGER.info("ESPN %r -> DB event %s | %d fights", event.name, event_id, len(event.fights))
        try:
            for fight in event.fights:
                if not fight.completed:
                    counts["fights_pending"] += 1
                    continue
                _process_fight(
                    connection, event_id, fight, exact_index, normalized_index, counts,
                    dry_run=dry_run,
                )
            if stats_session is not None:
                _process_live_stats(
                    connection, stats_session, event, event_id,
                    exact_index, normalized_index, counts, dry_run=dry_run,
                )
            if dry_run:
                connection.rollback()
            else:
                connection.commit()
        except Exception:  # noqa: BLE001 - keep going to the next event
            connection.rollback()
            counts["event_errors"] += 1
            LOGGER.exception("Failed to process ESPN event %r", event.name)
    return counts


def fetch_scoreboard(session: requests.Session, dates: str) -> dict[str, Any]:
    response = session.get(SCOREBOARD_URL, params={"dates": dates}, timeout=30)
    response.raise_for_status()
    return response.json()


def _build_session(settings) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": settings.user_agent.replace("ufcstats.com", "espn.com"),
        }
    )
    return session


def refresh_live_results(dates: str | None = None, dry_run: bool = False) -> Counter:
    settings = get_settings()
    window = dates or default_dates_window()
    session = _build_session(settings)
    payload = fetch_scoreboard(session, window)
    events = parse_scoreboard(payload)
    live_events = events_worth_processing(events)
    counts: Counter = Counter(
        {"scoreboard_events": len(events), "live_events": len(live_events)}
    )
    if not live_events:
        # Cheap guard for the 10-minute cron: nothing in/post in the window,
        # exit without opening a DB connection.
        LOGGER.info("Guard: no ESPN event in/post for dates=%s; DB untouched.", window)
        return counts
    with connect(settings.database_url) as connection:
        counts.update(
            process_events(
                connection, live_events, settings.promotion_id_ufc,
                dry_run=dry_run, stats_session=session,
            )
        )
        if not dry_run:
            # T3-A fase B: la tabla viva se poda sola (la web solo consulta
            # el evento del día). Commit propio: independiente de los eventos.
            pruned = prune_live_fight_stats(connection)
            connection.commit()
            if pruned:
                counts["live_stats_pruned"] = pruned
    return counts


SUMMARY_KEYS = [
    "scoreboard_events", "live_events", "events_matched", "events_unmatched",
    "fights_updated", "fights_already_filled", "fights_pending",
    "fights_no_winner", "fights_unmatched", "fighters_unresolved", "event_errors",
    "live_stats_written", "live_stats_skipped_final", "live_stats_unresolved",
    "live_stats_cancelled",
]


def run_bounded_loop(
    dates: str | None,
    dry_run: bool,
    duration_minutes: int,
    interval_seconds: int,
) -> Counter:
    """T3-A: ventana de evento en CI — una pasada cada interval_seconds hasta
    agotar duration_minutes (deadline con time.monotonic, patrón de
    capture_live_samples). GitHub retrasa los crons */10, así que un único job
    largo es lo que garantiza la cadencia durante la noche de evento.

    Guardas: si la PRIMERA pasada ve el scoreboard vacío no hay evento en la
    ventana — salida barata y en verde (espíritu del guard del cron). Un fallo
    de red/BD en una pasada se loguea y NO tumba la ventana entera (cada pasada
    es idempotente: COALESCE, el valor almacenado gana)."""
    deadline = time.monotonic() + duration_minutes * 60
    totals: Counter = Counter()
    iterations = 0
    failures = 0
    while True:
        iterations += 1
        try:
            counts = refresh_live_results(dates=dates, dry_run=dry_run)
            totals.update(counts)
            print(json.dumps({key: counts.get(key, 0) for key in SUMMARY_KEYS}), flush=True)
            if iterations == 1 and counts.get("scoreboard_events", 0) == 0:
                LOGGER.info("Bounded loop: scoreboard vacío en la 1ª pasada; sin evento en la ventana.")
                break
        except Exception:  # noqa: BLE001 - una pasada mala no mata la ventana
            failures += 1
            LOGGER.exception(
                "Bounded loop: falló la pasada %s (%s fallos acumulados)", iterations, failures
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(max(1, interval_seconds))
    print(json.dumps({"loop_iterations": iterations, "loop_failures": failures}), flush=True)
    return totals


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Fill near-live fight results from ESPN's scoreboard.")
    parser.add_argument(
        "--dates", default=None,
        help="ESPN dates filter (YYYYMMDD or YYYYMMDD-YYYYMMDD, US local dates). "
             "Default: today and yesterday in US Eastern.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Match + report, no writes.")
    parser.add_argument(
        "--loop", action="store_true",
        help=f"Poll forever every {LOOP_SLEEP_SECONDS}s (local fight-night use).",
    )
    parser.add_argument(
        "--duration-minutes", type=int, default=None,
        help="Bucle ACOTADO de ventana de evento (CI): pasadas hasta agotar los "
             "minutos indicados y salir. Tiene prioridad sobre --loop.",
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=20,
        help="Segundos entre pasadas del bucle acotado (con --duration-minutes). "
             "Una pasada es barata (~3 GETs durante un combate en directo), asi "
             "que 20s da sensacion de directo sin martillear ESPN.",
    )
    args = parser.parse_args()
    if args.duration_minutes is not None:
        run_bounded_loop(
            dates=args.dates,
            dry_run=args.dry_run,
            duration_minutes=args.duration_minutes,
            interval_seconds=args.interval_seconds,
        )
        return
    while True:
        counts = refresh_live_results(dates=args.dates, dry_run=args.dry_run)
        print(json.dumps({key: counts.get(key, 0) for key in SUMMARY_KEYS}, indent=2))
        if not args.loop:
            break
        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
