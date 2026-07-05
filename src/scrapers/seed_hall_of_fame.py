"""Seed the UFC Hall of Fame (feature /salon-de-la-fama).

Curated, fixed list of the 4 wings' inductees (verified against Wikipedia's
"UFC Hall of Fame" and ufc.com, incl. the 2026 class). Idempotent: upserts by
(wing, display_name) and (re)links fighter_id via the shared fuzzy matcher
(matching.IDENTITY_THRESHOLD), so re-running refreshes links as fighters are
added/enriched. Requires migration db/migrations/013_hall_of_fame.sql applied
first.

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

# (inductee_year, display_name). No competidores: no se enlazan a fighters.
CONTRIBUTOR: list[tuple[int, str]] = [
    (2009, 'Charles "Mask" Lewis Jr.'),
    (2015, "Jeff Blatnick"),
    (2016, "Bob Meyrowitz"),
    (2017, "Joe Silva"),
    (2018, "Art Davie"),
    (2018, "Bruce Connal"),
    (2021, "Marc Ratner"),
    (2025, "Craig Piligian"),
    (2026, "Thomas Gerbasi"),
]

# (inductee_year, display_name, corner_a, corner_b, subtitle/evento).
FIGHT: list[tuple[int, str, str, str, str]] = [
    (2013, "Forrest Griffin vs. Stephan Bonnar I", "Forrest Griffin", "Stephan Bonnar", "The Ultimate Fighter 1 Finale"),
    (2015, "Matt Hughes vs. Frank Trigg II", "Matt Hughes", "Frank Trigg", "UFC 52"),
    (2016, "Mark Coleman vs. Pete Williams", "Mark Coleman", "Pete Williams", "UFC 17"),
    (2018, "Maurício Rua vs. Dan Henderson I", "Maurício Rua", "Dan Henderson", "UFC 139"),
    (2019, "Diego Sanchez vs. Clay Guida", "Diego Sanchez", "Clay Guida", "TUF: US vs. UK Finale"),
    (2021, "Jon Jones vs. Alexander Gustafsson I", "Jon Jones", "Alexander Gustafsson", "UFC 165"),
    (2022, "Cub Swanson vs. Doo Ho Choi", "Cub Swanson", "Doo Ho Choi", "UFC 206"),
    (2023, "Robbie Lawler vs. Rory MacDonald II", "Robbie Lawler", "Rory MacDonald", "UFC 189"),
    (2024, "Anderson Silva vs. Chael Sonnen I", "Anderson Silva", "Chael Sonnen", "UFC 117"),
    (2025, "Israel Adesanya vs. Kelvin Gastelum", "Israel Adesanya", "Kelvin Gastelum", "UFC 236"),
    (2026, "Zhang Weili vs. Joanna Jędrzejczyk I", "Zhang Weili", "Joanna Jędrzejczyk", "UFC 248"),
]

UPSERT = """
    INSERT INTO hall_of_fame
        (wing, display_name, inductee_year, fighter_id, fighter_a_id, fighter_b_id, subtitle, sort_order)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (wing, display_name) DO UPDATE SET
        inductee_year = EXCLUDED.inductee_year,
        fighter_id    = EXCLUDED.fighter_id,
        fighter_a_id  = EXCLUDED.fighter_a_id,
        fighter_b_id  = EXCLUDED.fighter_b_id,
        subtitle      = EXCLUDED.subtitle,
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
        # Alas de personas.
        for wing, data, link in (("pioneer", PIONEER, True), ("modern", MODERN, True), ("contributor", CONTRIBUTOR, False)):
            for order, (year, name) in enumerate(data):
                fighter_id = _resolve(name, index) if link else None
                if link:
                    if fighter_id is None:
                        counts["fighters_unlinked"] += 1
                        unmatched.append(f"{wing}: {name}")
                    else:
                        counts["fighters_linked"] += 1
                cursor.execute(UPSERT, (wing, name, year, fighter_id, None, None, None, order))
                counts["upserted"] += 1

        # Ala de peleas.
        for order, (year, display_name, corner_a, corner_b, subtitle) in enumerate(FIGHT):
            a_id = _resolve(corner_a, index)
            b_id = _resolve(corner_b, index)
            for corner_name, resolved in ((corner_a, a_id), (corner_b, b_id)):
                if resolved is None:
                    counts["fighters_unlinked"] += 1
                    unmatched.append(f"fight: {corner_name}")
                else:
                    counts["fighters_linked"] += 1
            cursor.execute(UPSERT, ("fight", display_name, year, None, a_id, b_id, subtitle, order))
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
