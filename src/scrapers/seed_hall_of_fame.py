"""Seed the UFC Hall of Fame (feature /salon-de-la-fama).

Curated, fixed list of the 4 wings' inductees (verified against Wikipedia's
"UFC Hall of Fame" and ufc.com, incl. the 2026 class). Idempotent: upserts by
(wing, display_name) and (re)links fighter_id via the shared fuzzy matcher
(matching.IDENTITY_THRESHOLD), so re-running refreshes links as fighters are
added/enriched. Also seeds subtitle/photo_url/bio for the contributor and fight
wings (shown in the Salón modal). Requires migrations 013_hall_of_fame.sql and
014_hall_of_fame_bio.sql applied first.

    python -m src.scrapers.seed_hall_of_fame
"""

from __future__ import annotations

import json
import logging
from difflib import get_close_matches

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, fold, ratio

LOGGER = logging.getLogger(__name__)

# (inductee_year, display_name). Debut ANTES del 2000-11-17.
PIONEER: list[tuple[int, str]] = [
    (2003, "Royce Gracie"),
    (2003, "Ken Shamrock"),
    (2005, "Dan Severn"),
    (2006, "Randy Couture"),
    (2008, "Mark Coleman"),
    (2009, "Chuck Liddell"),
    (2010, "Matt Hughes"),
    (2012, "Tito Ortiz"),
    (2014, "Pat Miletich"),
    (2015, "Bas Rutten"),
    (2016, "Antônio Rodrigo Nogueira"),
    (2016, "Don Frye"),
    (2017, "Maurice Smith"),
    (2017, "Kazushi Sakuraba"),
    (2018, "Matt Serra"),
    (2019, "Rich Franklin"),
    (2021, "Kevin Randleman"),
    (2023, "Jens Pulver"),
    (2023, "Anderson Silva"),
    (2024, "Wanderlei Silva"),
    (2025, "Vitor Belfort"),
    (2025, "Mark Kerr"),
]

# (inductee_year, display_name). Debut ON/AFTER 2000-11-17.
MODERN: list[tuple[int, str]] = [
    (2013, "Forrest Griffin"),
    (2015, "B.J. Penn"),
    (2017, "Urijah Faber"),
    (2018, "Ronda Rousey"),
    (2019, "Rashad Evans"),
    (2019, "Michael Bisping"),
    (2021, "Georges St-Pierre"),
    (2022, "Daniel Cormier"),
    (2022, "Khabib Nurmagomedov"),
    (2023, "Donald Cerrone"),
    (2023, "José Aldo"),
    (2024, "Maurício Rua"),
    (2024, "Joanna Jędrzejczyk"),
    (2024, "Frankie Edgar"),
    (2025, "Robbie Lawler"),
    (2025, "Amanda Nunes"),
    (2026, "Dominick Cruz"),
    (2026, "Demetrious Johnson"),
    (2026, "Chris Weidman"),
]

# (inductee_year, display_name, subtitle/rol, photo_url, bio). No competidores: no
# se enlazan a fighters; su foto (curada, hospedada en la app) y bio se muestran al
# expandir la card en el Salón. photo_url None = sin foto libre disponible (iniciales).
CONTRIBUTOR: list[tuple[int, str, str, str | None, str]] = [
    (2009, 'Charles "Mask" Lewis Jr.', "Cofundador de TapouT", "/hof/charles-mask-lewis.webp",
     'Charles "Mask" Lewis Jr. cofundó en 1997 la marca de ropa TapouT, clave para '
     "popularizar las MMA. Falleció en un accidente de tráfico en 2009, el primer no "
     "luchador reconocido por el Salón de la Fama de UFC."),
    (2015, "Jeff Blatnick", "Comentarista y comisionado de UFC", None,
     "Medallista de oro olímpico en lucha grecorromana (1984), fue comentarista de UFC "
     "desde UFC 4 hasta UFC 32 y comisionado de la organización. Ayudó a impulsar las "
     'reglas unificadas y a popularizar el término "mixed martial arts".'),
    (2016, "Bob Meyrowitz", "Propietario de UFC en la era SEG", None,
     "Empresario estadounidense y cofundador de UFC. A través de Semaphore Entertainment "
     "Group (SEG) fue dueño de la promotora desde 1993 hasta venderla a Zuffa en 2001, "
     "sosteniéndola en sus años más difíciles."),
    (2017, "Joe Silva", "Matchmaker de UFC", None,
     "Joe Silva fue el matchmaker (emparejador) de UFC entre 1997 y 2016, encargado de "
     "diseñar los combates de las veladas, un papel clave en el desarrollo deportivo de "
     "la empresa."),
    (2018, "Art Davie", "Cocreador y primer matchmaker de UFC", "/hof/art-davie.webp",
     "Cocreador de UFC junto a Rorion Gracie: concibió el torneo original que dio lugar "
     "a UFC 1 en 1993 y fue el primer matchmaker de la organización."),
    (2018, "Bruce Connal", "Productor de televisión de UFC", None,
     "Productor de televisión que dirigió las retransmisiones de UFC desde UFC 17 (1998) "
     "con su empresa Concom, supervisando más de 300 eventos. Fue incluido de forma "
     "póstuma en el Salón de la Fama de UFC."),
    (2021, "Marc Ratner", "Regulador y directivo de UFC", None,
     "Fue director ejecutivo de la Comisión Atlética de Nevada antes de sumarse a UFC en "
     "2006 como responsable de asuntos regulatorios, figura clave en la legalización de "
     "las MMA en Estados Unidos."),
    (2025, "Craig Piligian", "Cocreador de The Ultimate Fighter", None,
     "Productor de televisión y fundador de Pilgrim Media Group. Junto a Dana White "
     "cocreó The Ultimate Fighter (TUF), el reality que impulsó la expansión global de "
     "UFC."),
    (2026, "Thomas Gerbasi", "Director editorial e historiador de UFC", None,
     "Thomas Gerbasi fue director editorial, redactor jefe e historiador de UFC durante "
     "casi dos décadas en UFC.com, contando las historias de miles de peleadores."),
]

