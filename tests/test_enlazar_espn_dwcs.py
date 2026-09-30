"""`scripts/enlazar_espn_dwcs.py` (t4-9-1): enlaza el espn_id de las 19 fichas
UFC con Contender Series que no lo tenían, y reenlaza a sus rivales en
fight_history_espn.

Es una escritura en la base de producción, así que lo que se prueba es lo que
puede salir mal:

  1. Escribir creyendo que simula: sin --aplicar, cero sentencias mutantes,
     cero commits y la sesión abierta en solo lectura.
  2. Pisar una ficha que ha cambiado desde que se hizo la lista: cada
     comprobación que falla SALTA esa fila (y solo esa) y el proceso acaba en
     1 para que lo mire una persona.
  3. Enlazar a quien no toca: Joey Gomez (7963, el 4357555 es otro), los dos
     Delgado (6358/7250, eso es una fusión) y Aswell Jr. (6534, ya enlazado)
     no pueden estar en la lista.
  4. Una segunda pasada tiene que no cambiar nada.

La base es un doble EN MEMORIA con estado (fichas y filas de rivales), que
aplica los UPDATE de verdad: así la segunda pasada ve lo que dejó la primera.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import enlazar_espn_dwcs as mod


# --- La lista cerrada --------------------------------------------------------


def test_la_lista_tiene_19_fichas_y_19_ids_distintos():
    assert len(mod.ENLACES) == 19
    assert len({e.fighter_id for e in mod.ENLACES}) == 19
    assert len({e.espn_id for e in mod.ENLACES}) == 19


def test_la_lista_tiene_los_tres_grupos_de_la_prueba():
    por_grupo = {g: sum(1 for e in mod.ENLACES if e.grupo == g) for g in (1, 2, 3)}
    assert por_grupo == {1: 14, 2: 3, 3: 2}
    assert all(e.prueba.strip() for e in mod.ENLACES)


@pytest.mark.parametrize("fighter_id", [7963, 6358, 7250, 6534])
def test_fichas_prohibidas_fuera_de_la_lista(fighter_id):
    assert fighter_id not in {e.fighter_id for e in mod.ENLACES}


@pytest.mark.parametrize(
    "espn_id",
    [
        "4357555",  # el Joey Gomez del DWCS, que NO es nuestro 7963
        "3947131",  # el Joey Gomez verdadero: se queda fuera por decisión
        "5223435",  # el Delgado que ya tiene 7250 (fusión, frente t4-3)
        "5212738",  # Aswell Jr., ya enlazado
    ],
)
def test_ids_de_espn_prohibidos_fuera_de_la_lista(espn_id):
    assert espn_id not in {e.espn_id for e in mod.ENLACES}


def test_los_casos_de_la_prueba_por_nombre_distinto_estan():
    por_ficha = {e.fighter_id: e for e in mod.ENLACES}
    assert por_ficha[8219].espn_id == "3024141" and por_ficha[8219].nombre == "Daniel Spohn"
    assert por_ficha[9084].espn_id == "5080485" and por_ficha[9084].nombre == "Jose Souza"
    assert por_ficha[8038].espn_id == "3108776"
    assert por_ficha[8219].nacimiento == date(1984, 10, 12)


def test_jose_souza_esta_en_los_pares_verificados_de_la_guarda():
    """Ninguna regla general de la guarda acepta «Jose Souza» contra «Jose
    Henrique» (es el patrón de dos personas distintas): si se enlaza y el par
    no está en la lista de verificados, el cron del martes lo salta."""
    from src.scrapers.espn_fight_history import VERIFIED_IDENTITY_PAIRS

    souza = next(e for e in mod.ENLACES if e.fighter_id == 9084)
    assert (souza.fighter_id, souza.espn_id) in VERIFIED_IDENTITY_PAIRS


def test_la_prueba_de_bittencourt_no_afirma_que_el_duplicado_sea_otro():
    """El 4835138 es un perfil ESPN DUPLICADO de la misma persona (misma
    fecha, altura, peso y país; 14-6 más la derrota del DWCS da el 14-7 del
    otro), no un homónimo."""
    caio = next(e for e in mod.ENLACES if e.fighter_id == 639)
    assert "NO es el" not in caio.prueba
    assert "4835138" in caio.prueba and "duplicado" in caio.prueba
    assert "NO es el" not in (mod.__doc__ or "")


def test_las_pruebas_de_los_dos_victor_no_se_cruzan():
    """Tom Nolan (5144007, 18-may-2024) y Leavitt fueron rivales UFC de Victor
    Martinez; a Valenzuela lo prueban Griffin (3040385, 25-abr-2026), su fecha
    de nacimiento 9/2/1994 y su 14-4-0. La revisión encontró a Nolan en la
    prueba de Valenzuela."""
    por_ficha = {e.fighter_id: e for e in mod.ENLACES}
    valenzuela, martinez = por_ficha[9075], por_ficha[1336]
    assert "Nolan" not in valenzuela.prueba and "5144007" not in valenzuela.prueba
    assert "3040385" in valenzuela.prueba
    assert "9/2/1994" in valenzuela.prueba and "14-4-0" in valenzuela.prueba
    assert "5144007" in martinez.prueba and "4686565" in martinez.prueba


# --- El SQL: el doble de la base casa las sentencias por PREFIJO, así que las
# cláusulas que importan se fijan aquí, sobre las constantes que ejecuta main().


def test_el_reenlace_solo_toca_filas_sin_rival_y_de_ese_id():
    sql = _flat(mod.SQL_REENLAZAR_RIVALES)
    set_clause, where = sql.split(" WHERE ")
    assert "opponent_fighter_id IS NULL" in where
    assert "opponent_espn_id = %s" in where
    assert " OR " not in where
    # Como el upsert del repo (repositories/espn_history.py).
    assert "updated_at = NOW()" in set_clause


def test_el_recuento_de_rivales_usa_el_mismo_filtro_que_el_reenlace():
    where_cuenta = _flat(mod.SQL_RIVALES_PENDIENTES).split(" WHERE ")[1]
    where_update = _flat(mod.SQL_REENLAZAR_RIVALES).split(" WHERE ")[1]
    assert where_cuenta == where_update


def test_otras_fichas_con_el_id_mira_espn_id_y_las_sembradas_y_excluye_la_propia():
    where = _flat(mod.SQL_OTRAS_CON_EL_ID).split(" WHERE ")[1]
    assert where.startswith("id <> %s AND (")
    assert "espn_id = %s" in where
    assert "(source = 'espn' AND source_id = %s)" in where
    assert where.count("%s") == 3


# --- La lista inválida aborta ANTES de abrir la base ---------------------------


def _con_extra(extra):
    return mod.ENLACES + (extra,)


@pytest.mark.parametrize(
    "lista",
    [
        # Joey Gomez, ficha prohibida.
        lambda: _con_extra(mod.Enlace(7963, "3947130", "Joey Gomez", date(1990, 1, 1), 1, "x")),
        # El Joey Gomez del DWCS, id prohibido, en una ficha cualquiera.
        lambda: _con_extra(mod.Enlace(1, "4357555", "Nadie", date(1990, 1, 1), 1, "x")),
        # Ficha repetida.
        lambda: _con_extra(mod.Enlace(6302, "1111111", "Josh Hokit", date(1997, 11, 12), 1, "x")),
        # Id de ESPN repetido.
        lambda: _con_extra(mod.Enlace(2, "4049391", "Otro", date(1990, 1, 1), 1, "x")),
    ],
)
def test_una_lista_invalida_sale_en_2_sin_abrir_ninguna_conexion(monkeypatch, capsys, lista):
    monkeypatch.setattr(mod, "ENLACES", lista())
    abiertas = []

    def _prohibido(*args, **kwargs):
        abiertas.append(args)
        raise AssertionError("no se puede abrir la base con una lista inválida")

    monkeypatch.setattr(mod, "_open_connection", _prohibido)
    monkeypatch.setattr(mod.psycopg2, "connect", _prohibido)
    monkeypatch.setattr(mod, "get_settings", _prohibido)

    assert mod.main(["--aplicar"]) == 2
    assert mod.main([]) == 2
    assert abiertas == []
    assert "ABORTA" in capsys.readouterr().out


# --- La conexión: el solo lectura de la simulación ----------------------------


class _ConexionEspia:
    def __init__(self):
        self.sesiones = []

    def set_session(self, **kwargs):
        self.sesiones.append(kwargs)


def _abrir_con_espia(monkeypatch, readonly):
    espia = _ConexionEspia()
    monkeypatch.setattr(mod.psycopg2, "connect", lambda dsn: espia)
    assert mod._open_connection("postgresql://x/y", readonly=readonly) is espia
    return espia


def test_la_simulacion_abre_la_sesion_en_solo_lectura(monkeypatch):
    espia = _abrir_con_espia(monkeypatch, readonly=True)
    assert espia.sesiones == [{"readonly": True}]


def test_aplicar_no_pide_nunca_una_sesion_de_escritura(monkeypatch):
    """set_session(readonly=False) emite BEGIN READ WRITE y eso se salta el
    PGOPTIONS='-c default_transaction_read_only=on' de la red de seguridad:
    con --aplicar la sesión se queda con el valor por defecto del servidor."""
    espia = _abrir_con_espia(monkeypatch, readonly=False)
    assert all(s.get("readonly") is not False for s in espia.sesiones)
    assert espia.sesiones == []


# --- El doble de la base ----------------------------------------------------


def _flat(sql: str) -> str:
    return " ".join(sql.split())


class BaseEnMemoria:
    """Las fichas de la lista tal como estaban el 30-sep-2026, más las filas de
    rivales sin enlazar (una por ficha salvo que se diga otra cosa)."""

    def __init__(self):
        self.fichas = {
            e.fighter_id: {
                "name": e.nombre,
                "birth_date": e.nacimiento,
                "espn_id": None,
                "source": "ufcstats",
                "source_id": f"/fighter-details/{e.fighter_id}",
                "checked": None,
            }
            for e in mod.ENLACES
        }
        # opponent_espn_id -> [opponent_fighter_id, ...] de cada fila
        self.rivales = {e.espn_id: [None] for e in mod.ENLACES}
        self.falla_set: set[int] = set()

    def __call__(self, sql, params):
        plano = _flat(sql)
        if plano.startswith("SELECT id, name, birth_date, espn_id, espn_history_checked_at"):
            (fid,) = params
            f = self.fichas.get(fid)
            if f is None:
                return []
            return [(fid, f["name"], f["birth_date"], f["espn_id"], f["checked"])]
        if plano.startswith("SELECT id FROM fighters WHERE id <> %s"):
            fid, espn_id, espn_id2 = params
            assert espn_id == espn_id2
            return [
                (otro,) for otro, f in self.fichas.items()
                if otro != fid
                and (f["espn_id"] == espn_id or (f["source"] == "espn" and f["source_id"] == espn_id))
            ]
        if plano.startswith("SELECT count(*) FROM fight_history_espn"):
            (espn_id,) = params
            return [(sum(1 for v in self.rivales.get(espn_id, []) if v is None),)]
        if plano.startswith("UPDATE fighters SET espn_id"):
            espn_id, fid = params
            f = self.fichas.get(fid)
            if fid in self.falla_set or f is None or f["espn_id"] is not None:
                return []
            f["espn_id"] = espn_id
            return [(1,)]
        if plano.startswith("UPDATE fight_history_espn SET opponent_fighter_id"):
            fid, espn_id = params
            filas = self.rivales.get(espn_id, [])
            n = 0
            for i, v in enumerate(filas):
                if v is None:
                    filas[i] = fid
                    n += 1
            return [(1,)] * n
        raise AssertionError(f"SQL inesperada: {plano}")


@pytest.fixture
def base():
    return BaseEnMemoria()


@pytest.fixture
def lanzar(monkeypatch, fakedb, base):
    """Ejecuta main() contra la base en memoria y devuelve (código, conexión,
    readonly con el que se abrió la sesión)."""

    def _lanzar(*argv):
        abiertas = []

        def _abrir(dsn, readonly):
            conn = fakedb.Connection(base)
            abiertas.append((conn, readonly))
            return conn

        monkeypatch.setattr(mod, "_open_connection", _abrir)
        monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(database_url="x"))
        codigo = mod.main(list(argv))
        assert len(abiertas) == 1
        conn, readonly = abiertas[0]
        return codigo, conn, readonly

    return _lanzar


def _sets(conn):
    return [
        params for cur in conn.cursors for sql, params in cur.executed
        if _flat(sql).startswith("UPDATE fighters SET espn_id")
    ]


def _relinks(conn):
    return [
        params for cur in conn.cursors for sql, params in cur.executed
        if _flat(sql).startswith("UPDATE fight_history_espn SET opponent_fighter_id")
    ]


# --- Simulación --------------------------------------------------------------


def test_sin_aplicar_no_escribe_nada(lanzar, fakedb, base, capsys):
    codigo, conn, readonly = lanzar()

    assert codigo == 0
    assert readonly is True
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    assert all(f["espn_id"] is None for f in base.fichas.values())
    salida = capsys.readouterr().out
    assert "19 OK" in salida
    assert "SIMULACION" in salida


# --- Aplicar -----------------------------------------------------------------


def test_aplicar_escribe_exactamente_los_19_y_reenlaza_rivales(lanzar, fakedb, base, capsys):
    base.rivales["3024141"] = [None] * 8  # Spohn: 8 filas de rivales
    base.rivales["4683396"] = []  # Vlismas: ninguna

    codigo, conn, readonly = lanzar("--aplicar")

    assert codigo == 0
    assert readonly is False
    assert sorted(_sets(conn)) == sorted((e.espn_id, e.fighter_id) for e in mod.ENLACES)
    # Un UPDATE de rivales por cada id que tiene filas pendientes, nunca para
    # los que tienen 0 (así la segunda pasada no emite ninguno).
    esperados = sorted((e.fighter_id, e.espn_id) for e in mod.ENLACES if e.espn_id != "4683396")
    assert sorted(_relinks(conn)) == esperados
    assert conn.commits == 1
    assert conn.rollbacks == 0
    for e in mod.ENLACES:
        assert base.fichas[e.fighter_id]["espn_id"] == e.espn_id
        assert base.fichas[e.fighter_id]["checked"] is None  # el sello no se toca
    assert base.rivales["3024141"] == [8219] * 8
    # Solo tocan las dos tablas previstas, y a espn_history_checked_at jamás.
    for sentencia in fakedb.mutating_statements(conn):
        assert sentencia.startswith(("UPDATE fighters SET espn_id", "UPDATE fight_history_espn"))
        assert "espn_history_checked_at" not in sentencia
    # Lo que se ejecuta es la constante que fijan los tests del SQL.
    reenlaces = [
        _flat(sql) for cur in conn.cursors for sql, _ in cur.executed
        if _flat(sql).startswith("UPDATE fight_history_espn")
    ]
    assert reenlaces and set(reenlaces) == {_flat(mod.SQL_REENLAZAR_RIVALES)}
    salida = capsys.readouterr().out
    assert "rivales reenlazados: 25" in salida  # 17 x 1 + 8 de Spohn


def test_la_segunda_pasada_no_cambia_nada(lanzar, fakedb, base, capsys):
    primero, _, _ = lanzar("--aplicar")
    assert primero == 0
    capsys.readouterr()

    codigo, conn, _ = lanzar("--aplicar")

    assert codigo == 0
    assert fakedb.mutating_statements(conn) == []
    assert "19 YA ENLAZADA" in capsys.readouterr().out


# --- Filas que se saltan ------------------------------------------------------


def _con_espn_id_distinto(b):
    b.fichas[6302]["espn_id"] = "9999999"


def _sellada(b):
    b.fichas[6302]["checked"] = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _otro_nombre(b):
    b.fichas[6302]["name"] = "Joshua Hokit"


def _otra_fecha(b):
    b.fichas[6302]["birth_date"] = date(1997, 11, 13)


def _sin_ficha(b):
    del b.fichas[6302]


def _id_en_otra_ficha(b):
    b.fichas[424242] = {
        "name": "Josh Hokit", "birth_date": None, "espn_id": "4049391",
        "source": "ufcstats", "source_id": "x", "checked": None,
    }


def _id_en_ficha_sembrada(b):
    b.fichas[424242] = {
        "name": "Josh Hokit", "birth_date": None, "espn_id": None,
        "source": "espn", "source_id": "4049391", "checked": None,
    }


@pytest.mark.parametrize(
    ("estropear", "motivo"),
    [
        (_con_espn_id_distinto, "espn_id"),
        (_sellada, "espn_history_checked_at"),
        (_otro_nombre, "nombre"),
        (_otra_fecha, "nacimiento"),
        (_sin_ficha, "no existe"),
        (_id_en_otra_ficha, "424242"),
        (_id_en_ficha_sembrada, "424242"),
    ],
)
def test_una_ficha_que_falla_una_comprobacion_se_salta_y_sale_en_1(
    lanzar, fakedb, base, capsys, estropear, motivo
):
    estropear(base)

    codigo, conn, _ = lanzar("--aplicar")

    assert codigo == 1
    fichas_escritas = {fid for _, fid in _sets(conn)}
    assert 6302 not in fichas_escritas
    assert len(fichas_escritas) == 18  # las demás siguen adelante
    assert ("4049391" not in {eid for _, eid in _relinks(conn)})
    assert conn.commits == 1
    linea = next(
        ln for ln in capsys.readouterr().out.splitlines()
        if re.search(r"\b6302\b", ln) and "SALTADA" in ln
    )
    assert motivo in linea


def test_la_simulacion_tambien_avisa_en_1_si_algo_se_saltaria(lanzar, fakedb, base):
    _con_espn_id_distinto(base)

    codigo, conn, _ = lanzar()

    assert codigo == 1
    assert fakedb.mutating_statements(conn) == []


def test_si_una_escritura_no_entra_se_deshace_todo(lanzar, fakedb, base):
    # Carrera: entre la comprobación y el UPDATE alguien rellenó el espn_id.
    base.falla_set.add(7462)

    codigo, conn, _ = lanzar("--aplicar")

    assert codigo == 1
    assert conn.commits == 0
    assert conn.rollbacks >= 1
