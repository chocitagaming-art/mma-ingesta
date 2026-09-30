"""El generador se NIEGA a escribir un CSV de entrenamiento envenenado.

EL FALLO QUE FIJAN ESTOS TESTS, medido el 20-sep-2026: `training_dataset.csv`
(generado el 27-jun) traia las 171 filas de 2026 con target=1 el 97,08 % de las
veces, contra un 44-58 % en todos los demas anos con al menos 100 filas. ufcstats
lista primero al ganador y la ingesta lo guardaba como esquina roja (arreglado en
5b8ef87, 22-ago). Como las variables son rojo-menos-azul, el resultado se colaba
en ellas; el generador escribio el CSV sin rechistar y el modelo publicado se
midio contra el. `method_training_dataset.csv` (20-jul) arrastraba la misma fuga
en sus 201 filas de 2026, aunque su target (el metodo) no la delata: por eso la
guarda del metodo mira quien gano en `fights`, no el target.

La guarda va en `main()` de los dos generadores, ANTES de escribir: si salta, el
CSV viejo se queda como estaba. Todo con datos sinteticos: aqui no se abre la
base (`load_*` y `get_settings` sustituidos) ni se escribe fuera de tmp_path.
"""

from datetime import date

import pandas as pd
import pytest

import src.prediction.features.method_output as method_output
import src.prediction.features.output as output
from src.prediction.features.dataset_guard import (
    MIN_ROWS_PER_YEAR,
    RED_WIN_RATE_BAND,
    check_method_dataset,
    check_winner_dataset,
)
from src.prediction.features.method_features import METHOD_FEATURE_COLUMNS
from src.prediction.features.method_training import MethodDatasetBuildResult
from src.prediction.features.types import FEATURE_COLUMNS

HOY = date(2026, 9, 30)
# Ids de luchador: la esquina roja de la pelea N es ROJO + N, la azul AZUL + N.
ROJO, AZUL = 10_000, 20_000


def _filas(por_ano: dict[int, tuple[int, int]]) -> list[dict]:
    """{ano: (filas, victorias_rojas)} -> filas con fight_id unico y fecha de ese ano.

    Las fechas caen entre enero y septiembre para que ninguna quede despues de HOY."""
    filas = []
    fight_id = 1
    for ano, (n, rojas) in por_ano.items():
        for i in range(n):
            filas.append(
                {
                    "fight_id": fight_id,
                    "event_date": date(ano, 1 + i % 9, 1 + i % 28),
                    "rojo_gana": 1 if i < rojas else 0,
                }
            )
            fight_id += 1
    return filas


def _dataset_ganador(por_ano: dict[int, tuple[int, int]]) -> pd.DataFrame:
    """Dataset con las columnas del CSV real: fight_id, event_date, features, target."""
    return pd.DataFrame(
        [
            {
                "fight_id": fila["fight_id"],
                "event_date": fila["event_date"],
                **{columna: 0.0 for columna in FEATURE_COLUMNS},
                "target": fila["rojo_gana"],
            }
            for fila in _filas(por_ano)
        ]
    )


