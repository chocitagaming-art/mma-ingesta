"""T3-A fase B: stats EN VIVO por pelea desde la core API de ESPN.

Verificado con las muestras reales del capturador durante UFC 329
(11-jul-2026, scripts/capture_live_samples.py): mientras una pelea está en
curso, ESPN publica y ACTUALIZA EN DIRECTO las estadísticas de cada
luchador vía

    sports.core.api.espn.com/v2/sports/mma/leagues/ufc/
        events/{event_id}/competitions/{comp_id}/competitors/{athlete_id}/statistics

(payload splits.categories[].stats[] con ~42 métricas; timeInControl llega
en SEGUNDOS: value 61.0 == displayValue "1:01"). El scoreboard NO lleva
stats por combate (competitors[].statistics viene vacío) y `probabilities`
no publica nada — este endpoint es la única fuente viva.

Este módulo solo sabe pedir y parsear; el matching pelea→BD y la escritura
los orquesta espn_live_results (que ya tiene el matcher y la ventana).
Se guarda un subconjunto compacto alineado con lo que la web pinta
(claves cortas, estilo fight_stats):

    kd   knockDowns                    tsl/tsa  golpes totales con./int.
    ssl/ssa  golpes significativos     tdl/tda  derribos con./int.
    sub  intentos de sumisión          ctrl     control en segundos
"""

from __future__ import annotations

import logging
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

CORE_STATS_URL = (
    "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/"
    "events/{event_id}/competitions/{comp_id}/competitors/{athlete_id}/statistics"
)

# Estado fino de UNA pelea. Lo pedimos solo como plan B del método, cuando el
# scoreboard no trae la entrada "Unofficial Winner" (su details[] viene topado
# a 10 y la entrada del método se cae por abajo: 2 de las 12 peleas del
# 15-ago-2026 se sellaron con method NULL por eso).
# Pesa 444-476 bytes medidos, contra los 165 KB del fightcenter.
CORE_STATUS_URL = (
    "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/"
    "events/{event_id}/competitions/{comp_id}/status"
)

# Nombre ESPN -> clave compacta almacenada en live_fight_stats.stats.
STAT_KEY_MAP = {
    "knockDowns": "kd",
    "totalStrikesLanded": "tsl",
    "totalStrikesAttempted": "tsa",
    "sigStrikesLanded": "ssl",
    "sigStrikesAttempted": "ssa",
    "takedownsLanded": "tdl",
    "takedownsAttempted": "tda",
    "submissions": "sub",
    "timeInControl": "ctrl",
}

# Desglose por objetivo estilo panel de ESPN (CABEZA/CUERPO/PIERNAS con./int.):
# ESPN publica el sig. striking por posición×objetivo; su panel suma las 3
# posiciones (distancia+clinch+suelo) por objetivo. Reproducimos esa suma al
# parsear -> claves derivadas hl/ha (head), bl/ba (body), ll/la (leg).
_TARGET_SUM_KEYS = {
    "hl": ("sigDistanceHeadStrikesLanded", "sigClinchHeadStrikesLanded", "sigGroundHeadStrikesLanded"),
    "ha": ("sigDistanceHeadStrikesAttempted", "sigClinchHeadStrikesAttempted", "sigGroundHeadStrikesAttempted"),
    "bl": ("sigDistanceBodyStrikesLanded", "sigClinchBodyStrikesLanded", "sigGroundBodyStrikesLanded"),
    "ba": ("sigDistanceBodyStrikesAttempted", "sigClinchBodyStrikesAttempted", "sigGroundBodyStrikesAttempted"),
    "ll": ("sigDistanceLegStrikesLanded", "sigClinchLegStrikesLanded", "sigGroundLegStrikesLanded"),
    "la": ("sigDistanceLegStrikesAttempted", "sigClinchLegStrikesAttempted", "sigGroundLegStrikesAttempted"),
}


def parse_stat_values(payload: dict[str, Any] | None) -> dict[str, int] | None:
    """Subconjunto compacto del payload de statistics; None si no es usable.

    Defensivo por diseño: un payload sin splits/categories (o con stats no
    numéricas) devuelve None y el llamador simplemente no escribe ese lado.
    Todos los valores observados son enteros (ctrl en segundos)."""
    if not isinstance(payload, dict):
        return None
    splits = payload.get("splits")
    if not isinstance(splits, dict):
        return None
    raw: dict[str, int] = {}
    for category in splits.get("categories") or []:
        for stat in (category or {}).get("stats") or []:
            name = (stat or {}).get("name")
            if not name:
                continue
            value = stat.get("value")
            if isinstance(value, (int, float)):
                raw[name] = int(round(value))
    values = {key: raw[name] for name, key in STAT_KEY_MAP.items() if name in raw}
    for key, components in _TARGET_SUM_KEYS.items():
        present = [raw[name] for name in components if name in raw]
        if present:
            values[key] = sum(present)
    return values if values else None


def fetch_competitor_stats(
    session: requests.Session,
    event_espn_id: str,
    competition_id: str,
    athlete_id: str,
) -> dict[str, int] | None:
    """GET + parseo de las stats vivas de un luchador; None si falla algo."""
    url = CORE_STATS_URL.format(
        event_id=event_espn_id, comp_id=competition_id, athlete_id=athlete_id
    )
    try:
        response = session.get(url, timeout=30)
    except requests.RequestException:
        LOGGER.info("  stats fetch failed for athlete %s (network)", athlete_id)
        return None
    if not response.ok:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return parse_stat_values(payload)


def fetch_competition_status(
    session: requests.Session,
    event_espn_id: str,
    competition_id: str,
) -> dict[str, Any] | None:
    """GET del leaf `status` de una pelea; None si falla algo.

    Mismo patrón defensivo que fetch_competitor_stats: cualquier tropiezo
    devuelve None y el llamador se queda como estaba. Este endpoint solo se
    consulta para RELLENAR un método ausente, así que no contestar nunca puede
    empeorar el dato, solo dejarlo igual.
    """
    url = CORE_STATUS_URL.format(event_id=event_espn_id, comp_id=competition_id)
    try:
        response = session.get(url, timeout=30)
    except requests.RequestException:
        LOGGER.info("  status fetch failed for competition %s (network)", competition_id)
        return None
    if not response.ok:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def collect_fight_stats(
    session: requests.Session,
    event_espn_id: str,
    competition_id: str,
    athletes: list[tuple[str | None, int]],
) -> dict[str, dict[str, int]] | None:
    """Stats de la pelea con claves = fighters.id NUESTROS.

    athletes: [(espn_athlete_id, db_fighter_id), ...] ya resueltos por el
    matcher de live-results. Devuelve {"<db_id>": {...}} solo con los lados
    que ESPN sirvió; None si no sirvió ninguno (se conserva el último JSON
    conocido gracias al COALESCE del UPSERT)."""
    stats: dict[str, dict[str, int]] = {}
    for espn_athlete_id, db_fighter_id in athletes:
        if not espn_athlete_id:
            continue
        values = fetch_competitor_stats(
            session, event_espn_id, competition_id, espn_athlete_id
        )
        if values is not None:
            stats[str(db_fighter_id)] = values
    return stats or None
