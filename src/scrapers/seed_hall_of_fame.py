"""Seed the UFC Hall of Fame (feature /salon-de-la-fama).

Curated, fixed list of the 4 wings' inductees (verified against Wikipedia's
"UFC Hall of Fame" and ufc.com, incl. the 2026 class). Idempotent: upserts by
(wing, display_name) and (re)links fighter_id via the shared fuzzy matcher
(matching.IDENTITY_THRESHOLD), so re-running refreshes links as fighters are
added/enriched. Also seeds subtitle/bio for the contributor and fight wings
(shown in the Salón modal), the curated photos (photo_url + photo_focus, PHOTO)
and the full-fight video of each Fight Wing bout (video_id/start/end,
FIGHT_VIDEO). Requires migrations 013_hall_of_fame.sql, 014_hall_of_fame_bio.sql
and 031_salon_video_recortes.sql applied first (seed-hall-of-fame.yml applies
014 and 031 right before running this).

The seed is the SOURCE OF TRUTH for every column it writes: a value edited by
hand in the DB is overwritten on the next run. To change a photo or a video,
change it here.

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

# (inductee_year, display_name, subtitle/rol, bio). No competidores: no se enlazan
# a fighters; su foto (PHOTO, más abajo) y su bio se muestran al expandir la card
# en el Salón.
CONTRIBUTOR: list[tuple[int, str, str, str]] = [
    (2009, 'Charles "Mask" Lewis Jr.', "Cofundador de TapouT",
     'Charles "Mask" Lewis Jr. cofundó en 1997 la marca de ropa TapouT, clave para '
     "popularizar las MMA. Falleció en un accidente de tráfico en 2009, el primer no "
     "luchador reconocido por el Salón de la Fama de UFC."),
    (2015, "Jeff Blatnick", "Comentarista y comisionado de UFC",
     "Medallista de oro olímpico en lucha grecorromana (1984), fue comentarista de UFC "
     "desde UFC 4 hasta UFC 32 y comisionado de la organización. Ayudó a impulsar las "
     'reglas unificadas y a popularizar el término "mixed martial arts".'),
    (2016, "Bob Meyrowitz", "Propietario de UFC en la era SEG",
     "Empresario estadounidense y cofundador de UFC. A través de Semaphore Entertainment "
     "Group (SEG) fue dueño de la promotora desde 1993 hasta venderla a Zuffa en 2001, "
     "sosteniéndola en sus años más difíciles."),
    (2017, "Joe Silva", "Matchmaker de UFC",
     "Joe Silva fue el matchmaker (emparejador) de UFC entre 1997 y 2016, encargado de "
     "diseñar los combates de las veladas, un papel clave en el desarrollo deportivo de "
     "la empresa."),
    (2018, "Art Davie", "Cocreador y primer matchmaker de UFC",
     "Cocreador de UFC junto a Rorion Gracie: concibió el torneo original que dio lugar "
     "a UFC 1 en 1993 y fue el primer matchmaker de la organización."),
    (2018, "Bruce Connal", "Productor de televisión de UFC",
     "Productor de televisión que dirigió las retransmisiones de UFC desde UFC 17 (1998) "
     "con su empresa Concom, supervisando más de 300 eventos. Fue incluido de forma "
     "póstuma en el Salón de la Fama de UFC."),
    (2021, "Marc Ratner", "Regulador y directivo de UFC",
     "Fue director ejecutivo de la Comisión Atlética de Nevada antes de sumarse a UFC en "
     "2006 como responsable de asuntos regulatorios, figura clave en la legalización de "
     "las MMA en Estados Unidos."),
    (2025, "Craig Piligian", "Cocreador de The Ultimate Fighter",
     "Productor de televisión y fundador de Pilgrim Media Group. Junto a Dana White "
     "cocreó The Ultimate Fighter (TUF), el reality que impulsó la expansión global de "
     "UFC."),
    (2026, "Thomas Gerbasi", "Director editorial e historiador de UFC",
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

# ----------------------------------------------------------------- fotos curadas

# Retratos oficiales del Salón en ufc.com, estilo 382x217_2 (764×434, 22-43 KB).
# ENLAZADOS, no alojados en la app (decisión del dueño, 18-oct-2026): es lo mismo
# que ya hacemos con las caras de ufc.com de 1.534 luchadores. El 12-jul se habían
# ALOJADO 5 de estos en public/hof y se revirtieron por no tener licencia libre.
# 🪤 Las URL de estilo (?h=…&itok=…) cambian si ufc.com vuelve a subir la imagen:
# measure_photo_cutout.py guarda el http_status de cada una para enterarnos.
_UFC_HOF = "https://www.ufc.com/images/styles/382x217_2/s3/"

# (wing, display_name) -> (photo_url, photo_focus). photo_focus = punto de la CARA
# en la foto, 'X% Y%' (migración 031); None = encuadre por defecto. Medido a ojo
# sobre una rejilla del 10 % en cada retrato (18-oct-2026).
# Quien NO está aquí escribe photo_url = NULL y la web usa la cara de su ficha
# (fighters.headshot_url), su foto local de reserva o la silueta:
#   - las 6 fotos de Wikimedia de la Pionera (Miletich, Rutten, Sakuraba,
#     Randleman, Pulver, Kerr) se quedan como están, enmarcadas;
#   - Nogueira se arregla en su ficha (fighters.id 8090), no aquí.
PHOTO: dict[tuple[str, str], tuple[str, str | None]] = {
    # Pionera: no tienen cara en ESPN ni en su ficha de ufc.com. Severn sí tiene,
    # pero es una foto de galería CON fondo (medida: PNG RGBA con solo el 27 % del
    # borde transparente): el retrato del Salón es igual de oficial y encuadra mejor.
    ("pioneer", "Dan Severn"): (_UFC_HOF + "2023-11/HOF_DSevern_16x9.jpg?h=854a7be2&itok=YmKVaqQS", "51% 24%"),
    ("pioneer", "Don Frye"): (_UFC_HOF + "2023-11/HOF_DFrye_16x9.jpg?h=854a7be2&itok=62V__v7c", "57% 22%"),
    ("pioneer", "Maurice Smith"): (_UFC_HOF + "2023-11/HOF_MSmith_16x9.jpg?h=854a7be2&itok=jqEs_-tB", "48% 18%"),
    # Moderna: la foto que la web ya pintaba por su override local. Al ponerla
    # aquí la base sabe qué foto se ve de verdad, y measure_photo_cutout puede
    # medirla (es un recorte: WEBP RGBA con el 100 % del borde transparente).
    ("modern", "Forrest Griffin"): ("/fighters/forrest-griffin.webp", None),
    # Contribuidora: las 9 con el retrato del Salón de ufc.com, también Mask y
    # Davie, que hasta ahora tenían foto de Wikimedia alojada en public/hof.
    ("contributor", 'Charles "Mask" Lewis Jr.'): (_UFC_HOF + "2023-11/HOF_CMask_16x9.jpg?h=854a7be2&itok=5And1mhv", "44% 32%"),
    ("contributor", "Jeff Blatnick"): (_UFC_HOF + "2023-11/HOF_JBlatnick_16x9.jpg?h=854a7be2&itok=qTdDWXOY", "50% 17%"),
    ("contributor", "Bob Meyrowitz"): (_UFC_HOF + "2023-11/HOF_BMeyrowitz_16x9.jpg?h=854a7be2&itok=eWqGeIHL", "50% 22%"),
    ("contributor", "Joe Silva"): (_UFC_HOF + "2023-11/HOF_JSilva_16x9_Eng.jpg?h=854a7be2&itok=f6hzyJiw", "46% 38%"),
    ("contributor", "Art Davie"): (_UFC_HOF + "2023-11/HOF_ADavie_16x9.jpg?h=854a7be2&itok=sy6ZWVIe", "50% 31%"),
    ("contributor", "Bruce Connal"): (_UFC_HOF + "2023-11/HOF_BConnal_16x9.jpg?h=854a7be2&itok=kKd39Kfo", "48% 33%"),
    ("contributor", "Marc Ratner"): (_UFC_HOF + "2023-11/HOF_MRatner_16x9.jpg?h=854a7be2&itok=Q0k6v5Co", "49% 35%"),
    ("contributor", "Craig Piligian"): (
        _UFC_HOF + "2025-06/Craig-Piligian-HOF-image-clean-1920x1080.jpg?h=29c0266b&itok=YyB-hCd4", "50% 33%"),
    # 🪤 El fichero se llama «DO-NOT-USE-…» pero es un retrato real (el 5-jul se
    # tomó por un marcador provisional). ufc.com puede cambiarlo: si da error, la
    # web cae a la silueta y la medida guarda el http_status.
    ("contributor", "Thomas Gerbasi"): (
        _UFC_HOF + "2026-05/DO-NOT-USE-050926-thomas-gerbasi-hall-of-fame-announcement-HERO.jpg"
        "?h=d1cb525d&itok=OdcEKvzb", "50% 42%"),
}

# ------------------------------------------------------------ vídeo de cada pelea

# display_name (ala 'fight') -> (video_id, video_start, video_end). Segundos None =
# desde el principio / hasta el final. Comprobados el 18-oct-2026: oEmbed 200 en
# los 10 ids distintos y, con una llamada a videos.list, públicos, incrustables y
# sin bloqueo en España (solo en BR, KR o RU). Ver
# docs/ARCHIVO/planes/rediseno-2026-10-17/videos_ala_combate.md.
# REGLA DEL DUEÑO: si UFC Español tiene la pelea COMPLETA, va la suya. Hoy solo
# en 3 de 11; del resto solo tiene resúmenes de menos de 5 minutos.
# 4 peleas van dentro de un maratón: los segundos salen de sus capítulos.
FIGHT_VIDEO: dict[str, tuple[str, int | None, int | None]] = {
    # @ufc, «GREATEST FIGHTS From The Ultimate Fighter»: último capítulo.
    "Forrest Griffin vs. Stephan Bonnar I": ("R24nT158alA", 6282, None),
    # @ufc, «GREATEST UFC Hall of Fame Fights»: último capítulo. Mismo vídeo que
    # Coleman–Williams, otro tramo.
    "Matt Hughes vs. Frank Trigg II": ("E2fUq7gT8z4", 6646, None),
    "Mark Coleman vs. Pete Williams": ("E2fUq7gT8z4", 3016, 3909),
    # UFC India, pelea completa (35:35).
    "Maurício Rua vs. Dan Henderson I": ("rLuppO32rUI", None, None),
    # @ufc, «10 Fights GUARANTEED to Make You a Fight Fan»: del capítulo de la
    # pelea al siguiente.
    "Diego Sanchez vs. Clay Guida": ("x0YxW2uF3Bo", 1652, 2617),
    # UFC Español, pelea completa (27:47).
    "Jon Jones vs. Alexander Gustafsson I": ("vdY36eZZ6IY", None, None),
    "Cub Swanson vs. Doo Ho Choi": ("ZEdSfXwwA6w", None, None),
    "Robbie Lawler vs. Rory MacDonald II": ("ocsu8P4-GS4", None, None),
    "Anderson Silva vs. Chael Sonnen I": ("DpLaqnwFQEY", None, None),
    # UFC Español, «#NocheUFC Hall Of Fame Pelea: Gastelum vs. Adesanya» (26:50).
    "Israel Adesanya vs. Kelvin Gastelum": ("CUrZ9OSC-yI", None, None),
    # UFC Español, «#UFC275 Pelea Gratis: Zhang vs Jedrzejczyk» (29:05): es la
    # PRIMERA pelea, subida antes de la revancha (la 2 acabó por KO en el 2.º).
    "Zhang Weili vs. Joanna Jędrzejczyk I": ("vSMQ-_Nnk-A", None, None),
}

UPSERT = """
    INSERT INTO hall_of_fame
        (wing, display_name, inductee_year, fighter_id, fighter_a_id, fighter_b_id,
         subtitle, photo_url, photo_focus, bio, video_id, video_start, video_end,
         sort_order)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (wing, display_name) DO UPDATE SET
        inductee_year = EXCLUDED.inductee_year,
        fighter_id    = EXCLUDED.fighter_id,
        fighter_a_id  = EXCLUDED.fighter_a_id,
        fighter_b_id  = EXCLUDED.fighter_b_id,
        subtitle      = EXCLUDED.subtitle,
        photo_url     = EXCLUDED.photo_url,
        photo_focus   = EXCLUDED.photo_focus,
        bio           = EXCLUDED.bio,
        video_id      = EXCLUDED.video_id,
        video_start   = EXCLUDED.video_start,
        video_end     = EXCLUDED.video_end,
        sort_order    = EXCLUDED.sort_order
