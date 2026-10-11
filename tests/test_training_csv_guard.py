"""El generador se NIEGA a escribir un CSV de entrenamiento envenenado.

EL FALLO QUE FIJAN ESTOS TESTS, medido el 20-sep-2026: `training_dataset.csv`
(generado el 27-jun) traía las 171 filas de 2026 con target=1 el 97,08 % de las
veces, contra un 44-58 % en todos los demás años con al menos 100 filas. ufcstats
lista primero al ganador y la ingesta lo guardaba como esquina roja (arreglado en
5b8ef87, 22-ago). Como las variables son rojo-menos-azul, el resultado se colaba
en ellas; el generador escribió el CSV sin rechistar y el modelo publicado se
midió contra él. `method_training_dataset.csv` (20-jul) arrastraba la misma fuga
en sus 201 filas de 2026, aunque su target (el método) no la delata: por eso la
guarda del método mira quién ganó en `fights`, no el target.

La guarda va en `main()` de los dos generadores, ANTES de escribir: si salta, el
CSV viejo se queda como estaba. Todo con datos sintéticos: aquí no se abre la
base (`load_*` y `get_settings` sustituidos), no se escribe fuera de tmp_path y
el reloj de la guarda se congela en HOY.
"""

from datetime import date

import pandas as pd
import pytest

