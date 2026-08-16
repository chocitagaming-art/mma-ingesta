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
from .espn import (
    _build_exact_name_index,
    _build_normalized_name_index,
    _match_fighter,
    build_espn_session,
)
from .espn_live_stats import collect_fight_stats
from .logging_config import configure_logging
from .models import EventRecord
from .repositories.events import find_existing_event_id
from .repositories.fighters import get_all_fighters, get_fighter_id_by_source
from .repositories.fights import fill_fight_result, find_fight_id_by_fighters
from .repositories.live_stats import (
    delete_live_fight_stat_samples,
    delete_live_fight_stats,
    get_final_stats_fight_ids,
    insert_live_fight_stat_sample,
    last_in_progress_clock,
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


# Un asalto de MMA dura 5 minutos, sin excepción en UFC.
ROUND_SECONDS = 300


def _clock_seconds(clock: str | None) -> int | None:
    """'2:17' -> 137. None para el guion, el vacío y cualquier cosa rara."""
    if not clock or ":" not in clock:
        return None
    minutes, _, seconds = clock.partition(":")
    if not (minutes.isdigit() and len(seconds) == 2 and seconds.isdigit()):
        return None
    total = int(minutes) * 60 + int(seconds)
    return total if 0 <= total <= ROUND_SECONDS else None


def elapsed_end_time(
    clock: str | None, method: str | None, previous_clock: str | None
) -> str | None:
    """Tiempo TRANSCURRIDO del asalto final, a partir del reloj de ESPN.

    `fights.end_time` es el minuto en que se acabó el combate, y de él salen los
    segundos peleados que alimentan golpes por minuto, duraciones y el radar.
    Pero el reloj de ESPN es una CUENTA ATRÁS, así que guardarlo crudo escribe
    el complemento: el 25-jul-2026 el estelar quedó en 2:17 cuando fue 2:41, y
    8 de los 12 combates del 1062 salieron invertidos a producción.

    Antes se notaba menos porque el bucle iba a 120 s y ESPN tenía tiempo de
    publicar el oficial; a 20 s sellamos en la primera pasada 'post' y
    congelamos la cuenta atrás.

    NO se puede invertir a ciegas: en las decisiones ESPN sí manda el
    transcurrido ('5:00'), y por eso 4 de los 12 estaban bien.

    EL DISCRIMINADOR ES LA HIPÓTESIS MÁS CERCANA a `previous_clock`, el último
    reloj visto con la pelea EN CURSO. El par (reloj de 'post', último de 'in')
    admite dos lecturas y cada una deja un residuo: si ESPN ya sirviera el
    transcurrido el desfase sería |seconds - (300 - previous)|, y si hubiera
    congelado la cuenta atrás sería |seconds - previous|. Gana la pequeña.

    Antes se exigía IGUALDAD EXACTA con `previous_clock`, y por ahí se coló el
    UFC 330: la 15318 congeló 3:18 en las muestras 'in' y publicó 3:24 en la
    primera 'post' — ESPN corrigiéndose a sí mismo 6 segundos. Esa deriva
    bastaba para darlo por tiempo oficial, y se selló 3:24 habiendo sido 1:36
    (108 s de error). No fue un accidente: la misma deriva, de 1 s, ya había
    invertido la 13925, la 14231 y la 12845 en eventos anteriores. En las cuatro
    lo tapó ufcstats al consolidar, no el código.

    NO vale una tolerancia fija ("si difiere en menos de 10 s está congelado"):
    rompe el patrón Steveson del 19-jul, donde el congelado (2:26) y el oficial
    (2:31) se llevan 5 segundos y el bueno es el oficial. Comparar las dos
    hipótesis ENTRE SÍ lo resuelve; compararlas contra un umbral inventado, no.

    Se probó también con "menor o igual que el anterior" y estaba mal: horas
    después del evento ESPN ya sirve el oficial, y un valor pequeño y legítimo
    (la 12865 acabó en 1:12) se tomaba por cuenta atrás y se invertía a 3:48.

    Sin historial NO se adivina: se devuelve None y lo rellena ufcstats. La
    suposición contraria ("es cuenta atrás, que es lo que ESPN sirve en vivo")
    se midió el 16-ago-2026 y falló 12 de 12. Ver el comentario de la rama.

    Es una APROXIMACIÓN de 1-2 segundos: el reloj se congela cuando ESPN
    registra el final, no cuando lo canta el árbitro. ufcstats la sustituye por
    la oficial al consolidar (`backfill_results` deja ganar al valor nuevo), así
    que este valor es provisional por diseño — pero un provisional casi exacto
    en vez de uno invertido, y es el único que queda si la pelea nunca casa.
    """
    # Una decisión agota el asalto por definición: el dato se conoce sin mirar
    # el reloj, y así se reparan de paso los '-' que ESPN manda a veces (las
    # peleas 12871 y 12872 guardaron un guion literal como hora).
    if method and method.casefold().startswith("decision"):
        return _format_clock(ROUND_SECONDS)

    seconds = _clock_seconds(clock)
    if seconds is None:
        return None

    previous = _clock_seconds(previous_clock)
    if previous is not None:
        # Las dos lecturas del mismo par de relojes, medidas contra el último
        # 'in': `elapsed_gap` es el residuo si ESPN ya sirve el transcurrido y
        # `frozen_gap` el residuo si congeló la cuenta atrás. Gana la pequeña.
        elapsed_gap = abs(seconds - (ROUND_SECONDS - previous))
        frozen_gap = abs(seconds - previous)
        # El empate solo cambia la respuesta con previous == 2:30 exacto (si el
        # empatado es `seconds`, las dos hipótesis dan el MISMO valor), y ahí
        # son indistinguibles por construcción. Se resuelve hacia
        # el transcurrido, que es además lo que hacía el código anterior. Es el
        # lado barato: equivocarse ahí cuesta como mucho el doble de la deriva
        # de ESPN (1-6 s medidos), mientras que caer del otro lado costaría el
        # doble del intervalo de muestreo (~30 s, hasta 60 s de error).
        if elapsed_gap <= frozen_gap:
            # No está congelado: ESPN ya sirve el tiempo oficial transcurrido.
            return _format_clock(seconds)
        return _format_clock(ROUND_SECONDS - seconds)

    # SIN HISTORIAL NO SE ADIVINA (16-ago-2026). Antes esta rama invertía a
    # ciegas, "porque es lo que ESPN sirve mientras el combate está vivo". Se
    # midió contra la base y es falso: las 12 finalizaciones del evento 1063 que
    # pasaron por aquí (un único sondeo a las 19:43Z encontró el cartel entero
    # terminado: 0 muestras 'in', 14 'post' en el mismo instante) tienen hoy un
    # end_time que coincide con el reloj CRUDO en las 12 y con el invertido en
    # NINGUNA. Los errores de la inversión iban de 16 s a 262 s.
    #
    # Y tiene sentido: si no hay serie es porque llegamos tarde, y para entonces
    # ESPN ya sirve el transcurrido oficial, no la cuenta atrás. El caso que la
    # rama vieja decía cubrir — en directo, sin historial y con el reloj
    # congelado — no tiene NI UN ejemplo en toda la base.
    #
    # Aun así NO se devuelve el crudo, que es lo que acertaría en esos 12: no
    # sabemos distinguir aquí "llegamos tarde" de "estamos en directo", y esa
    # segunda rama es la que nadie ha medido nunca. Se devuelve None, lo rellena
    # ufcstats unas horas después y end_time NULL es un estado ya soportado. Un
    # hueco visible es mejor que un número que puede estar cuatro minutos
    # equivocado y que alimenta segundos peleados, golpes por minuto y el radar,
    # donde ya no hay forma de distinguirlo de un dato bueno.
    return None


def _format_clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def is_cancelled_status(status_name: str | None) -> bool:
    """¿El estado fino de ESPN indica una pelea cancelada/aplazada/suspendida?"""
    if not status_name:
        return False
    upper = status_name.upper()
    return any(marker in upper for marker in _CANCELLED_STATUS_MARKERS)


def is_suspended_status(status_name: str | None) -> bool:
    """¿Suspensión (interrupción REANUDABLE en la taxonomía de ESPN)?

    Para el snapshot da igual (borrar era autorreparable), pero la SERIE de
    muestras (timeline, mig. 024) es histórica e irrecuperable: una pelea
    suspendida y reanudada no debe perder su primera mitad (revisión 19-jul).
    """
    return bool(status_name) and "SUSPEND" in status_name.upper()


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
    # El reloj de ESPN es una cuenta atrás y end_time es el transcurrido: sin
    # esta conversión el estelar del 1062 se guardó como 2:17 habiendo sido 2:41.
    end_time = elapsed_end_time(
        fight.end_time,
        fight.method,
        last_in_progress_clock(connection, fight_id),
    )
    LOGGER.info(
        "  LIVE %s def. %s — %s R%s %s (reloj ESPN %s) (fight %s%s)",
        winner_name,
        fight.blue_name if winner_id == red_id else fight.red_name,
        fight.method or "method TBD",
        fight.end_round,
        end_time,
        fight.end_time,
        fight_id,
        ", dry-run" if dry_run else "",
    )
    if dry_run:
        counts["fights_updated"] += 1
        return
    if fill_fight_result(connection, fight_id, winner_id, fight.method, fight.end_round, end_time):
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
                # Timeline (024): la serie de una cancelada tampoco se pinta.
                # EXCEPTO en suspensiones: son reanudables y borrar la serie
                # amputaría la película para siempre (el snapshot sí se borra,
                # es autorreparable en la siguiente pasada).
                if not is_suspended_status(fight.status_name):
                    delete_live_fight_stat_samples(connection, fight_id)
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
        # Timeline del directo (024): tras cada upsert, la fila FUSIONADA se
        # copia como muestra append-only (dedup + guardas en el propio SQL).
        counts["live_samples_written"] += insert_live_fight_stat_sample(
            connection, fight_id
        )


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


def refresh_live_results(dates: str | None = None, dry_run: bool = False) -> Counter:
    settings = get_settings()
    window = dates or default_dates_window()
    # Sin User-Agent propio: `site.api.espn.com` (el scoreboard de aquí abajo)
    # responde 403 a cualquiera. Ver `espn.build_espn_session`.
    session = build_espn_session()
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
    "live_stats_cancelled", "live_samples_written",
]


