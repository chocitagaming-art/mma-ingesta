"""Correcciones manuales del ranking oficial, para cuando la fuente va con retraso.

POR QUÉ EXISTE ESTO. `rankings.py` copia ufc.com/rankings tal cual, y eso es lo
correcto el 99 % del tiempo. Pero ufc.com tarda días en reflejar un título vacante:
el 5-sep-2026 Shevchenko vació el de peso mosca femenino por lesión (ESPN) y la
página siguió publicándola como campeona. Nuestra propia tabla `fights` ya sabía la
verdad —Natalia Silva vs Wang Cong con `is_title_fight = true` en UFC 332—, pero el
scraper no la mira, así que escribía a la campeona equivocada cada noche. Borrarlo a
mano no servía: el cron lo rehacía.

LAS TRES REGLAS QUE HACEN QUE ESTO NO SE PUDRA. Una corrección manual es deuda, y la
deuda que nadie mira se convierte en una mentira nueva:

1. **Se retira sola.** Cada corrección lleva una guarda (`solo_si_campeon_es`). En
   cuanto ufc.com publica a otra campeona, la corrección se desactiva sin que nadie
   toque nada, y se registra que ya sobra.
2. **Caduca.** Pasada `caduca`, deja de aplicarse aunque la fuente siga mal. Es
   deliberado: preferimos volver a estar mal y enterarnos, que arrastrar para siempre
   un parche que ya nadie recuerda.
3. **Chilla.** `test_no_se_acumulan_correcciones_zombis` se pone rojo si una
   corrección lleva caducada más de 30 días.

CÓMO SE AÑADE UNA. Se escribe aquí, con su motivo y su URL, y se borra cuando la
fuente se corrige. Va en el repo y no en la base de datos a propósito: así viaja con
su porqué, se revisa como código y queda en git.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date

LOGGER = logging.getLogger(__name__)

ACCIONES = ("sin_campeon",)


@dataclass(frozen=True)
class Correccion:
    """Una corrección puntual sobre lo que publica la fuente.

    motivo / fuente     por qué existe y dónde se comprueba. Obligatorios.
    desde / caduca      ventana en la que se aplica.
    division            slug del contrato (p. ej. 'womens_flyweight').
    accion              qué hacer. Hoy solo 'sin_campeon'.
    solo_si_campeon_es  la guarda: solo se aplica si la fuente sigue publicando
                        ESTE nombre como campeón. Si publica otro, la corrección
                        ya no hace falta y se desactiva sola.
    """

    motivo: str
    fuente: str
    desde: date
    caduca: date
    division: str
    accion: str
    solo_si_campeon_es: str | None = None


# --------------------------------------------------------------------------- vivas

CORRECCIONES: list[Correccion] = [
    Correccion(
        motivo=(
            "Shevchenko se lesionó y vació el título de peso mosca femenino. ufc.com/rankings "
            "sigue publicándola como campeona. Nuestra propia tabla fights ya lo refleja: "
            "el evento 1092 (UFC 332, 3-oct-2026) tiene Natalia Silva vs Wang Cong con "
            "is_title_fight = true. Se retirará sola en cuanto ufc.com corone a la ganadora."
        ),
        fuente="https://www.espn.com/mma/story/_/id/49839558/shevchenko-injured-vacates-silva-wang-vie-belt-ufc-332",
        desde=date(2026, 9, 5),
        caduca=date(2026, 11, 1),  # UFC 332 es el 3-oct; un mes de margen para que ufc.com se ponga al día
        division="womens_flyweight",
        accion="sin_campeon",
        solo_si_campeon_es="Valentina Shevchenko",
    ),
]


# --------------------------------------------------------------------------- lógica


def _normaliza(nombre: str) -> str:
    return " ".join(str(nombre or "").split()).casefold()


def _campo(fila, nombre: str):
    """Lee un campo tanto de un dict como de un RankingRecord.

    El scraper trabaja con dataclasses y las pruebas con dicts; esto evita convertir
    de un lado a otro solo para poder filtrar cuatro campos.
    """
    if isinstance(fila, dict):
        return fila.get(nombre)
    return getattr(fila, nombre, None)


def correcciones_vigentes(
    correcciones: list[Correccion], hoy: date | None = None
) -> list[Correccion]:
    """Las que caen dentro de su ventana. Fuera de ella, la fuente manda."""
    hoy = hoy or date.today()
    return [c for c in correcciones if c.desde <= hoy <= c.caduca]


def aplicar_correcciones(
    filas: list[dict],
    correcciones: list[Correccion] | None = None,
    hoy: date | None = None,
    counts: Counter | None = None,
) -> list[dict]:
    """Devuelve las filas con las correcciones vigentes aplicadas.

    `filas` son dicts con al menos division / rank_position / is_champion /
    fighter_name, que es lo que comparten RankingRecord y el volcado del parser.
    No muta la lista de entrada.
    """
    counts = Counter() if counts is None else counts
    correcciones = CORRECCIONES if correcciones is None else correcciones

    for c in correcciones:
        if c.accion not in ACCIONES:
            raise ValueError(
                f"Acción de corrección desconocida: {c.accion!r} "
                f"(división {c.division}). Las válidas son: {', '.join(ACCIONES)}."
            )

    vigentes = correcciones_vigentes(correcciones, hoy=hoy)
    for c in correcciones:
        if c not in vigentes and (hoy or date.today()) > c.caduca:
            counts["correcciones_caducadas"] += 1
            LOGGER.warning(
                "Corrección de %s CADUCADA el %s y todavía en el fichero. "
                "Si la fuente ya está bien, bórrala; si no, renuévala a conciencia.",
                c.division, c.caduca,
            )

    if not vigentes:
        return list(filas)

    resultado = list(filas)
    for c in vigentes:
        if c.accion == "sin_campeon":
            resultado = _sin_campeon(resultado, c, counts)
    return resultado


def _sin_campeon(filas: list[dict], c: Correccion, counts: Counter) -> list[dict]:
    """Quita al campeón de una división, si la fuente sigue publicando al esperado."""
    campeones = [
        f for f in filas
        if _campo(f, "division") == c.division and _campo(f, "is_champion")
    ]

    if not campeones:
        counts["correcciones_sin_efecto"] += 1
        LOGGER.warning(
            "Corrección de %s sin efecto: la fuente no trae campeón en esa división. "
            "¿Cambió el slug, o ufc.com ya la publica vacante?",
            c.division,
        )
        return filas

    if c.solo_si_campeon_es is not None:
        publicado = _campo(campeones[0], "fighter_name") or ""
        if _normaliza(publicado) != _normaliza(c.solo_si_campeon_es):
            counts["correcciones_ya_no_necesarias"] += 1
            LOGGER.info(
                "Corrección de %s YA NO HACE FALTA: la fuente publica a %r y no a %r. "
                "Bórrala del fichero.",
                c.division, publicado, c.solo_si_campeon_es,
            )
            return filas

    # Se quita SOLO la fila de campeón de esa división. La misma persona puede seguir
    # clasificada en otra (Shevchenko sigue siendo la nº1 libra por libra sin cinturón).
    quitadas = [
        f for f in filas
        if not (_campo(f, "division") == c.division and _campo(f, "is_champion"))
    ]
    counts["correcciones_aplicadas"] += 1
    LOGGER.info(
        "Corrección aplicada en %s: se retira a %r como campeón. Motivo: %s",
        c.division, _campo(campeones[0], "fighter_name"), c.motivo,
    )
    return quitadas