"""

# Order of the UPSERT parameters, shared by build_rows() and its tests.
COLUMNS: tuple[str, ...] = (
    "wing", "display_name", "inductee_year", "fighter_id", "fighter_a_id", "fighter_b_id",
    "subtitle", "photo_url", "photo_focus", "bio", "video_id", "video_start", "video_end",
    "sort_order",
)


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




def _photo(wing: str, name: str) -> tuple[str | None, str | None]:
    return PHOTO.get((wing, name), (None, None))


def build_rows(index: dict[str, int]) -> tuple[list[dict[str, object]], dict[str, int], list[str]]:
    """Every hall_of_fame row the seed writes, as {column: value} with COLUMNS keys.

    Pure (no DB): the fighter links come from ``index`` ({fold(name): id}). Returns
    (rows, counts, unmatched) so main() can report what could not be linked."""
    rows: list[dict[str, object]] = []
    counts = {"fighters_linked": 0, "fighters_unlinked": 0}
    unmatched: list[str] = []

    def row(**values: object) -> dict[str, object]:
        full: dict[str, object] = {column: None for column in COLUMNS}
        full.update(values)
        return full

    # Alas de luchadores (pioneer/modern): enlazan a su ficha y la card navega a
    # ella, no al modal. Sin bio; foto propia solo si está en PHOTO.
    for wing, data in (("pioneer", PIONEER), ("modern", MODERN)):
        for order, (year, name) in enumerate(data):
            fighter_id = _resolve(name, index)
            if fighter_id is None:
                counts["fighters_unlinked"] += 1
                unmatched.append(f"{wing}: {name}")
            else:
                counts["fighters_linked"] += 1
            photo_url, photo_focus = _photo(wing, name)
            rows.append(row(wing=wing, display_name=name, inductee_year=year, fighter_id=fighter_id,
                            photo_url=photo_url, photo_focus=photo_focus, sort_order=order))

    # Ala de contribuidores: no luchan (fighter_id NULL) pero tienen rol
    # (subtitle), foto (PHOTO) y bio para el modal.
    for order, (year, name, subtitle, bio) in enumerate(CONTRIBUTOR):
        photo_url, photo_focus = _photo("contributor", name)
        rows.append(row(wing="contributor", display_name=name, inductee_year=year, subtitle=subtitle,
                        photo_url=photo_url, photo_focus=photo_focus, bio=bio, sort_order=order))

    # Ala de peleas: dos esquinas enlazadas + evento (subtitle) + bio + vídeo.
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
        video = FIGHT_VIDEO.get(display_name)
        if video is None:
            # Una pelea nueva del Salón sin vídeo todavía: se siembra igual (la web
            # conserva el botón que busca en YouTube), pero que se vea en el log.
            LOGGER.warning("Pelea del Salón SIN VÍDEO en FIGHT_VIDEO: %s", display_name)
            video_id, video_start, video_end = None, None, None
        else:
            video_id, video_start, video_end = video
        rows.append(row(wing="fight", display_name=display_name, inductee_year=year,
                        fighter_a_id=a_id, fighter_b_id=b_id, subtitle=subtitle, bio=bio,
                        video_id=video_id, video_start=video_start, video_end=video_end,
                        sort_order=order))

    return rows, counts, unmatched


def seed(connection) -> tuple[dict[str, int], list[str]]:
    index = _build_folded_index(connection)
    rows, link_counts, unmatched = build_rows(index)
    counts = {"upserted": 0, **link_counts}
    with connection.cursor() as cursor:
        for values in rows:
            cursor.execute(UPSERT, tuple(values[column] for column in COLUMNS))
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
