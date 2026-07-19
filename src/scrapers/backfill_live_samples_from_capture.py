"""Backfill del Timeline del directo (migración 024) desde capturas locales.

Reproduce las pasadas guardadas por scripts/capture_live_samples.py (p. ej.
UFC 329, 11-jul-2026: 116 polls cada ~120 s en
docs/assets/espn-live-samples-ufc329/espn-live-samples-29167386226/) contra
live_fight_stat_samples, con sampled_at = timestamp del NOMBRE de cada
scoreboard (la hora real de captura). Así la "película" del combate existe
para eventos anteriores al estreno del escritor en vivo.

REQUIRES migration db/migrations/024_live_fight_stat_samples.sql applied first.

Guardas (las mismas que el escritor en vivo, ver repositories/live_stats.py):
  - solo peleas in/post resueltas contra la BD con el MISMO matcher que
    espn_live_results (_resolve_bout); las canceladas se excluyen y, si ya
    habían emitido muestras en el replay, la serie entera se descarta;
  - walkouts (period 0) y polls sin stats acumuladas aún no emiten muestra:
    la serie empieza con el primer asalto;
  - dedup contra la última muestra emitida de cada pelea (mismo criterio que
    el NOT EXISTS del escritor);
  - live_fight_stats (el snapshot vivo) y fights/fight_stats NO se tocan.

Idempotencia: --apply BORRA primero las muestras existentes de las peleas con
serie nueva y re-inserta completo (replace); re-ejecutar deja el mismo
resultado. Por defecto dry-run (informe sin escribir).

Usage:
    python -m src.scrapers.backfill_live_samples_from_capture --capture-dir DIR
    python -m src.scrapers.backfill_live_samples_from_capture --capture-dir DIR --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings
from .db import connect
from .espn import _build_exact_name_index, _build_normalized_name_index
from .espn_live_results import (
    _resolve_bout,
    is_cancelled_status,
    is_suspended_status,
    match_db_event,
    parse_scoreboard,
)
from .espn_live_stats import parse_stat_values
from .logging_config import configure_logging
from .repositories.fighters import get_all_fighters
from .repositories.live_stats import (
    count_live_fight_stat_samples,
    delete_live_fight_stat_samples,
    insert_live_fight_stat_sample_at,
)

LOGGER = logging.getLogger(__name__)

# Nombres que escribe capture_live_samples: <ts>-scoreboard.json y
# <ts>-comp-<compId>-stats-<athleteId>.json (fightcenter/status/situation se
# ignoran: el scoreboard ya trae el estado fino y va sincronizado con la core).
CAPTURE_NAME_RE = re.compile(
    r"^(?P<ts>\d{8}T\d{6}Z)-"
    r"(?:(?P<scoreboard>scoreboard)|comp-(?P<comp>\d+)-stats-(?P<athlete>\d+))"
    r"\.json$"
)


@dataclass(frozen=True)
class CapturePoll:
    """Una pasada capturada: su scoreboard y las hojas de stats de esa pasada."""

    sampled_at: datetime
    scoreboard_name: str
    stats_names: dict[tuple[str, str], str]  # (comp_id, athlete_id) -> fichero


def _parse_capture_ts(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def group_capture_files(names: list[str]) -> list[CapturePoll]:
    """Agrupa los ficheros de una captura en pasadas (polls).

    El capturador escribe el scoreboard y ~1-2 s después las hojas de stats de
    las peleas en curso, así que cada hoja pertenece al ÚLTIMO scoreboard
    anterior a su timestamp. Hojas sin scoreboard previo (no debería ocurrir)
    se descartan."""
    scoreboards: list[tuple[datetime, str]] = []
    stat_sheets: list[tuple[datetime, str, str, str]] = []
    for name in names:
        match = CAPTURE_NAME_RE.match(name)
        if not match:
            continue
        ts = _parse_capture_ts(match.group("ts"))
        if match.group("scoreboard"):
            scoreboards.append((ts, name))
        else:
            stat_sheets.append((ts, match.group("comp"), match.group("athlete"), name))
    scoreboards.sort()
    stat_sheets.sort()
    polls: list[CapturePoll] = []
    for i, (ts, sb_name) in enumerate(scoreboards):
        next_ts = scoreboards[i + 1][0] if i + 1 < len(scoreboards) else None
        sheets = {
            (comp, athlete): sheet_name
            for sheet_ts, comp, athlete, sheet_name in stat_sheets
            if sheet_ts >= ts and (next_ts is None or sheet_ts < next_ts)
        }
        polls.append(CapturePoll(sampled_at=ts, scoreboard_name=sb_name, stats_names=sheets))
    return polls


def _load_json(capture_dir: Path, name: str, counts: Counter):
    """JSON de un fichero de captura; None (y contador) si está corrupto.

    Un job de captura matado a mitad de write_text deja un JSON truncado: un
    solo fichero malo no debe inutilizar el replay entero (revisión 19-jul)."""
    try:
        return json.loads((capture_dir / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        counts["files_corrupt"] += 1
        LOGGER.warning("Fichero de captura ilegible, se salta: %s (%s)", name, exc)
        return None


def replay_capture(
    connection, capture_dir: Path, promotion_id: int, *,
    apply: bool, force: bool = False,
) -> Counter:
    """Reproduce la captura y (con apply) escribe la serie en Neon.

    Mantiene en memoria el snapshot FUSIONADO por pelea (mismo merge por lado
    que el UPSERT del escritor en vivo) y emite una muestra por poll cuando
    cambió algo, con las mismas guardas que insert_live_fight_stat_sample,
    más el 'post' pegajoso del escritor (un scoreboard rezagado no resucita
    una pelea terminada). Las SUSPENSIONES (reanudables) solo saltan el poll;
    únicamente los estados terminales (cancel/postpone/...) descartan la
    serie. Con apply, el guard anti-machaque salta las peleas cuya serie
    existente en BD tiene MÁS muestras que la nueva (p. ej. la del escritor
    en vivo a 20 s frente a una captura de 120 s) salvo con force=True."""
    counts: Counter = Counter()
    polls = group_capture_files([p.name for p in capture_dir.iterdir()])
    counts["polls"] = len(polls)
    fighters = get_all_fighters(connection)
    exact_index = _build_exact_name_index(fighters)
    normalized_index = _build_normalized_name_index(fighters)
    event_cache: dict[str, int | None] = {}
    bout_cache: dict[str, tuple[int, int, int] | None] = {}
    merged: dict[int, dict[str, dict[str, int]]] = {}
    last_key: dict[int, tuple] = {}
    # 'post' pegajoso del escritor, replicado en memoria: un scoreboard
    # rezagado de la captura no puede devolver una pelea terminada a 'in'.
    posted: set[int] = set()
    cancelled: set[int] = set()
    samples: list[tuple] = []
    resolve_counts: Counter = Counter()
    for poll in polls:
        payload = _load_json(capture_dir, poll.scoreboard_name, counts)
        if payload is None:
            continue
        for event in parse_scoreboard(payload):
            if event.espn_id not in event_cache:
                event_cache[event.espn_id] = match_db_event(connection, event, promotion_id)
                if event_cache[event.espn_id] is None:
                    counts["events_unmatched"] += 1
                    LOGGER.info("Sin evento BD para ESPN %r; se ignora", event.name)
            event_id = event_cache[event.espn_id]
            if event_id is None:
                continue
            for fight in event.fights:
                if not (fight.completed or fight.state in ("in", "post")):
                    continue
                if fight.competition_id not in bout_cache:
                    bout_cache[fight.competition_id] = _resolve_bout(
                        connection, event_id, fight, exact_index, normalized_index,
                        resolve_counts,
                    )
                bout = bout_cache[fight.competition_id]
                if bout is None:
                    continue
                red_id, blue_id, fight_id = bout
                if is_cancelled_status(fight.status_name):
                    # Suspensión = interrupción reanudable: se salta el poll
                    # pero NO descarta la serie (revisión 19-jul). Solo los
                    # estados terminales (cancel/postpone/...) la descartan.
                    if not is_suspended_status(fight.status_name):
                        cancelled.add(fight_id)
                    continue
                fresh: dict[str, dict[str, int]] = {}
                for espn_id, db_id in ((fight.red_espn_id, red_id), (fight.blue_espn_id, blue_id)):
                    if not espn_id:
                        continue
                    sheet = poll.stats_names.get((fight.competition_id, espn_id))
                    if not sheet:
                        continue
                    sheet_payload = _load_json(capture_dir, sheet, counts)
                    values = (
                        parse_stat_values(sheet_payload)
                        if sheet_payload is not None
                        else None
                    )
                    if values is not None:
                        fresh[str(db_id)] = values
                if fresh:
                    merged.setdefault(fight_id, {}).update(fresh)
                snapshot = merged.get(fight_id)
                if not snapshot:
                    continue
                # Walkouts/pre-asalto: la serie empieza con el primer asalto.
                if not fight.end_round or fight.end_round < 1:
                    continue
                if fight.completed or fight.state == "post":
                    posted.add(fight_id)
                # Pegajoso: una vez 'post', ningún scoreboard rezagado de la
                # captura la devuelve a 'in' (mismo CASE del upsert real).
                state = "post" if fight_id in posted else "in"
                key = (
                    state, fight.end_round, fight.end_time,
                    json.dumps(snapshot, sort_keys=True),
                )
                if last_key.get(fight_id) == key:
                    continue
                last_key[fight_id] = key
                samples.append(
                    (
                        fight_id, state, fight.status_name, fight.status_detail,
                        fight.end_round, fight.end_time,
                        {side: dict(values) for side, values in snapshot.items()},
                        poll.sampled_at,
                    )
                )
    kept = [sample for sample in samples if sample[0] not in cancelled]
    counts["samples_emitted"] = len(kept)
    counts["samples_dropped_cancelled"] = len(samples) - len(kept)
    per_fight = Counter(sample[0] for sample in kept)
    counts["fights_with_samples"] = len(per_fight)
    for fight_id, n in sorted(per_fight.items()):
        LOGGER.info(
            "  fight %s: %d muestras%s", fight_id, n, "" if apply else " (dry-run)"
        )
    if apply:
        # Guard anti-machaque: una serie existente MÁS densa que la nueva
        # (p. ej. la del escritor en vivo a 20 s frente a esta captura de
        # ~120 s) no se reemplaza salvo --force (revisión 19-jul).
        skipped: set[int] = set()
        if not force:
            for fight_id, n_new in sorted(per_fight.items()):
                existing = count_live_fight_stat_samples(connection, fight_id)
                if existing > n_new:
                    skipped.add(fight_id)
                    counts["fights_skipped_richer_series"] += 1
                    LOGGER.warning(
                        "  fight %s: serie existente (%d muestras) > nueva (%d); "
                        "se conserva (usa --force para reemplazarla)",
                        fight_id, existing, n_new,
                    )
        # Replace: re-ejecutar el backfill deja exactamente la misma serie.
        for fight_id in sorted(per_fight):
            if fight_id not in skipped:
                delete_live_fight_stat_samples(connection, fight_id)
        # Las canceladas descartadas también limpian su serie en BD (una
        # pasada previa del backfill o del escritor pudo dejarla huérfana).
        for fight_id in sorted(cancelled):
            delete_live_fight_stat_samples(connection, fight_id)
            counts["cancelled_series_deleted"] += 1
        for sample in kept:
            if sample[0] not in skipped:
                insert_live_fight_stat_sample_at(connection, *sample)
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Backfill de live_fight_stat_samples desde una captura local "
        "de capture_live_samples (timeline del directo).",
    )
    parser.add_argument(
        "--capture-dir", required=True,
        help="Carpeta con <ts>-scoreboard.json y <ts>-comp-*-stats-*.json",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Escribe la serie en Neon: borra la previa de esas peleas y "
        "re-inserta (default: dry-run).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Reemplaza también series existentes MÁS densas que la nueva "
        "(por defecto se conservan; ver guard anti-machaque).",
    )
    args = parser.parse_args()
    capture_dir = Path(args.capture_dir)
    if not capture_dir.is_dir():
        raise SystemExit(f"--capture-dir no existe o no es carpeta: {capture_dir}")
    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = replay_capture(
            connection, capture_dir, settings.promotion_id_ufc,
            apply=args.apply, force=args.force,
        )
        if args.apply:
            connection.commit()
        else:
            connection.rollback()
    print(json.dumps(dict(counts), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
