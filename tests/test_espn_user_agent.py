"""ESPN rechaza los User-Agent propios: las sesiones no deben mandar ninguno.

EL FALLO QUE ESTO FIJA, y es el peor de los que ha tenido el directo. El
8-ago-2026 —el día de una velada— `site.api.espn.com` empezó a responder 403 a
cualquier User-Agent propio. Ese host es el scoreboard, y el scoreboard es de
donde sale TODO el directo: `live-event-loop.yml` corre el mismo módulo que
`live-results.yml`, así que las tres redes del sábado (bucle, vigilante y el
job corto de cada 10 min) caían por la misma línea. 12 runs seguidos en rojo
desde la 01:50Z antes de que nadie lo mirara.

MEDIDO, alternando el UA contra el mismo endpoint con 3 s de pausa entre
peticiones para descartar rate-limiting:

    mma-ingesta/1.0 (+https://espn.com)   -> 403   403
    Mozilla/5.0 (compatible; ...)         -> 403
    python-requests/2.34.2                -> 200   200
    sin cabecera User-Agent               -> 200

No es la IP de GitHub (falla igual desde una IP doméstica), y no es el
fingerprint TLS/JA3: `curl` con su UA por defecto pasa y con uno propio no —
mismo cliente, mismo JA3, distinto resultado. Es el header, y sólo el header.
Por eso disfrazarse de navegador tampoco vale, y de ahí que este fichero
compruebe que el UA es EXACTAMENTE el de `requests` y no "algo que no sea el
nuestro": un `Mozilla/5.0` inventado pasaría esa comprobación más débil y
seguiría dando 403.

Sin red: mira las cabeceras que la sesión llevaría, no lo que ESPN conteste.
El que sí sale a la red es el smoke (`tests/smoke/`), que es el consumidor real.
"""

import pytest
import requests

from src.scrapers.consolidate_fighters import _build_session as consolidate_session
from src.scrapers.enrich_ranked import _build_session as enrich_ranked_session
from src.scrapers.espn import build_espn_session


UA_POR_DEFECTO_DE_REQUESTS = requests.utils.default_headers()["User-Agent"]


# Los tres constructores de sesión que quedan tras unificar. `enrich_ranked` es
# el que más arrastra: lo llaman ocho módulos (enrich_records_espn,
# enrich_upcoming, espn_fight_history, link_upcoming_fighters,
# refresh_fighter_records, resolve_espn_ids y el conftest de los smoke).
CONSTRUCTORES = [
    pytest.param(build_espn_session, id="espn.build_espn_session"),
    pytest.param(enrich_ranked_session, id="enrich_ranked._build_session"),
    pytest.param(consolidate_session, id="consolidate_fighters._build_session"),
]


@pytest.mark.parametrize("construir", CONSTRUCTORES)
def test_la_sesion_no_pisa_el_user_agent_de_requests(construir):
    """El UA tiene que ser el de `requests`, no uno nuestro ni uno disfrazado."""
    sesion = construir()

    assert sesion.headers["User-Agent"] == UA_POR_DEFECTO_DE_REQUESTS, (
        "Esta sesión manda un User-Agent propio y ESPN responde 403 a esos. "
        f"Manda {sesion.headers['User-Agent']!r}."
    )


@pytest.mark.parametrize("construir", CONSTRUCTORES)
def test_la_sesion_sigue_pidiendo_json(construir):
    """La unificación no puede haberse llevado por delante el `Accept`."""
    assert construir().headers["Accept"] == "application/json"


def test_enrich_ranked_acepta_settings_por_compatibilidad():
    """Ocho módulos la llaman con `settings`; la firma no puede haber cambiado.

    Se conserva el parámetro (opcional) a propósito: cambiarlo obligaba a tocar
    ocho ficheros más el día de una velada.
    """
    from src.scrapers.config import Settings

    ajustes = Settings(database_url="postgres://x", anthropic_api_key=None)

    assert enrich_ranked_session(ajustes).headers["User-Agent"] == UA_POR_DEFECTO_DE_REQUESTS
    assert enrich_ranked_session().headers["User-Agent"] == UA_POR_DEFECTO_DE_REQUESTS


def test_ningun_scraper_de_espn_construye_su_propia_sesion():
    """La regla que evita que esto vuelva: una sola sesión de ESPN, no siete.

    El fallo no fue una línea mala: fue la MISMA línea copiada en siete
    ficheros, así que al bloquearse el host había que acordarse de los siete.
    Este test falla si alguien vuelve a escribir un `User-Agent` a mano en un
    scraper de ESPN.

    🪤 El filtro es `api.espn.com`, no `espn.com`, y la diferencia la cazó este
    mismo test al verlo fallar: con `espn.com` metía a `news.py`, que va contra
    `espndeportes.espn.com` por `http_browser` — y ahí el User-Agent explícito
    es CORRECTO (`_drop_user_agent` lo retira cuando impersona Chrome, y el
    camino de respaldo sin `curl_cffi` lo necesita). Ese host bloquea por JA3 y
    no por header, que es el problema contrario al de aquí.
    """
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent / "src" / "scrapers"
    culpables = []
    for fichero in sorted(raiz.glob("*.py")):
        texto = fichero.read_text(encoding="utf-8")
        if "api.espn.com" not in texto.lower():
            continue
        for numero, linea in enumerate(texto.splitlines(), start=1):
            if '"User-Agent"' not in linea or linea.lstrip().startswith("#"):
                continue
            culpables.append(f"{fichero.name}:{numero}: {linea.strip()}")

    assert not culpables, (
        "Un scraper de ESPN vuelve a fijar su propio User-Agent. Usa "
        "`espn.build_espn_session()`; ESPN devuelve 403 a los UA propios.\n"
        + "\n".join(culpables)
    )
