"""El TIPO de velada, y quien puede ser "el evento destacado".

EL PROBLEMA QUE ARREGLA. El 26-ago-2026 el "Road To UFC: Maheshate vs. Flowers"
(2 combates, sin sede, sin poster y sin cuotas) desplazo al UFC Fight Night del
sabado en la portada, en /eventos, en /en-vivo, en /ufc-hoy, en /estado y en
/directo, y ademas se llevaba por delante al centinela y al vigilante del
directo. Los dos eventos son ``promotion_id = 1``, asi que la promotora NO los
distingue: todas esas consultas ordenaban por fecha y nada mas.

DONDE VIVE LA REGLA. No aqui: en la base, en la columna generada ``events.tier``
(migracion 028). Postgres la calcula sola a partir del slug y del nombre, asi que
no se puede escribir ni desincronizar, y la ingesta NO tiene que hacer nada al
insertar un evento. Este modulo solo dice DE QUE LADO cae cada valor.

Su gemelo en la web es ``mma-app/src/lib/event-tier.ts``. Si tocas uno, mira el
otro.
"""

from __future__ import annotations

# Los que NO pueden ser el evento destacado.
#
# 'unknown' NO esta aqui, y es la decision de diseno mas importante del modulo:
# un formato que no reconozcamos se VE en la portada (molesto, visible y de un
# renglon) y nunca se esconde. El fallo contrario -- un UFC Fight Night que
# desaparece del hero, de /en-vivo y del centinela, en silencio, un sabado por la
# noche -- es mucho peor. Los 8 'unknown' de la base son veladas UFC completas y
# todas pasadas (Ultimate Japan, UFC Macao, UFC Freedom 250...).
TIERS_SECUNDARIOS: tuple[str, ...] = ("road_to_ufc", "dwcs", "tuf_series")


def evento_principal_sql(alias: str = "e") -> str:
    """El predicado SQL de "este evento puede ser el destacado".

    Va SOLO donde la pregunta es "cual es EL evento que enseno / grabo / vigilo".
    Donde la pregunta es "que eventos existen" -- la ficha del evento, el
    buscador, el cierre post-evento, el sitemap -- ponerlo es una REGRESION:
    dejaria un evento de la UFC inaccesible.
    """
    lista = ",".join(f"'{tier}'" for tier in TIERS_SECUNDARIOS)
    punto = f"{alias}." if alias else ""
    return f"{punto}tier NOT IN ({lista})"
