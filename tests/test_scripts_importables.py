"""Los `scripts/` tienen que importar. El megatest NO los cubría, y costó caro.

EL FALLO QUE ESTO FIJA, y es del día en que se escribió. Al unificar las
sesiones de ESPN (ver `test_espn_user_agent.py`) se borró
`espn_live_results._build_session`. El grep que buscó a sus usuarios miró
`src/` y `tests/` y NO miró `scripts/`, así que se quedaron dos importándolo:
`preflight_live_results.py` y `event_readiness.py`.

El megatest salió **6/6 VERDE** con esos dos rotos —688 vitest, 755 pytest,
347 e2e ejecutados—, porque ninguna de sus seis puertas ejecuta nada de
`scripts/`. Lo cazó ejecutar el consumidor real a mano, que es justo la regla
del expediente: *la comprobación que vale es la que ejecuta el consumidor real
del dato, no la que ejecuta lo que acabas de escribir*.

Y no eran scripts cualesquiera: `preflight_live_results` y `event_readiness`
son las dos herramientas con las que se decide si una velada está lista. Se
habrían descubierto rotas la noche del evento, buscándolas para comprobar algo.

Esto es una red basta a propósito: importar un módulo ejecuta sus imports y su
código de nivel de módulo, que es exactamente donde vive esta clase de fallo.
No prueba que los scripts HAGAN lo suyo — para eso están sus propias pruebas y
el consumidor real.
"""

import importlib
import pkgutil
from pathlib import Path

import pytest


RAIZ_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# Módulos que al importarse hacen algo que no queremos en la batería (red, BD,
# credenciales). Vacío hoy: los scripts del repo dejan todo dentro de `main()`.
# Si algún día hay que excluir uno, va aquí CON su motivo escrito.
EXCLUIDOS: dict[str, str] = {}


def _modulos_de_scripts() -> list[str]:
    return sorted(
        m.name
        for m in pkgutil.iter_modules([str(RAIZ_SCRIPTS)])
        if not m.name.startswith("_")
    )


MODULOS = _modulos_de_scripts()


def test_hay_scripts_que_comprobar():
    """Si el descubridor devuelve una lista vacía, los demás tests pasan solos.

    Es el modo de fallo de este fichero: una batería parametrizada sobre una
    lista vacía sale en VERDE sin haber comprobado nada, que es la trampa que
    el expediente lleva persiguiendo desde el informe de 594 tests con 0
    ejecutados.
    """
    assert len(MODULOS) >= 10, f"solo {len(MODULOS)} módulos en scripts/: {MODULOS}"


@pytest.mark.parametrize("nombre", MODULOS)
def test_el_script_importa(nombre):
    if nombre in EXCLUIDOS:
        pytest.skip(EXCLUIDOS[nombre])

    importlib.import_module(f"scripts.{nombre}")
