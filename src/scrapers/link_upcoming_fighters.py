"""Create + link fighter records for upcoming-event bouts stored as name-only.

`upcoming_events` stored some bout slots with only fighter_red_name /
fighter_blue_name and no fighter_red_id / fighter_blue_id (fighters new to the
DB). Those render as initials + 0-0-0 because there is no fighter row behind
them. This resolves each unlinked name on ESPN, imports a fighter row (record +
photo + measures + nationality), and links the bout slot to it.

DOS VIAS, Y LA BUENA VA PRIMERO
-------------------------------
1. POR EL MARCADOR (`resolver_rival_por_marcador`). ESPN publica la cartelera
   del evento con el ID de cada atleta. Si la otra esquina del combate YA tiene
   ficha, se busca el combate del marcador donde aparece ese luchador y se lee
   el id del rival. No se adivina nada: lo dice la misma fuente que leera el
   bucle en directo el dia de la velada.
2. POR NOMBRE (`_resolve`), solo si la primera no puede. Es una busqueda
   ESTRICTA y se queda corta a proposito.

Por que ese orden, con un caso real: la cartelera del 8-ago-2026 llama al rival
de Louie Sutherland "Jose Montanha da Silva". Por nombre, ESPN no encuentra
nada; aflojando la busqueda aparece "Jose Montanha" (5351808), que parece el
mismo hombre y NO LO ES -- el que pelea es 4389073, "Henrique da Silva Lopes",
apodo "Montanha". Por el marcador no hay duda posible: en el combate de
Sutherland, el rival es 4389073.

Usage:
    python -m src.scrapers.link_upcoming_fighters --dry-run   # resolve + report, no writes
    python -m src.scrapers.link_upcoming_fighters             # import + link (writes)
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from .config import get_settings
from .db import connect
from .enrich_ranked import (
    ESPN_SOURCE,
    REQUEST_DELAY_SECONDS,
    _build_session,
    _fetch_athlete_by_id,
    _search_espn_athlete,
)
from .enrich_records_espn import _fetch_espn_record
from .espn import EspnAthlete
from .espn_live_results import fetch_scoreboard, match_db_event, parse_scoreboard
from .logging_config import configure_logging
from .models import FighterRecord
from .repositories.fighters import get_fighter_id_by_espn_id, upsert_fighter

LOGGER = logging.getLogger(__name__)


def resolver_rival_por_marcador(ancla_espn_id: str, peleas) -> str | None:
    """El id de ESPN del rival, leido del combate donde aparece el ancla.

    `ancla_espn_id` es el atleta de la esquina que YA tiene ficha; `peleas` son
    los `LiveFight` del marcador de ESPN para ese evento. Si el ancla aparece en
    exactamente UN combate, se devuelve el id de la otra esquina.

    Es identidad sin adivinar: no compara nombres, compara identificadores de la
    misma fuente que leera el bucle en directo. Devuelve None -- y el hueco se
    queda visible, que es lo correcto -- cuando:

    * el ancla no aparece en el marcador (cartelera aun sin publicar, o el
      luchador cambio);
    * aparece en MAS de un combate (no deberia pasar nunca, pero si pasa es que
      el marcador esta mal y adivinar seria peor);
    * el rival viene sin id (ESPN publica a veces un hueco "TBD").
    """
    if not ancla_espn_id:
        return None
    encontrados = [
        pelea
        for pelea in peleas
        if ancla_espn_id in (pelea.red_espn_id, pelea.blue_espn_id)
    ]
    if len(encontrados) != 1:
        if encontrados:
            LOGGER.warning(
                "El atleta %s aparece en %d combates del marcador; no se resuelve",
                ancla_espn_id,
                len(encontrados),
            )
        return None
    pelea = encontrados[0]
    rival = pelea.blue_espn_id if pelea.red_espn_id == ancla_espn_id else pelea.red_espn_id
    return rival or None


def _resolve(session, name: str) -> tuple[EspnAthlete, tuple[int, int, int]] | None:
    """Resolve an ESPN athlete + W-L-D for a name. No DB writes.

    🪤 NO AFLOJES ESTA BUSQUEDA POR NOMBRE. Se intento el 2-ago-2026 y salio
    mal en menos de una hora. La cartelera del 1087 decia "Jose Montanha da
    Silva" y ESPN no encuentra ese nombre; probando variantes mas cortas,
    "Jose Montanha" SI devuelve un atleta (5351808) que ademas pasa
    `token_subset_match`. Parece el mismo hombre y NO LO ES: el que pelea es
    4389073, "Henrique da Silva Lopes", apodo "Montanha" — nacido en 1985 y no
    en 1996, 5-2-0 y no 6-1-0. Lo zanja el calendario de ESPN, donde el evento
    600060621 aparece en el de 4389073 y no en el del otro.

    Un nombre parecido no es una persona. Esta funcion CREA fichas y las suelda
    a un combate, asi que es una ruta de IDENTIDAD, y la politica del proyecto
    para esas rutas esta escrita en matching.py: umbral estricto, y un falso
    positivo suelda el historial del luchador equivocado. Si un hueco no se
    resuelve, la respuesta correcta es dejarlo sin resolver y que se vea, no
    bajar el liston.

    Lo que SI resuelve estos casos sin adivinar es el marcador de ESPN del
    propio evento, que dice que atleta pelea cada combate por id.
    """
    found = _search_espn_athlete(session, name)
    if found is None:
        return None
    athlete_id, _espn_name = found
    athlete = _fetch_athlete_by_id(session, athlete_id)
    if athlete is None:
        return None
    record = _fetch_espn_record(session, athlete_id) or (0, 0, 0)
    return athlete, record


# ESPN publica huecos como si fueran datos: un alcance de 0 cm y una postura
# "--". Hoy hay 27 fichas con reach_cm = 0 y 26 con stance = '--', y el alcance
# alimenta el modelo de predicción, donde un cero no es "no lo sé" sino un
# luchador sin brazos. Aquí solo se crean fichas NUEVAS, así que convertir el
# hueco en NULL no puede pisar un dato bueno: o llega vacío de ESPN o no llega.
_ESPN_EMPTY_STANCE = {"--", "-", "n/a", ""}


def _clean_measure(value: float | None) -> float | None:
    """Un 0 de ESPN es un hueco, no una medida."""
    return value if value else None


def _clean_stance(value: str | None) -> str | None:
    """Convierte los marcadores de hueco de ESPN ('--') en NULL."""
    if value is None:
        return None
    return None if value.strip().lower() in _ESPN_EMPTY_STANCE else value


def _get_unlinked_slots(connection) -> list[tuple[int, str, str]]:
    """Huecos por rellenar. Firma historica: `add_manual_fighter` la importa."""
    return [(fila[0], fila[1], fila[2]) for fila in _get_unlinked_slots_con_contexto(connection)]


def _get_unlinked_slots_con_contexto(connection) -> list[tuple[int, str, str, int, object, int | None]]:
    """Los huecos, mas lo que hace falta para resolverlos por el marcador.

    Devuelve `(fight_id, esquina, nombre, event_id, event_date, ancla_id)`,
    donde `ancla_id` es el `fighters.id` de la OTRA esquina del mismo combate
    -- el luchador que si tiene ficha y que sirve para localizar el combate en
    el marcador de ESPN. Es NULL cuando las dos esquinas estan sin ficha, y
    entonces no hay ancla posible y solo queda la busqueda por nombre.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fi.id, 'red' AS corner, fi.fighter_red_name AS name,
                   e.id AS event_id, e.event_date, fi.fighter_blue_id AS ancla
            FROM events e JOIN fights fi ON fi.event_id = e.id
            WHERE e.status = 'upcoming'
              AND fi.fighter_red_id IS NULL AND fi.fighter_red_name IS NOT NULL
              AND fi.status IS DISTINCT FROM 'cancelled'
            UNION ALL
            SELECT fi.id, 'blue', fi.fighter_blue_name,
                   e.id, e.event_date, fi.fighter_red_id
            FROM events e JOIN fights fi ON fi.event_id = e.id
            WHERE e.status = 'upcoming'
              AND fi.fighter_blue_id IS NULL AND fi.fighter_blue_name IS NOT NULL
              AND fi.status IS DISTINCT FROM 'cancelled'
            ORDER BY name
            """
        )
        return [
            (int(r[0]), str(r[1]), str(r[2]), int(r[3]), r[4], int(r[5]) if r[5] is not None else None)
            for r in cursor.fetchall()
        ]


def _espn_id_de_ficha(connection, fighter_id: int) -> str | None:
    """El id de ESPN de una ficha, este donde este guardado."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT CASE WHEN source = 'espn' THEN source_id ELSE espn_id END
            FROM fighters WHERE id = %s
            """,
            (fighter_id,),
        )
        row = cursor.fetchone()
        return str(row[0]) if row and row[0] else None


def _peleas_del_marcador(session, event_date, event_id: int, promotion_id: int, connection, cache: dict):
    """Los combates que ESPN publica para el evento, con sus ids de atleta.

    Cachea por fecha: una cartelera con varios huecos no tiene que descargar el
    marcador una vez por hueco.
    """
    if event_date is None:
        return ()
    clave = event_date.strftime("%Y%m%d")
    if clave not in cache:
        try:
            payload = fetch_scoreboard(session, clave)
            cache[clave] = parse_scoreboard(payload)
        except Exception as exc:  # noqa: BLE001 - un evento sin marcador no para el resto
            LOGGER.warning("No se pudo leer el marcador de ESPN para %s: %s", clave, exc)
            cache[clave] = []
    for evento in cache[clave]:
        if match_db_event(connection, evento, promotion_id) == event_id:
            return evento.fights
    return ()


def _resolver_hueco(session, connection, hueco, promotion_id: int, cache: dict, counts: dict):
    """Resuelve un hueco: primero por el marcador de ESPN, y si no, por nombre.

    Devuelve `(athlete, record, fighter_id_existente)`. El tercer valor no es
    None cuando ya hay una ficha para ese atleta: entonces basta con enlazar y
    NO se inserta nada -- que es lo que evita duplicar a un luchador que ya
    tenia historial y quemar su clave de nombre.
    """
    bout_id, corner, name, event_id, event_date, ancla_id = hueco

    if ancla_id is not None:
        ancla_espn_id = _espn_id_de_ficha(connection, ancla_id)
        if ancla_espn_id:
            peleas = _peleas_del_marcador(
                session, event_date, event_id, promotion_id, connection, cache
            )
            rival_espn_id = resolver_rival_por_marcador(ancla_espn_id, peleas)
            if rival_espn_id:
                counts["por_marcador"] += 1
                existente = get_fighter_id_by_espn_id(connection, rival_espn_id)
                if existente is not None:
                    LOGGER.info(
                        "%r -> por el marcador: atleta %s, que YA tiene ficha (%d)",
                        name, rival_espn_id, existente,
                    )
                    return None, None, existente
                athlete = _fetch_athlete_by_id(session, rival_espn_id)
                if athlete is not None:
                    record = _fetch_espn_record(session, rival_espn_id) or (0, 0, 0)
                    LOGGER.info(
                        "%r -> por el marcador: %s (%s) %s-%s-%s",
                        name, athlete.full_name, rival_espn_id, *record,
                    )
                    return athlete, record, None

    # Sin ancla utilizable: la busqueda ESTRICTA por nombre, que se queda corta
    # a proposito. Ver el aviso de `_resolve`.
    resolved = _resolve(session, name)
    if resolved is None:
        return None, None, None
    athlete, record = resolved
    counts["por_nombre"] += 1
    existente = get_fighter_id_by_espn_id(connection, athlete.athlete_id)
    if existente is not None:
        return None, None, existente
    return athlete, record, None


def link_upcoming(dry_run: bool = False) -> dict[str, int]:
    settings = get_settings()
    session = _build_session(settings)
    counts = {
        "slots": 0, "resolved": 0, "linked": 0, "unresolved": 0,
        "por_marcador": 0, "por_nombre": 0, "reutilizadas": 0,
    }
    cache_marcador: dict = {}
    with connect(settings.database_url) as connection:
        slots = _get_unlinked_slots_con_contexto(connection)
        counts["slots"] = len(slots)
        LOGGER.info("Unlinked upcoming bout slots: %d", len(slots))

        for hueco in slots:
            bout_id, corner, name = hueco[0], hueco[1], hueco[2]
            try:
                athlete, record, existente = _resolver_hueco(
                    session, connection, hueco, settings.promotion_id_ufc, cache_marcador, counts
                )
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:  # noqa: BLE001 - keep going on a single failure
                LOGGER.warning("Resolve failed for %r: %s", name, exc)
                counts["unresolved"] += 1
                continue

            if existente is not None:
                counts["resolved"] += 1
                counts["reutilizadas"] += 1
                if dry_run:
                    continue
                column = "fighter_red_id" if corner == "red" else "fighter_blue_id"
                with connection.cursor() as cursor:
                    cursor.execute(f"UPDATE fights SET {column} = %s WHERE id = %s", (existente, bout_id))
                connection.commit()
                counts["linked"] += 1
                continue

            if athlete is None:
                counts["unresolved"] += 1
                LOGGER.info("No ESPN match for %r", name)
                continue

            counts["resolved"] += 1
            LOGGER.info("%s -> %s  %s-%s-%s  photo=%s", name, athlete.full_name, *record, bool(athlete.headshot_url))
            if dry_run:
                continue

            fighter = FighterRecord(
                name=athlete.full_name,
                nickname=athlete.nickname,
                headshot_url=athlete.headshot_url,
                nationality=athlete.nationality,
                birth_date=athlete.birth_date,
                height_cm=_clean_measure(athlete.height_cm),
                reach_cm=_clean_measure(athlete.reach_cm),
                stance=_clean_stance(athlete.stance),
                weight_grams=athlete.weight_grams,
                wins=record[0],
                losses=record[1],
                draws=record[2],
                source=ESPN_SOURCE,
                source_id=athlete.athlete_id,
            )
            fighter_id = upsert_fighter(connection, fighter)
            column = "fighter_red_id" if corner == "red" else "fighter_blue_id"
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE fights SET {column} = %s WHERE id = %s", (fighter_id, bout_id))
            connection.commit()
            counts["linked"] += 1
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Import + link unlinked upcoming-event fighters from ESPN.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    args = parser.parse_args()
    counts = link_upcoming(dry_run=args.dry_run)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