def _metodo_y_peleas(
    por_ano: dict[int, tuple[int, int]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(dataset de metodo, fights_df). El target del metodo es SANO en todos los
    anos (0/1/2 repartidos); quien gano solo esta en fights_df, como en la base."""
    filas = _filas(por_ano)
    dataset = pd.DataFrame(
        [
            {
                "fight_id": fila["fight_id"],
                "event_date": fila["event_date"],
                **{columna: 0.0 for columna in METHOD_FEATURE_COLUMNS},
                "target": fila["fight_id"] % 3,
            }
            for fila in filas
        ]
    )
    peleas = pd.DataFrame(
        [
            {
                "fight_id": fila["fight_id"],
                "event_date": fila["event_date"],
                "fighter_red_id": ROJO + fila["fight_id"],
                "fighter_blue_id": AZUL + fila["fight_id"],
                "winner_id": (ROJO if fila["rojo_gana"] else AZUL) + fila["fight_id"],
            }
            for fila in filas
        ]
    )
    return dataset, peleas


SANO = {2024: (300, 150), 2025: (300, 160), 2026: (171, 90)}
# El CSV del 27-jun: 166 de 171 = 97,08 % en 2026.
ENVENENADO = {2024: (300, 150), 2025: (300, 160), 2026: (171, 166)}


class _Settings:
    database_url = "postgresql://no-usar"


@pytest.fixture
def ganador(monkeypatch, tmp_path):
    """main() del ganador sin base: construye el dataset que haya en ['dataset']."""
    estado: dict = {}

    def _construir(*_a, **_k):
        return output.DatasetBuildResult(
            dataset=estado["dataset"],
            spot_checks=[],
            total_fights_seen=len(estado["dataset"]),
            excluded_no_target=0,
            excluded_missing_history=0,
            excluded_missing_stats=0,
        )

    monkeypatch.setattr(output, "get_settings", lambda: _Settings())
    monkeypatch.setattr(output, "load_base_dataframe", lambda _url: pd.DataFrame())
    monkeypatch.setattr(output, "load_rankings_dataframe", lambda _url: pd.DataFrame())
    monkeypatch.setattr(output, "build_training_dataset", _construir)
    monkeypatch.setattr(
        output,
        "create_output_table",
        lambda *_a, **_k: pytest.fail("no debe tocar la base"),
    )
    csv = tmp_path / "training_dataset.csv"
    monkeypatch.setattr(output, "OUTPUT_CSV_PATH", csv)
    estado["csv"] = csv
    return estado


@pytest.fixture
def metodo(monkeypatch, tmp_path):
    """main() del metodo sin base: fights_df y dataset en ['peleas'] y ['dataset']."""
    estado: dict = {}

    def _construir(_peleas, _rankings):
        return MethodDatasetBuildResult(
            dataset=estado["dataset"],
            total_fights_seen=len(estado["dataset"]),
            excluded_no_method=0,
            excluded_missing_history=0,
            excluded_missing_stats=0,
        )

    monkeypatch.setattr(method_output, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        method_output, "load_base_dataframe", lambda _url: estado["peleas"]
    )
    monkeypatch.setattr(
        method_output, "load_rankings_dataframe", lambda _url: pd.DataFrame()
    )
    monkeypatch.setattr(method_output, "build_method_training_dataset", _construir)
    csv = tmp_path / "method_training_dataset.csv"
    monkeypatch.setattr(method_output, "METHOD_OUTPUT_CSV_PATH", csv)
    estado["csv"] = csv
    return estado


# --- El caso que paso de verdad, por el camino real de main() -----------------


def test_el_csv_de_junio_no_se_escribe_y_el_viejo_se_queda_como_estaba(ganador):
    ganador["csv"].write_text("el CSV viejo\n", encoding="utf-8")
    ganador["dataset"] = _dataset_ganador(ENVENENADO)

    with pytest.raises(RuntimeError, match="NO se ha escrito") as error:
        output.main()

    assert "2026" in str(error.value)
    assert "97,1 %" in str(error.value)
    assert ganador["csv"].read_text(encoding="utf-8") == "el CSV viejo\n"


def test_un_csv_sano_si_se_escribe(ganador):
    ganador["dataset"] = _dataset_ganador(SANO)

    output.main()

    escrito = pd.read_csv(ganador["csv"])
    assert len(escrito) == 771


def test_el_metodo_con_la_fuga_de_esquinas_no_se_escribe(metodo):
    """Su target (decision/ko/sumision) sale sano en 2026: la fuga solo se ve en
    quien gano, y eso lo trae fights_df."""
    metodo["csv"].write_text("el CSV viejo\n", encoding="utf-8")
    metodo["dataset"], metodo["peleas"] = _metodo_y_peleas(ENVENENADO)

    with pytest.raises(RuntimeError, match="NO se ha escrito") as error:
        method_output.main()

    assert "2026" in str(error.value)
    assert metodo["csv"].read_text(encoding="utf-8") == "el CSV viejo\n"


def test_el_metodo_sano_si_se_escribe(metodo):
    metodo["dataset"], metodo["peleas"] = _metodo_y_peleas(SANO)

    method_output.main()

    assert len(pd.read_csv(metodo["csv"])) == 771


# --- Los detalles de la guarda, sobre la funcion ----------------------------


def _comprobar(por_ano: dict[int, tuple[int, int]]) -> None:
    check_winner_dataset(_dataset_ganador(por_ano), today=HOY)


def test_un_ano_con_pocas_filas_no_dispara():
    """Un ano con menos de MIN_ROWS_PER_YEAR peleas puede salir 100 % por azar:
    en el CSV de junio, 1997 tenia 2 filas y las 2 con target=1."""
    pocas = MIN_ROWS_PER_YEAR - 1
    _comprobar({2025: (300, 150), 2026: (pocas, pocas)})


def test_la_banda_es_cerrada_en_sus_dos_bordes():
    bajo, alto = RED_WIN_RATE_BAND
    n = MIN_ROWS_PER_YEAR
    _comprobar({2026: (n, round(n * alto))})
    _comprobar({2026: (n, round(n * bajo))})
    with pytest.raises(RuntimeError, match="NO se ha escrito"):
        _comprobar({2026: (n, round(n * alto) + 1)})
    with pytest.raises(RuntimeError, match="NO se ha escrito"):
        _comprobar({2026: (n, round(n * bajo) - 1)})


def test_el_rojo_perdiendo_casi_siempre_tambien_salta():
    """La fuga al reves (azul = ganador) envenena igual."""
    with pytest.raises(RuntimeError, match="NO se ha escrito"):
        _comprobar({2025: (300, 150), 2026: (171, 5)})


def test_un_fight_id_repetido_no_se_escribe():
    dataset = _dataset_ganador(SANO)
    dataset.loc[1, "fight_id"] = dataset.loc[0, "fight_id"]

    with pytest.raises(RuntimeError, match="fight_id repetido"):
        check_winner_dataset(dataset, today=HOY)


def test_una_pelea_posterior_a_hoy_no_se_escribe():
    dataset = _dataset_ganador(SANO)
    dataset.loc[0, "event_date"] = date(2026, 10, 3)

    with pytest.raises(RuntimeError, match="posterior a hoy"):
        check_winner_dataset(dataset, today=HOY)


def test_una_pelea_de_hoy_si_vale():
    """El sabado de velada, una pelea de ese mismo dia ya tiene resultado."""
    dataset = _dataset_ganador(SANO)
    dataset.loc[0, "event_date"] = HOY

    check_winner_dataset(dataset, today=HOY)


def test_un_target_que_no_es_0_ni_1_no_se_escribe():
    dataset = _dataset_ganador(SANO)
    dataset.loc[0, "target"] = 2

    with pytest.raises(RuntimeError, match="target"):
        check_winner_dataset(dataset, today=HOY)


def test_el_metodo_acepta_sus_tres_clases_y_ninguna_mas():
    dataset, peleas = _metodo_y_peleas(SANO)
    check_method_dataset(dataset, peleas, today=HOY)

    dataset.loc[0, "target"] = 3
    with pytest.raises(RuntimeError, match="target"):
        check_method_dataset(dataset, peleas, today=HOY)


def test_en_el_metodo_los_empates_no_diluyen_la_fuga():
    """Un empate no tiene ganador (winner_id NULL): no cuenta ni a favor ni en
    contra. Si contara como derrota roja, 150 empates esconderian 100 peleas con
    el rojo ganando el 100 %."""
    dataset, peleas = _metodo_y_peleas({2025: (300, 150), 2026: (250, 250)})
    empates = peleas.index[peleas["event_date"].map(lambda d: d.year) == 2026][:150]
    peleas.loc[empates, "winner_id"] = None

    with pytest.raises(RuntimeError, match="2026"):
        check_method_dataset(dataset, peleas, today=HOY)