def night_produced_nothing(totals: Counter) -> bool:
    """La ventana vio el directo y no dejó ni rastro. Es el modo de fallo del run
    29179498181: 109 pasadas, evento encontrado, cero escrituras, SUCCESS.

    Vive aquí, en UNA sola función, porque la usan tanto el aviso como el código
    de salida. Duplicada, un día se toca una copia y no la otra, y el bucle
    acaba avisando de algo por lo que no sale en rojo.

    Lo que NO cuenta como problema, que es la mitad del diseño: un relevo sobre
    cartelera ya sellada (`live_stats_skipped_final`) o ya resuelta
    (`fights_already_filled`) no escribe nada Y HACE BIEN.
    """
    escrituras = totals.get("fights_updated", 0) + totals.get("live_stats_written", 0)
    ya_estaba = (
        totals.get("live_stats_skipped_final", 0) + totals.get("fights_already_filled", 0)
    )
    return bool(totals.get("live_events")) and not escrituras and not ya_estaba


def loop_health_warnings(totals: Counter) -> list[str]:
    """Qué salió mal en la ventana, en cristiano. Lista vacía = todo bien.

    `loop_failures` NO basta, aunque lo pareciera: solo cuenta lo que revienta
    FUERA del bucle por evento. Hay tres tragaderos y este mira los tres, más el
    modo de fallo que ya ocurrió de verdad (el run 29179498181: 109 pasadas, el
    evento encontrado, cero escrituras y conclusión SUCCESS).

    Cuidado con lo que NO es un problema, que es la mitad del diseño: un relevo
    que entra con la cartelera ya sellada no escribe nada y hace bien
    (`live_stats_skipped_final`), igual que uno que la encuentra ya resuelta
    (`fights_already_filled`). Confundir eso con un fallo estrenaría la alerta
    en falso, y como `notify-on-failure` reutiliza un único Issue y no lo cierra
    solo, el primer falso positivo degrada los avisos de todos los workflows
    vigilados a comentarios en un hilo que nadie lee.
    """
    avisos: list[str] = []
    iteraciones = totals.get("loop_iterations", 0)

    fallos = totals.get("loop_failures", 0)
    if fallos:
        avisos.append(
            f"{fallos} de {iteraciones} pasadas fallaron enteras (red, BD o ESPN caído)."
        )
    if totals.get("event_errors"):
        avisos.append(
            f"{totals['event_errors']} errores al procesar un evento (event_errors). "
            "loop_failures es CIEGO a esto: son fallos de escritura o de matching "
            "que process_events se traga por evento."
        )
    if totals.get("events_unmatched"):
        avisos.append(
            f"{totals['events_unmatched']} veces no se pudo casar el evento de ESPN "
            "con ninguna fila de la BD. No lanza excepción: se salta en silencio. "
            "Suele ser que ufc.com renombró la cartelera durante la semana."
        )
    if totals.get("live_stats_unresolved"):
        avisos.append(
            f"{totals['live_stats_unresolved']} veces una pelea de ESPN no resolvió "
            "a una fila nuestra: se queda sin stats en directo ni película."
        )

    if night_produced_nothing(totals):
        avisos.append(
            f"La ventana vio eventos en directo ({totals['live_events']} veces) y no "
            "escribió NADA en toda la noche, ni encontró nada ya sellado. Es el "
            "modo de fallo del run 29179498181, que salió verde igual."
        )
    return avisos