# (inductee_year, display_name, corner_a, corner_b, subtitle/evento, bio).
FIGHT: list[tuple[int, str, str, str, str, str]] = [
    (2013, "Forrest Griffin vs. Stephan Bonnar I", "Forrest Griffin", "Stephan Bonnar", "The Ultimate Fighter 1 Finale",
     "Final de The Ultimate Fighter 1 (9 de abril de 2005, Spike TV): Forrest Griffin "
     "venció a Stephan Bonnar por decisión unánime en una guerra de tres asaltos. Dana "
     "White fichó a ambos y la pelea impulsó a UFC en la televisión estadounidense."),
    (2015, "Matt Hughes vs. Frank Trigg II", "Matt Hughes", "Frank Trigg", "UFC 52",
     "En UFC 52 (2005), Frank Trigg golpeó y tomó la espalda de Matt Hughes buscando el "
     "mataleón, pero Hughes escapó, lo cargó por el octágono y lo derribó. Ganó por "
     "mataleón en el primer asalto, reteniendo el título wélter."),
    (2016, "Mark Coleman vs. Pete Williams", "Mark Coleman", "Pete Williams", "UFC 17",
     "En UFC 17 (mayo de 1998), Pete Williams noqueó a Mark Coleman con una patada alta: "
     "una de las primeras victorias por KO con patada a la cabeza en la historia de UFC "
     "y el mayor batacazo de aquel año."),
    (2018, "Maurício Rua vs. Dan Henderson I", "Maurício Rua", "Dan Henderson", "UFC 139",
     'En UFC 139 (noviembre de 2011), Maurício "Shogun" Rua y Dan Henderson protagonizaron '
     "cinco asaltos de ida y vuelta. Henderson venció por decisión unánime en un combate "
     "considerado uno de los mejores de la historia de las MMA."),
    (2019, "Diego Sanchez vs. Clay Guida", "Diego Sanchez", "Clay Guida", "TUF: US vs. UK Finale",
     "En la final de TUF: US vs UK (junio de 2009), Diego Sanchez venció a Clay Guida por "
     "decisión dividida tras tres asaltos de ritmo frenético. Fue ampliamente reconocida "
     "como la Pelea del Año 2009."),
    (2021, "Jon Jones vs. Alexander Gustafsson I", "Jon Jones", "Alexander Gustafsson", "UFC 165",
     "En UFC 165 (21 de septiembre de 2013, Toronto), Jon Jones retuvo el título "
     "semipesado ante Alexander Gustafsson por decisión unánime (48-47, 48-47, 49-46), en "
     "la defensa más ajustada y disputada de su reinado."),
    (2022, "Cub Swanson vs. Doo Ho Choi", "Cub Swanson", "Doo Ho Choi", "UFC 206",
     "En UFC 206 (Toronto, diciembre de 2016), Cub Swanson venció a Doo Ho Choi por "
     "decisión unánime tras tres asaltos de guerra en peso pluma. Fue reconocida como la "
     "Pelea del Año 2016."),
    (2023, "Robbie Lawler vs. Rory MacDonald II", "Robbie Lawler", "Rory MacDonald", "UFC 189",
     "En UFC 189 (2015), Robbie Lawler retuvo el título wélter al vencer por TKO a Rory "
     "MacDonald en el quinto asalto, una guerra brutal considerada una de las mejores "
     "peleas de la historia de UFC."),
    (2024, "Anderson Silva vs. Chael Sonnen I", "Anderson Silva", "Chael Sonnen", "UFC 117",
     "En UFC 117 (Oakland, 2010), Chael Sonnen dominó cuatro asaltos y medio a Anderson "
     "Silva, pero el brasileño lo sometió con un triángulo en el quinto y retuvo el título "
     "mediano. Una de las mayores remontadas de la historia de UFC."),
    (2025, "Israel Adesanya vs. Kelvin Gastelum", "Israel Adesanya", "Kelvin Gastelum", "UFC 236",
     "En UFC 236 (2019), Israel Adesanya venció a Kelvin Gastelum por decisión unánime y "
     "ganó el título interino de peso medio. Un combate de ida y vuelta coronado por un "
     "quinto asalto brutal y reconocido como Pelea del Año."),
    (2026, "Zhang Weili vs. Joanna Jędrzejczyk I", "Zhang Weili", "Joanna Jędrzejczyk", "UFC 248",
     "En UFC 248 (marzo de 2020), Zhang Weili venció a Joanna Jędrzejczyk por decisión "
     "dividida y retuvo el título de peso paja. Aquel duelo de 25 minutos, premiado como "
     "Pelea del Año, es el primer combate femenino inducido al Fight Wing del Salón."),
]

