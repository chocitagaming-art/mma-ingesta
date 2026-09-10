# -*- coding: utf-8 -*-
"""Dos correcciones puntuales en produccion, 10-sep-2026. YA APLICADAS.

Se deja en el repo como registro de lo que se toco a mano y por que. Es
idempotente: si se vuelve a lanzar sobre una base ya corregida, aborta sin
escribir.

1. fights.id=14067 (estelar de Noche UFC 1088): odds_red y odds_blue estaban
   cruzadas. ufc.com da Jean Silva -425 / Jose Delgado +325, o sea Silva
   favorito claro; la BD tenia a Silva a 3.644 (no favorito). Se intercambian
   los dos valores, que es lo que reconstruye la linea original: el ingestor
   trajo bien los numeros y los asigno a la esquina equivocada.
   Las otras 10 parejas con cuota del mismo evento se verificaron una a una
   contra ufc.com y son correctas, asi que no se toca nada mas.

2. rankings: Valentina Shevchenko figuraba como campeona de womens_flyweight
   en el snapshot del 9-sep. Vacio el titulo por lesion (ESPN, 5-sep-2026) y
   Natalia Silva vs Wang Cong pelean por el vacante en UFC 332 el 3-oct, cosa
   que nuestra propia BD ya reflejaba (evento 1092, is_title_fight=true).
   ufc.com/rankings sigue sin actualizarlo y nuestro scraper lo copia fiel:
   el fallo es de la fuente, no del scraper. Se borra la fila de campeona del
   ultimo snapshot para que la division salga sin campeon, que es como
   `buildDivision` de mma-app representa una vacante (champion = null).

   OJO: el cron nocturno la volvera a crear mientras ufc.com no se corrija.
   El arreglo de fondo esta pendiente (ver BACKLOG).

Uso:  .venv/Scripts/python.exe scripts/fix_1088_y_vacante.py [--aplicar]
Sin --aplicar solo enseña lo que haria.
"""
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIGHT_ESTELAR = 14067


def _serializa(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def _dsn() -> str:
    ruta = os.path.join(RAIZ, ".env")
    for linea in open(ruta, encoding="utf-8"):
        if linea.startswith("DATABASE_URL="):
            return linea.split("=", 1)[1].strip().strip('"')
    raise SystemExit("No hay DATABASE_URL en .env")


def _filas(cur, sql, args=()):
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main(argv=None) -> int:
    import psycopg2  # dentro de main: importar el script no debe exigir la BD

    argv = sys.argv[1:] if argv is None else argv
    aplicar = "--aplicar" in argv

    conn = psycopg2.connect(_dsn())
    cur = conn.cursor()

    pelea = _filas(cur, "select * from fights where id = %s", (FIGHT_ESTELAR,))
    campeona = _filas(
        cur,
        """select * from rankings
           where division = 'womens_flyweight' and is_champion
             and snapshot_date = (select max(snapshot_date) from rankings)""",
    )

    if not pelea:
        print("ABORTA: no existe fights.id=%s" % FIGHT_ESTELAR)
        return 1

    f = pelea[0]
    print("1) ESTELAR fights.id=%s" % FIGHT_ESTELAR)
    print("   ahora   ->  rojo(%s) %s  |  azul(%s) %s"
          % (f["fighter_red_name"], f["odds_red"], f["fighter_blue_name"], f["odds_blue"]))

    hay_que_girar = (
        f["odds_red"] is not None
        and f["odds_blue"] is not None
        and f["odds_red"] > f["odds_blue"]
    )
    if hay_que_girar:
        print("   quedaria->  rojo(%s) %s  |  azul(%s) %s"
              % (f["fighter_red_name"], f["odds_blue"], f["fighter_blue_name"], f["odds_red"]))
    else:
        print("   el rojo ya es favorito (o falta cuota): NO se toca")

    print()
    print("2) RANKINGS campeona de womens_flyweight")
    if campeona:
        r = campeona[0]
        print("   ahora   ->  id=%s  %s  (snapshot %s)"
              % (r["id"], r["fighter_name"], r["snapshot_date"]))
        print("   quedaria->  fila borrada; division sin campeon (titulo vacante)")
    else:
        print("   no hay fila de campeona: ya esta corregido")

    if not hay_que_girar and not campeona:
        print()
        print("Nada que hacer. La base ya esta corregida.")
        conn.close()
        return 0

    if not aplicar:
        print()
        print("SIMULACION. Nada escrito. Repite con --aplicar para ejecutarlo.")
        conn.close()
        return 0

    copias = os.path.join(RAIZ, "docs", "copias")
    os.makedirs(copias, exist_ok=True)
    destino = os.path.join(copias, "backup_fix_20260910.json")
    json.dump(
        {"fight_%s" % FIGHT_ESTELAR: pelea, "ranking_campeona_mosca_f": campeona},
        open(destino, "w", encoding="utf-8"),
        default=_serializa, ensure_ascii=False, indent=1,
    )
    print()
    print("copia de seguridad ->", destino)

    if hay_que_girar:
        cur.execute(
            "update fights set odds_red = %s, odds_blue = %s, updated_at = now() where id = %s",
            (f["odds_blue"], f["odds_red"], FIGHT_ESTELAR),
        )
        print("fights actualizadas:", cur.rowcount)

    if campeona:
        cur.execute(
            """delete from rankings
               where division = 'womens_flyweight' and is_champion
                 and snapshot_date = (select max(snapshot_date) from rankings)"""
        )
        print("rankings borradas:", cur.rowcount)

    conn.commit()

    print()
    print("COMPROBACION EN CALIENTE")
    print(" ", _filas(cur,
                      "select id, fighter_red_name, odds_red, fighter_blue_name, odds_blue "
                      "from fights where id = %s", (FIGHT_ESTELAR,))[0])
    quedan = _filas(cur,
                    """select count(*) as n from rankings
                       where division = 'womens_flyweight' and is_champion
                         and snapshot_date = (select max(snapshot_date) from rankings)""")[0]["n"]
    print("  filas de campeona de mosca femenino ahora:", quedan)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
