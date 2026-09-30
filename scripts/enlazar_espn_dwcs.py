# -*- coding: utf-8 -*-
"""Enlaza el espn_id de las 19 fichas UFC con Contender Series que no lo tienen
(tarea t4-9-1, 30-sep-2026).

POR QUE. El historial ESPN de cada luchador (fight_history_espn) solo se
importa para fichas con fighters.espn_id. Estas 19 son source='ufcstats', nunca
se les resolvio el id de ESPN y por eso no tienen ni su Contender Series ni sus
regionales. Ademas hay ~45 filas de OTROS luchadores cuyo rival es uno de estos
19 y que quedaron con opponent_fighter_id NULL: el cron no las arreglaria,
porque esas fichas ya estan selladas y no las vuelve a visitar.

LA LISTA ES CERRADA. Cada par lleva su prueba (una linea):
  grupo 1: el mismo id de ESPN aparece enfrente de NUESTROS rivales en sus
           combates UFC (marcador de ESPN, +-1 dia) o en su carrera common/v3;
  grupo 2: la prueba es por id, pero ESPN escribe el nombre de otra forma
           (la guarda t4-9-2 ya los acepta);
  grupo 3: sin combates en `fights`; prueba por fecha de nacimiento + record
           + apodo, y cada uno tiene un homonimo en ESPN que NO es el.
FUERA a proposito: 7963 Joey Gomez (el 4357555 del DWCS es otro, nacido en
1989; el suyo es 3947131 y se deja para otra decision), 6358/7250 Jose Delgado
(el 5223435 ya lo tiene 7250: es una fusion, frente t4-3) y 6534 Michael
Aswell Jr. (ya tiene espn_id 5212738).

QUE COMPRUEBA, ficha a ficha: que existe; que el nombre y la fecha de
nacimiento son los esperados; que espn_id es NULL; que ninguna otra ficha
tiene ese id (ni en espn_id ni como source='espn'/source_id); y que
espn_history_checked_at es NULL (el cron del martes la recogera sola). Una
ficha que falla algo se SALTA y se informa; las demas siguen, y el proceso
sale en 1 para que lo mire una persona. Una ficha que ya tiene EXACTAMENTE el
id esperado se da por hecha (YA ENLAZADA): asi la segunda pasada no cambia
nada y sale en 0.

QUE ESCRIBE, en UNA transaccion: fighters.espn_id con set_fighter_espn_id
(el helper del repo: solo escribe si espn_id IS NULL y no toca el sello) y
    UPDATE fight_history_espn SET opponent_fighter_id = <ficha>
     WHERE opponent_espn_id = <id> AND opponent_fighter_id IS NULL
Si una escritura de fighters no entra (alguien la relleno entre medias), se
deshace TODO y sale en 1.

Uso (DATABASE_URL en el entorno):
    python -m scripts.enlazar_espn_dwcs              # simulacion, sesion de solo lectura
    python -m scripts.enlazar_espn_dwcs --aplicar    # escribe
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date

import psycopg2

from src.scrapers.config import get_settings
from src.scrapers.repositories.fighters import set_fighter_espn_id

if hasattr(sys.stdout, "reconfigure"):  # nombres con acentos en Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


@dataclass(frozen=True)
class Enlace:
    fighter_id: int
    espn_id: str
    nombre: str  # fighters.name tal cual estaba el 30-sep-2026
    nacimiento: date  # fighters.birth_date tal cual estaba el 30-sep-2026
    grupo: int
    prueba: str


ENLACES: tuple[Enlace, ...] = (
    # --- Grupo 1: el mismo id de ESPN frente a nuestros rivales UFC ---------
    Enlace(6302, "4049391", "Josh Hokit", date(1997, 11, 12), 1,
           "2 votos del marcador ESPN frente a sus rivales UFC; DWCS 20-ago-2025"),
    Enlace(6310, "3023804", "Steve Garcia", date(1992, 5, 22), 1,
           "7 votos del marcador ESPN frente a sus rivales UFC; DWCS 2019"),
    Enlace(6472, "5145495", "Stephanie Luciano", date(1999, 12, 16), 1,
           "3 votos del marcador ESPN frente a sus rivales UFC; DWCS 2023"),
    Enlace(6617, "4684135", "Joe Pyfer", date(1996, 9, 17), 1,
           "4 votos del marcador ESPN; DWCS 2020 y 2022 (ESPN tiene la gemela 4686568)"),
    Enlace(6883, "4875511", "Christian Rodriguez", date(1997, 12, 17), 1,
           "4 votos del marcador ESPN frente a sus rivales UFC; DWCS 2021"),
    Enlace(7136, "5007668", "Caio Machado", date(1994, 7, 20), 1,
           "1 voto del marcador ESPN frente a su rival UFC; DWCS 2023"),
    Enlace(7275, "4812389", "Shannon Ross", date(1989, 5, 12), 1,
           "1 voto del marcador ESPN frente a su rival UFC; DWCS 2022"),
    Enlace(7362, "4683396", "Cheyanne Vlismas", date(1995, 6, 25), 1,
           "1 voto del marcador ESPN frente a su rival UFC; DWCS 2020"),
    Enlace(7462, "2220951", "Greg Hardy", date(1988, 7, 28), 1,
           "6 votos del marcador ESPN frente a sus rivales UFC; DWCS 2018 (x2)"),
    Enlace(7551, "3119811", "Ray Rodriguez", date(1987, 12, 10), 1,
           "1 voto del marcador ESPN frente a su rival UFC; DWCS 2019"),
    Enlace(7596, "4044409", "Brok Weaver", date(1991, 12, 5), 1,
           "2 votos del marcador ESPN frente a sus rivales UFC; DWCS 2019"),
    Enlace(7866, "3991049", "Henrique da Silva", date(1989, 9, 1), 1,
           "2 votos del marcador ESPN frente a sus rivales UFC; DWCS 2018"),
    Enlace(1336, "4252258", "Victor Martinez", date(1991, 7, 17), 1,
           "carrera ESPN con su rival UFC Leavitt 4686565; DWCS 22-sep-2021"),
    Enlace(9075, "5310564", "Victor Valenzuela", date(1994, 2, 9), 1,
           "carrera ESPN con sus rivales UFC Nolan 5144007 y Griffin 3040385; DWCS 15-oct-2025"),
    # --- Grupo 2: prueba por id, pero ESPN escribe otro nombre -------------
    Enlace(9084, "5080485", "Jose Souza", date(2002, 5, 23), 2,
           "ESPN 'Jose Henrique'; rival UFC Ding Meng 4813565; nacido 23-05-2002 en los dos lados"),
    Enlace(8038, "3108776", "Marcio Alexandre Junior", date(1989, 5, 5), 2,
           "ESPN 'Marcio Alexandre Jr.'; 3 combates UFC con los mismos ids de rival; DWCS Brasil 2018"),
    Enlace(8219, "3024141", "Daniel Spohn", date(1984, 10, 12), 2,
           "ESPN 'Dan Spohn'; TUF 19 contra Walsh 3112019; nacido 12-10-1984 en los dos lados"),
    # --- Grupo 3: sin combates en fights; nacimiento + record + apodo ------
    Enlace(602, "5218815", "Benjamin Bennett", date(1994, 5, 8), 3,
           "8-5-1994, 7-1-0, apodo 'Mr. Alaska'; el homonimo ESPN 5093794 NO es el"),
    Enlace(639, "3120454", "Caio Bittencourt", date(1991, 5, 22), 3,
           "22-5-1991, 14-7-0, apodo 'Leao'; el homonimo ESPN 4835138 NO es el"),
)

# Nunca pueden entrar en la lista (ver la cabecera). main() aborta si entran.
FICHAS_PROHIBIDAS = frozenset({7963, 6358, 7250, 6534})
IDS_ESPN_PROHIBIDOS = frozenset({"4357555", "3947131", "5223435", "5212738"})

OK = "OK"
YA_ENLAZADA = "YA ENLAZADA"
SALTADA = "SALTADA"

SQL_FICHA = (
    "SELECT id, name, birth_date, espn_id, espn_history_checked_at "
    "FROM fighters WHERE id = %s"
)
SQL_OTRAS_CON_EL_ID = (
    "SELECT id FROM fighters WHERE id <> %s "
    "AND (espn_id = %s OR (source = 'espn' AND source_id = %s))"
)
SQL_RIVALES_PENDIENTES = (
    "SELECT count(*) FROM fight_history_espn "
    "WHERE opponent_espn_id = %s AND opponent_fighter_id IS NULL"
)
SQL_REENLAZAR_RIVALES = (
    "UPDATE fight_history_espn SET opponent_fighter_id = %s "
    "WHERE opponent_espn_id = %s AND opponent_fighter_id IS NULL"
)


@dataclass(frozen=True)
class Veredicto:
    enlace: Enlace
    estado: str
    motivo: str
    rivales_pendientes: int


def _open_connection(dsn: str, readonly: bool):
    """Sin --aplicar la SESION es de solo lectura: es Postgres quien rechaza
    cualquier escritura, no la disciplina del codigo."""
    connection = psycopg2.connect(dsn)
    connection.set_session(readonly=readonly)
    return connection


def _lista_valida() -> str | None:
    """Motivo por el que la lista no se puede usar, o None."""
    if len({e.fighter_id for e in ENLACES}) != len(ENLACES):
        return "fichas repetidas en la lista"
    if len({e.espn_id for e in ENLACES}) != len(ENLACES):
        return "ids de ESPN repetidos en la lista"
    if FICHAS_PROHIBIDAS & {e.fighter_id for e in ENLACES}:
        return "hay una ficha prohibida en la lista"
    if IDS_ESPN_PROHIBIDOS & {e.espn_id for e in ENLACES}:
        return "hay un id de ESPN prohibido en la lista"
    return None


def _evaluar(cursor, enlace: Enlace) -> Veredicto:
    cursor.execute(SQL_FICHA, (enlace.fighter_id,))
    fila = cursor.fetchone()
    cursor.execute(SQL_RIVALES_PENDIENTES, (enlace.espn_id,))
    pendientes = int(cursor.fetchone()[0])

    def saltada(motivo: str) -> Veredicto:
        return Veredicto(enlace, SALTADA, motivo, pendientes)

    if fila is None:
        return saltada("la ficha no existe")
    _, nombre, nacimiento, espn_id, sello = fila
    if nombre != enlace.nombre:
        return saltada(f"nombre {nombre!r}, se esperaba {enlace.nombre!r}")
    if nacimiento != enlace.nacimiento:
        return saltada(f"nacimiento {nacimiento}, se esperaba {enlace.nacimiento}")
    cursor.execute(SQL_OTRAS_CON_EL_ID, (enlace.fighter_id, enlace.espn_id, enlace.espn_id))
    otras = sorted(int(r[0]) for r in cursor.fetchall())
    if otras:
        return saltada(f"el id {enlace.espn_id} ya lo tiene la ficha {', '.join(map(str, otras))}")
    if espn_id == enlace.espn_id:
        return Veredicto(enlace, YA_ENLAZADA, "", pendientes)
    if espn_id is not None:
        return saltada(f"espn_id ya es {espn_id}")
    if sello is not None:
        return saltada(f"espn_history_checked_at ya es {sello}")
    return Veredicto(enlace, OK, "", pendientes)


def _imprimir(veredictos: list[Veredicto]) -> None:
    print(f"{'ficha':>6}  {'nombre':<24} {'espn_id':<8} g  {'rivales':>7}  estado")
    for v in veredictos:
        e = v.enlace
        linea = (
            f"{e.fighter_id:>6}  {e.nombre:<24} {e.espn_id:<8} {e.grupo}  "
            f"{v.rivales_pendientes:>7}  {v.estado}"
        )
        if v.motivo:
            linea += f": {v.motivo}"
        print(linea)
    cuenta = {estado: sum(1 for v in veredictos if v.estado == estado)
              for estado in (OK, YA_ENLAZADA, SALTADA)}
    reenlazables = sum(v.rivales_pendientes for v in veredictos if v.estado != SALTADA)
    print()
    print(
        f"{cuenta[OK]} OK, {cuenta[YA_ENLAZADA]} YA ENLAZADA, {cuenta[SALTADA]} SALTADA; "
        f"filas de rivales por reenlazar: {reenlazables}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enlaza el espn_id de las 19 fichas del Contender Series (t4-9-1)."
    )
    parser.add_argument("--aplicar", action="store_true", help="escribe (por defecto: simulacion)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    problema = _lista_valida()
    if problema:
        print(f"ABORTA: {problema}")
        return 2

    connection = _open_connection(get_settings().database_url, readonly=not args.aplicar)
    try:
        print(f"modo: {'APLICAR (escribe)' if args.aplicar else 'SIMULACION (sesion de solo lectura)'}")
        with connection.cursor() as cursor:
            veredictos = [_evaluar(cursor, enlace) for enlace in ENLACES]
        _imprimir(veredictos)
        hubo_saltadas = any(v.estado == SALTADA for v in veredictos)

        if not args.aplicar:
            connection.rollback()
            print("\nSIMULACION. Nada escrito. Repite con --aplicar para ejecutarlo.")
            return 1 if hubo_saltadas else 0

        enlazadas = 0
        reenlazados = 0
        with connection.cursor() as cursor:
            for v in veredictos:
                if v.estado == SALTADA:
                    continue
                e = v.enlace
                if v.estado == OK:
                    if not set_fighter_espn_id(connection, e.fighter_id, e.espn_id):
                        connection.rollback()
                        print(
                            f"\nABORTA: la ficha {e.fighter_id} no admitio el espn_id "
                            f"{e.espn_id} (alguien lo relleno entre medias). "
                            "Deshecho TODO; no se ha escrito nada."
                        )
                        return 1
                    enlazadas += 1
                if v.rivales_pendientes:
                    cursor.execute(SQL_REENLAZAR_RIVALES, (e.fighter_id, e.espn_id))
                    reenlazados += cursor.rowcount
        connection.commit()
        print(f"\nfichas enlazadas: {enlazadas}")
        print(f"rivales reenlazados: {reenlazados}")
        if hubo_saltadas:
            print("HAY FICHAS SALTADAS: revisalas a mano (salida 1).")
        return 1 if hubo_saltadas else 0
    except psycopg2.Error as exc:
        connection.rollback()
        print(f"\nABORTA por error de la base, deshecho TODO: {exc}")
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