def loop_exit_code(totals: Counter) -> int:
    """Código de salida de la ventana. En rojo solo lo que no admite discusión.

    Dos criterios, y la diferencia entre ellos es POR QUÉ uno se puede endurecer
    hoy y el otro no:

    1. Fallaron TODAS las pasadas. Binario: la ventana no produjo nada.
    2. Vio el directo y no dejó rastro (`night_produced_nothing`). También
       binario — o escribió, o encontró algo ya sellado, o la noche se perdió.

    Lo que sigue SIN poder endurecerse es el criterio porcentual sobre
    `loop_failures` ("¿a partir de qué % de pasadas malas tumbo el job?"): con 0
    fallos en 2339 pasadas de 8 runs, cualquier umbral estaría calibrado contra
    una distribución toda a cero. Ese espera a tener dos o tres veladas de
    muestra; estos dos no, porque no hay nada que calibrar.

    El resto sigue saliendo por `::warning::`: un rojo en falso quema la alerta
    para siempre, porque notify-on-failure reutiliza un único Issue y no lo
    cierra solo.
    """
    iteraciones = totals.get("loop_iterations", 0)
    if iteraciones and totals.get("loop_failures", 0) >= iteraciones:
        return 1
    if night_produced_nothing(totals):
        return 1
    return 0


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
    # Por ASIGNACIÓN, nunca con totals.update(): esto es un Counter y update()
    # SUMA en vez de fijar (es justo lo que hace la línea 666 con los contadores
    # de cada pasada). Hasta hoy estos dos números solo existían en el log, así
    # que main() no tenía forma de saber cómo había ido la ventana.
    totals["loop_iterations"] = iterations
    totals["loop_failures"] = failures
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
        totals = run_bounded_loop(
            dates=args.dates,
            dry_run=args.dry_run,
            duration_minutes=args.duration_minutes,
            interval_seconds=args.interval_seconds,
        )
        # Hasta hoy esta rama hacía un `return` desnudo: el bucle del directo
        # salía en VERDE pasara lo que pasara, y como notify-on-failure filtra
        # por conclusion == 'failure', esa alerta era estructuralmente incapaz
        # de avisar de una noche de directo perdida.
        avisos = loop_health_warnings(totals)
        for aviso in avisos:
            # ::warning:: sale resaltado en el resumen del run de Actions.
            print(f"::warning title=Bucle del directo::{aviso}", flush=True)
            LOGGER.warning("BUCLE: %s", aviso)
        if not avisos:
            LOGGER.info("Bucle: ventana sana, nada que reportar.")
        raise SystemExit(loop_exit_code(totals))
    while True:
        counts = refresh_live_results(dates=args.dates, dry_run=args.dry_run)
        print(json.dumps({key: counts.get(key, 0) for key in SUMMARY_KEYS}, indent=2))
        if not args.loop:
            break
        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
