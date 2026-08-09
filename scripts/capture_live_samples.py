"""Captura muestras CRUDAS de las APIs de ESPN durante un evento UFC en vivo.

RECON para la futura página "En vivo" (idea del dueño, 11-jul-2026): antes de
construir nada hay que saber QUÉ publica ESPN mientras un combate está en
curso (¿stats de golpeo en directo? ¿solo asalto/reloj? ¿probabilidades?).
Este script NO toca la base de datos ni comparte nada con live-results: solo
hace GETs y guarda los JSON tal cual, con timestamp, para analizarlos en frío.

Qué guarda en cada pasada (poll):
  - El scoreboard del día (la misma URL que usa espn_live_results).
  - El "fightcenter" de cada evento del día (la vista que usa espn.com).
  - Para la pelea EN CURSO (state == 'in'): status, situation y las
    statistics de ambos atletas vía la core API (el gran interrogante:
    ¿se rellenan en vivo o solo al acabar?).

Usage:
    python -m scripts.capture_live_samples --once            # 1 poll de QA
    python -m scripts.capture_live_samples                   # ~4h cada 2 min
    python -m scripts.capture_live_samples --duration-minutes 30 --interval-seconds 60
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.scrapers.espn import build_espn_session

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
FIGHTCENTER_URL = "https://site.web.api.espn.com/apis/common/v3/sports/mma/ufc/fightcenter/{event_id}"
CORE_COMP_URL = (
    "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/"
    "events/{event_id}/competitions/{comp_id}/{leaf}"
)
COMP_LEAVES = ("status", "situation", "probabilities")
STATS_LEAF = "competitors/{athlete_id}/statistics"

REQUEST_DELAY_SECONDS = 0.3
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_-]+")

# Cuántas pasadas seguidas puede fallar el scoreboard antes de dar el run por
# perdido. Con el intervalo por defecto (120 s) son 10 minutos seguidos sin que
# ESPN conteste: eso ya no es una pasada mala, es la fuente cerrada.
#
# 🪤 El 8-ago este script ocupó el runner los 235 minutos capturando 403 y
# terminó en VERDE con el artifact vacío: `poll_once` devolvía -1 y `main` solo
# miraba `== 0`, así que el -1 se colaba por el hueco. Un run verde que no
# guarda nada no prueba nada, y encima tapa la avería. La regla de la casa es
# que **los jobs fallen cuando no producen nada**.
MAX_CONSECUTIVE_SCOREBOARD_ERRORS = 5

# Código de salida cuando el run termina sin haber capturado nada útil.
EXIT_NOTHING_CAPTURED = 1


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fetch_json(session: requests.Session, url: str, params: dict | None = None) -> dict | None:
    try:
        response = session.get(url, params=params, timeout=30)
    except requests.RequestException:
        return None
    time.sleep(REQUEST_DELAY_SECONDS)
    if not response.ok:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _save(out_dir: Path, label: str, payload: dict, counts: Counter) -> None:
    safe = _SAFE_LABEL_RE.sub("-", label)
    path = out_dir / f"{_now_stamp()}-{safe}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    counts["saved"] += 1


def poll_once(session: requests.Session, out_dir: Path, dates: str, counts: Counter) -> int:
    """Una pasada completa. Devuelve el nº de eventos con competiciones hoy."""
    scoreboard = _fetch_json(session, SCOREBOARD_URL, params={"dates": dates})
    if scoreboard is None:
        counts["scoreboard_error"] += 1
        return -1
    events = [
        event for event in scoreboard.get("events") or []
        if event.get("competitions")
    ]
    _save(out_dir, "scoreboard", scoreboard, counts)
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        fightcenter = _fetch_json(session, FIGHTCENTER_URL.format(event_id=event_id))
        if fightcenter is not None:
            _save(out_dir, f"fightcenter-{event_id}", fightcenter, counts)
        else:
            counts["fightcenter_miss"] += 1
        for comp in event.get("competitions") or []:
            state = (((comp.get("status") or {}).get("type")) or {}).get("state")
            if state != "in":
                continue
            comp_id = str(comp.get("id") or "")
            counts["live_competitions"] += 1
            for leaf in COMP_LEAVES:
                payload = _fetch_json(
                    session, CORE_COMP_URL.format(event_id=event_id, comp_id=comp_id, leaf=leaf)
                )
                if payload is not None:
                    _save(out_dir, f"comp-{comp_id}-{leaf}", payload, counts)
            for competitor in comp.get("competitors") or []:
                athlete_id = str(((competitor.get("athlete") or {}).get("id")) or competitor.get("id") or "")
                if not athlete_id:
                    continue
                leaf = STATS_LEAF.format(athlete_id=athlete_id)
                payload = _fetch_json(
                    session, CORE_COMP_URL.format(event_id=event_id, comp_id=comp_id, leaf=leaf)
                )
                if payload is not None:
                    _save(out_dir, f"comp-{comp_id}-stats-{athlete_id}", payload, counts)
                else:
                    counts["stats_miss"] += 1
    return len(events)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture raw ESPN live-event payloads (read-only recon).")
    parser.add_argument("--duration-minutes", type=int, default=235)
    parser.add_argument("--interval-seconds", type=int, default=120)
    parser.add_argument("--dates", default=None, help="YYYYMMDD (default: hoy en UTC)")
    parser.add_argument("--out", default="samples")
    parser.add_argument("--once", action="store_true", help="Una sola pasada (QA).")
    args = parser.parse_args()

    dates = args.dates or datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Sin User-Agent propio: este script pide SCOREBOARD_URL, y
    # `site.api.espn.com` responde 403 a cualquiera — el `Mozilla/5.0 (...)
    # mma-status-recon` que había aquí incluido, medido. Ver
    # `espn.build_espn_session`.
    session = build_espn_session()
    counts: Counter = Counter()

    events_today = poll_once(session, out_dir, dates, counts)
    if events_today < 0:
        # El scoreboard no contestó en la PRIMERA pasada. No es mala suerte: es
        # la fuente cerrada (así se veía el 403 del 8-ago). Salir YA y en ROJO,
        # en vez de ocupar el runner 235 minutos capturando el mismo error.
        print(json.dumps({"aborted": "scoreboard unreachable on first poll", "dates": dates}))
        raise SystemExit(EXIT_NOTHING_CAPTURED)
    if events_today == 0:
        # Guard del cron: sábado sin cartelera -> salir barato y en verde.
        print(json.dumps({"skipped": "no events today", "dates": dates}))
        return
    counts["polls"] += 1

    if not args.once:
        deadline = time.monotonic() + args.duration_minutes * 60
        consecutive_errors = 0
        while time.monotonic() < deadline:
            time.sleep(max(1, args.interval_seconds))
            events_now = poll_once(session, out_dir, dates, counts)
            counts["polls"] += 1
            # Una pasada mala no mata la ventana (misma filosofía que
            # `run_bounded_loop`), pero una racha sí: si ESPN se cierra a mitad
            # de velada, seguir hasta el final es quemar el runner y salir en
            # verde sin haber grabado la segunda mitad.
            if events_now < 0:
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_SCOREBOARD_ERRORS:
                    print(json.dumps({
                        "aborted": "scoreboard unreachable",
                        "consecutive_errors": consecutive_errors,
                        "dates": dates,
                        **{k: counts[k] for k in sorted(counts)},
                    }, indent=2))
                    raise SystemExit(EXIT_NOTHING_CAPTURED)
            else:
                consecutive_errors = 0
            if counts["polls"] % 10 == 0:
                print(json.dumps({"progress": dict(counts)}), flush=True)

    print(json.dumps({"dates": dates, **{k: counts[k] for k in sorted(counts)}}, indent=2))

    # Cinturón: si el run termina sin un solo fichero guardado, no es un run en
    # verde. Hoy es inalcanzable (la primera pasada ya aborta), y está a
    # propósito: deja escrito el invariante para quien toque el aborto de arriba.
    if counts["saved"] == 0:
        raise SystemExit(EXIT_NOTHING_CAPTURED)


if __name__ == "__main__":
    main()