import src.prediction.features.dataset_guard as dataset_guard
import src.prediction.features.method_output as method_output
import src.prediction.features.output as output
from src.prediction.features.dataset_guard import (
    MIN_ROWS_PER_YEAR,
    RECENT_FIGHTS,
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


class _HoyCongelado(date):
    """`date` con `today()` clavado en HOY. main() llama a la guarda sin `today`:
    sin esto, los tests de main() dependerían del reloj del ordenador, porque los
    datos sintéticos llegan hasta el 2026-09-27."""

    @classmethod
    def today(cls) -> date:
        return HOY


def _filas(
    por_ano: dict[int, tuple[int, int]],
    primer_id: int = 1,
    dia: date | None = None,
) -> list[dict]:
    """{año: (filas, victorias_rojas)} -> filas con fight_id único y fecha de ese año.

    Las fechas caen entre enero y septiembre, para que ninguna quede después de
    HOY, y las victorias rojas se reparten por todos los meses. Con `dia`, todas
    las filas caen ese día."""
    filas = []
    fight_id = primer_id
    for ano, (n, rojas) in por_ano.items():
        for i in range(n):
            filas.append(
                {
                    "fight_id": fight_id,
                    "event_date": dia or date(ano, 1 + i % 9, 1 + i % 28),
                    "rojo_gana": 1 if i < rojas else 0,
                }
            )
            fight_id += 1
    return filas


def _dataset_ganador(por_ano: dict[int, tuple[int, int]], **kwargs) -> pd.DataFrame:
    """Dataset con las columnas del CSV real: fight_id, event_date, features, target."""
    return pd.DataFrame(
        [
            {
                "fight_id": fila["fight_id"],
                "event_date": fila["event_date"],
                **{columna: 0.0 for columna in FEATURE_COLUMNS},
                "target": fila["rojo_gana"],
            }
            for fila in _filas(por_ano, **kwargs)
        ]
    )


def _metodo_y_peleas(
    por_ano: dict[int, tuple[int, int]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(dataset de método, fights_df). El target del método es SANO en todos los
    años (0/1/2 repartidos); quién ganó solo está en fights_df, como en la base."""
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
VENTANA = f"{RECENT_FIGHTS} peleas más recientes"


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

    monkeypatch.setattr(dataset_guard, "date", _HoyCongelado)
    monkeypatch.setattr(output, "get_settings", lambda: _Settings())
    monkeypatch.setattr(output, "load_base_dataframe", lambda _url: pd.DataFrame())
    monkeypatch.setattr(output, "load_rankings_dataframe", lambda _url: pd.DataFrame())
    monkeypatch.setattr(
        output, "load_espn_inputs", lambda _url, _snapshot: (pd.DataFrame(), set())
    )
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
    """main() del método sin base: fights_df y dataset en ['peleas'] y ['dataset']."""
    estado: dict = {}

    def _construir(_peleas, _rankings):
        return MethodDatasetBuildResult(
            dataset=estado["dataset"],
            total_fights_seen=len(estado["dataset"]),
            excluded_no_method=0,
            excluded_missing_history=0,
            excluded_missing_stats=0,
        )

    monkeypatch.setattr(dataset_guard, "date", _HoyCongelado)
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


# --- El caso que pasó de verdad, por el camino real de main() -----------------


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
    """Su target (decisión/KO/sumisión) sale sano en 2026: la fuga solo se ve en
    quién ganó, y eso lo trae fights_df."""
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


# --- Los detalles de la guarda, sobre la función ----------------------------


def _comprobar(por_ano: dict[int, tuple[int, int]]) -> None:
    check_winner_dataset(_dataset_ganador(por_ano), today=HOY)


def test_un_ano_antiguo_con_pocas_filas_no_dispara():
    """Un año con menos de MIN_ROWS_PER_YEAR peleas puede salir 100 % por azar: en
    el CSV de junio, 1997 tenía 2 filas y las 2 con target=1. Si queda lejos de
    las peleas más recientes, nada lo mira."""
    pocas = MIN_ROWS_PER_YEAR - 1
    _comprobar({2000: (pocas, pocas), 2024: (300, 150), 2025: (300, 160)})


@pytest.mark.parametrize(
    ("del_ano", "rojas"),
    [(96, 96), (99, 97)],
    ids=["96 de 96, el CSV de junio a 15-abr", "99 al 98 %, a finales de marzo"],
)
def test_el_ano_en_curso_con_la_fuga_salta_antes_de_llegar_a_100_peleas(
    del_ano, rojas
):
    """El mínimo de 100 filas por año dejaba ciego el año en curso hasta abril o
    mayo: el CSV de junio cortado al 15-abr (96 filas de 2026, todas rojas) se
    habría escrito. Las peleas más recientes no miran el año."""
    with pytest.raises(RuntimeError, match=VENTANA) as error:
        _comprobar({2025: (300, 150), 2026: (del_ano, rojas)})

    assert "2026: el rojo gana" not in str(error.value)


def test_una_fuga_que_empieza_a_final_de_ano_tambien_salta():
    """Setenta peleas con fuga al final de un año de 300 dejan ese año en el
    61,7 %, dentro de la banda: el año entero la diluye. Las 150 más recientes,
    no."""
    sano = _dataset_ganador({2025: (300, 150), 2026: (230, 115)})
    fuga = _dataset_ganador({2026: (70, 70)}, primer_id=10_001, dia=date(2026, 9, 29))
    dataset = pd.concat([sano, fuga], ignore_index=True)

    with pytest.raises(RuntimeError, match=VENTANA) as error:
        check_winner_dataset(dataset, today=HOY)

    assert "2026: el rojo gana" not in str(error.value)


def test_la_banda_es_cerrada_en_sus_dos_bordes():
    bajo, alto = RED_WIN_RATE_BAND
    n = MIN_ROWS_PER_YEAR
    _comprobar({2026: (n, round(n * alto))})
    _comprobar({2026: (n, round(n * bajo))})
    with pytest.raises(RuntimeError, match="NO se ha escrito"):
        _comprobar({2026: (n, round(n * alto) + 1)})
    with pytest.raises(RuntimeError, match="NO se ha escrito"):
        _comprobar({2026: (n, round(n * bajo) - 1)})


def test_la_ventana_es_cerrada_en_sus_dos_bordes():
    """Justo 150 filas, 70 de 2025 y 80 de 2026: ningún año llega a las 100 de la
    comprobación por año, así que solo mira la ventana, y la ventana son todas.
    105 de 150 es el 70 % y 45 de 150 el 30 %: los dos bordes valen."""
    assert (RECENT_FIGHTS, RED_WIN_RATE_BAND) == (150, (0.30, 0.70))
    _comprobar({2025: (70, 49), 2026: (80, 56)})
    _comprobar({2025: (70, 21), 2026: (80, 24)})
    with pytest.raises(RuntimeError, match=VENTANA):
        _comprobar({2025: (70, 49), 2026: (80, 57)})
    with pytest.raises(RuntimeError, match=VENTANA):
        _comprobar({2025: (70, 21), 2026: (80, 23)})


def test_el_rojo_perdiendo_casi_siempre_tambien_salta():
    """La fuga al revés (azul = ganador) envenena igual."""
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
    """El sábado de velada, una pelea de ese mismo día ya tiene resultado."""
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
    contra. En 2026 el rojo gana 141 de las 200 peleas con ganador (70,5 %, fuera
    de la banda); si los 10 empates contaran como derrotas rojas, serían 141 de
    210 (67,1 %) y la fuga pasaría."""
    dataset, peleas = _metodo_y_peleas({2025: (300, 150), 2026: (210, 151)})
    del_2026 = peleas.index[peleas["event_date"].map(lambda d: d.year) == 2026]
    peleas.loc[del_2026[141:151], "winner_id"] = None

    with pytest.raises(RuntimeError, match="2026: el rojo gana el 70,5 %"):
        check_method_dataset(dataset, peleas, today=HOY)


@pytest.mark.parametrize(
    "romper",
    [
        lambda peleas: peleas.assign(fight_id=peleas["fight_id"].astype(str)),
        lambda peleas: peleas.iloc[:0],
    ],
    ids=["fight_id de otro tipo en fights", "fights sin filas"],
)
def test_el_metodo_sin_cruce_con_fights_no_se_escribe(romper):
    """Si el cruce con fights no encuentra quién ganó, ninguna fila tiene ganador
    conocido, la comprobación por año no ve nada y la guarda dejaba pasar la
    fuga en silencio. Sin ganador conocido, la guarda se niega."""
    dataset, peleas = _metodo_y_peleas(ENVENENADO)

    with pytest.raises(RuntimeError, match="ganador conocido"):
        check_method_dataset(dataset, romper(peleas), today=HOY)


def test_el_metodo_sin_ganador_en_las_peleas_antiguas_no_se_escribe():
    """Si el cruce solo falla en las peleas viejas (todo 2024 sin ganador), la
    ventana no lo ve y la comprobación por año se salta ese año sin avisar. El
    total sí: 471 de 771 filas con ganador (61,1 %)."""
    dataset, peleas = _metodo_y_peleas(SANO)
    de_2024 = peleas["event_date"].map(lambda d: d.year) == 2024
    peleas.loc[de_2024, "winner_id"] = None

    with pytest.raises(RuntimeError, match="solo 471 de 771 filas") as error:
        check_method_dataset(dataset, peleas, today=HOY)

    assert VENTANA not in str(error.value)


def test_el_metodo_sin_ganador_en_las_peleas_recientes_no_se_escribe():
    """Si solo las 30 peleas más recientes se quedan sin ganador, en total sigue
    habiendo un 96 % con ganador, pero la ventana miraría peleas más viejas y no
    vería una fuga nueva: la ventana también pide el 90 %."""
    dataset, peleas = _metodo_y_peleas(SANO)
    recientes = peleas.sort_values(["event_date", "fight_id"]).index[-30:]
    peleas.loc[recientes, "winner_id"] = None

    with pytest.raises(RuntimeError, match=f"{VENTANA}.*ganador conocido"):
        check_method_dataset(dataset, peleas, today=HOY)