UPSERT = """
    INSERT INTO hall_of_fame
        (wing, display_name, inductee_year, fighter_id, fighter_a_id, fighter_b_id,
         subtitle, photo_url, bio, sort_order)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (wing, display_name) DO UPDATE SET
        inductee_year = EXCLUDED.inductee_year,
        fighter_id    = EXCLUDED.fighter_id,
        fighter_a_id  = EXCLUDED.fighter_a_id,
        fighter_b_id  = EXCLUDED.fighter_b_id,
        subtitle      = EXCLUDED.subtitle,
        photo_url     = EXCLUDED.photo_url,
        bio           = EXCLUDED.bio,
        sort_order    = EXCLUDED.sort_order
"""


def _build_folded_index(connection) -> dict[str, int]:
    """{fold(name): fighter_id} for every fighter (first id wins on a fold clash)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, name FROM fighters WHERE name IS NOT NULL")
        index: dict[str, int] = {}
        for fighter_id, name in cursor.fetchall():
            index.setdefault(fold(name), int(fighter_id))
    return index


def _resolve(name: str, index: dict[str, int]) -> int | None:
    """DB fighter id by exact folded key, else fuzzy at IDENTITY_THRESHOLD."""
    key = fold(name)
    hit = index.get(key)
    if hit is not None:
        return hit
    candidates = get_close_matches(key, list(index.keys()), n=1, cutoff=IDENTITY_THRESHOLD)
    if candidates and ratio(key, candidates[0]) >= IDENTITY_THRESHOLD:
        return index[candidates[0]]
    return None


def seed(connection) -> tuple[dict[str, int], list[str]]:
    index = _build_folded_index(connection)
    counts = {"upserted": 0, "fighters_linked": 0, "fighters_unlinked": 0}
    unmatched: list[str] = []

    with connection.cursor() as cursor:
        # Alas de luchadores (pioneer/modern): enlazan a su ficha; sin foto/bio
        # propias (la card usa su headshot y navega a la ficha, no al modal).
        for wing, data in (("pioneer", PIONEER), ("modern", MODERN)):
            for order, (year, name) in enumerate(data):
                fighter_id = _resolve(name, index)
                if fighter_id is None:
                    counts["fighters_unlinked"] += 1
                    unmatched.append(f"{wing}: {name}")
                else:
                    counts["fighters_linked"] += 1
                cursor.execute(
                    UPSERT,
                    (wing, name, year, fighter_id, None, None, None, None, None, order),
                )
                counts["upserted"] += 1

        # Ala de contribuidores: no luchan (fighter_id NULL) pero tienen rol
        # (subtitle), foto curada opcional (photo_url) y bio para el modal.
        for order, (year, name, subtitle, photo_url, bio) in enumerate(CONTRIBUTOR):
            cursor.execute(
                UPSERT,
                ("contributor", name, year, None, None, None, subtitle, photo_url, bio, order),
            )
            counts["upserted"] += 1

        # Ala de peleas: dos esquinas enlazadas + evento (subtitle) + bio.
        for order, fight in enumerate(FIGHT):
            year, display_name, corner_a, corner_b, subtitle, bio = fight
            a_id = _resolve(corner_a, index)
            b_id = _resolve(corner_b, index)
            for corner_name, resolved in ((corner_a, a_id), (corner_b, b_id)):
                if resolved is None:
                    counts["fighters_unlinked"] += 1
                    unmatched.append(f"fight: {corner_name}")
                else:
                    counts["fighters_linked"] += 1
            cursor.execute(
                UPSERT,
                ("fight", display_name, year, None, a_id, b_id, subtitle, None, bio, order),
            )
            counts["upserted"] += 1

    connection.commit()
    return counts, unmatched


def main() -> None:
    configure_logging()
    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts, unmatched = seed(connection)
    print(json.dumps(counts, indent=2))
    if unmatched:
        print("SIN ENLAZAR A FICHA (esperado en contributors y pioneros sin ficha):")
        for item in unmatched:
            print(f"  - {item}")


if __name__ == "__main__":
    main()
