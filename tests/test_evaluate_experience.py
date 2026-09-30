"""evaluate.py segmentado por experiencia UFC: debutantes, novatos y veteranos.

Por que existe. La fase 4 mete en el dataset ~2.200 peleas de debutantes, y su doble
criterio es que los veteranos NO empeoren y los debutantes MEJOREN (docs/DECISIONS.md,
20-sep y 30-sep). evaluate.py solo segmentaba por division, asaltos y era: no habia
instrumento para ver si los debutantes estropean la calibracion de los veteranos.

Los tramos son los de la fase 3 (docs/experiments/preufc-dwcs-2026-09-30): manda la
esquina con MENOS peleas UFC previas; novatos = min < 3, la regla lowConfidence de
api.py; debutante = min 0; veterano = min >= 3. Cuentan las peleas con ganador y no
canceladas, como el contador de la fase 3, y solo las de fecha ESTRICTAMENTE anterior.

Puros: marcos sinteticos, sin base de datos y sin modelo."""

from contextlib import contextmanager
import math
import warnings

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import log_loss

import src.prediction.api as api
import src.prediction.evaluate as evaluate


def _fights(filas):
    """(fight_id, fecha, rojo, azul, ganador, estado) -> el marco que lee la base."""
    return pd.DataFrame(
        filas,
        columns=[
            "fight_id",
            "event_date",
            "fighter_red_id",
            "fighter_blue_id",
            "winner_id",
            "status",
        ],
    )


def _recuento(fights):
    return evaluate.count_prior_ufc_fights(fights).set_index("fight_id")


# --- 1. Los tramos: la esquina con menos peleas decide --------------------------------


@pytest.mark.parametrize(
    "rojo, azul, tramo",
    [
        (0, 0, "debutante"),
        (0, 12, "debutante"),
        (12, 0, "debutante"),
        (1, 12, "novato"),
        (2, 2, "novato"),
        (12, 2, "novato"),
        (3, 3, "veterano"),
        (3, 40, "veterano"),
        (40, 3, "veterano"),
    ],
)
def test_la_frontera_de_cada_tramo_la_decide_la_esquina_con_menos_peleas(
    rojo, azul, tramo
):
    esperado = {
        "debutante": evaluate.TIER_DEBUTANT,
        "novato": evaluate.TIER_ROOKIE,
        "veterano": evaluate.TIER_VETERAN,
    }[tramo]

    assert evaluate.experience_tier(rojo, azul) == esperado


def test_el_umbral_de_veterano_es_el_de_lowconfidence_en_api():
    # Si alguien cambia MIN_CONFIDENT_FIGHTS en api.py, los tramos se mueven con el.
    umbral = api.MIN_CONFIDENT_FIGHTS

    assert evaluate.experience_tier(umbral - 1, 99) == evaluate.TIER_ROOKIE
    assert evaluate.experience_tier(umbral, 99) == evaluate.TIER_VETERAN


@pytest.mark.parametrize("vacio", [None, pd.NA, np.nan])
def test_sin_recuento_el_tramo_es_unknown(vacio):
    assert evaluate.experience_tier(vacio, 5) == evaluate.TIER_UNKNOWN
    assert evaluate.experience_tier(5, vacio) == evaluate.TIER_UNKNOWN


# --- 2. El recuento: solo lo ESTRICTAMENTE anterior -----------------------------------


def test_cuenta_las_peleas_estrictamente_anteriores_de_cada_esquina():
    recuento = _recuento(
        _fights(
            [
                (1, "2020-01-01", 10, 20, 10, None),
                (2, "2020-06-01", 10, 30, 30, None),
                (3, "2021-01-01", 40, 10, 10, None),
            ]
        )
    )

    # La propia pelea no cuenta: en su debut, el 10 tiene 0 previas, no 1.
    assert recuento.loc[1, "red_prior_fights"] == 0
    assert recuento.loc[1, "blue_prior_fights"] == 0
    assert recuento.loc[2, "red_prior_fights"] == 1
    assert recuento.loc[3, "blue_prior_fights"] == 2
    assert recuento.loc[3, "red_prior_fights"] == 0


def test_las_peleas_posteriores_no_cambian_el_recuento():
    pasado = [
        (1, "2020-01-01", 10, 20, 10, None),
        (2, "2020-06-01", 10, 30, 30, None),
        (3, "2021-01-01", 40, 10, 10, None),
    ]
    futuro = [
        (4, "2022-01-01", 10, 20, 20, None),
        (5, "2023-01-01", 30, 10, 10, None),
    ]

    sin_futuro = _recuento(_fights(pasado))
    con_futuro = _recuento(_fights(pasado + futuro)).loc[sin_futuro.index]

    pd.testing.assert_frame_equal(sin_futuro, con_futuro)


def test_el_recuento_no_depende_del_orden_de_entrada():
    filas = [
        (1, "2020-01-01", 10, 20, 10, None),
        (2, "2020-06-01", 10, 30, 30, None),
        (3, "2021-01-01", 40, 10, 10, None),
        (4, "2021-01-01", 20, 30, 20, None),
        (5, "2022-03-01", 20, 10, 10, None),
    ]
    referencia = _recuento(_fights(filas)).sort_index()

    for semilla in range(20):
        barajado = _fights(filas).sample(frac=1, random_state=semilla)
        pd.testing.assert_frame_equal(_recuento(barajado).sort_index(), referencia)


def test_dos_peleas_el_mismo_dia_no_se_cuentan_entre_si():
    # Torneo al estilo del UFC 1: el 1 gana dos combates el mismo dia (ids 100 y 101).
    # El contador de la fase 3 recorria (fecha, fight_id) y daba 1 previa en la 101;
    # aqui es 0, porque el fight_id no dice en que orden se pelearon.
    filas = [
        (100, "1995-04-07", 1, 2, 1, None),
        (101, "1995-04-07", 1, 3, 1, None),
        (102, "1995-04-07", 4, 5, 4, None),
        (103, "1996-01-01", 6, 1, 6, None),
    ]
    recuento = _recuento(_fights(filas))

    assert recuento.loc[100, "red_prior_fights"] == 0
    assert recuento.loc[101, "red_prior_fights"] == 0
    # Al dia siguiente del torneo, las dos ya son pasado.
    assert recuento.loc[103, "blue_prior_fights"] == 2

    # Y no depende de que id le toco a cada combate del torneo.
    intercambiados = _fights(
        [(101, *filas[0][1:]), (100, *filas[1][1:]), filas[2], filas[3]]
    )
    recuento_bis = _recuento(intercambiados)
    assert recuento_bis.loc[100, "red_prior_fights"] == 0
    assert recuento_bis.loc[101, "red_prior_fights"] == 0


def test_canceladas_y_peleas_sin_ganador_no_suman_experiencia():
    recuento = _recuento(
        _fights(
            [
                (1, "2020-01-01", 10, 20, None, "cancelled"),  # nunca se peleo
                (2, "2020-02-01", 10, 21, None, None),  # empate o sin resultado
                (3, "2020-03-01", 10, 22, 10, None),  # esta si cuenta
                (4, "2020-04-01", 10, 23, 23, "cancelled"),  # cancelada manda
                (5, "2020-05-01", 10, 24, 10, None),
            ]
        )
    )

    assert recuento.loc[5, "red_prior_fights"] == 1
    # La fila sin ganador tambien recibe su recuento (0: la cancelada no suma).
    assert recuento.loc[2, "red_prior_fights"] == 0


def test_sin_peleas_el_recuento_sale_vacio():
    vacio = evaluate.count_prior_ufc_fights(_fights([]))

    assert list(vacio.columns) == ["fight_id", "red_prior_fights", "blue_prior_fights"]
    assert vacio.empty


# --- 3. Pegar el tramo al test slice ------------------------------------------------


def test_el_tramo_se_pega_por_fight_id_sin_desordenar_las_filas():
    fights = _fights(
        [
            (1, "2020-01-01", 10, 20, 10, None),
            (2, "2020-06-01", 10, 30, 30, None),
            (3, "2020-09-01", 10, 40, 10, None),
            (4, "2021-01-01", 10, 50, 50, None),
            (5, "2021-06-01", 60, 10, 60, None),
            (6, "2021-07-01", 20, 30, 20, None),
            (7, "2021-08-01", 20, 30, 30, None),
        ]
    )
    # 5: el 10 llega con 4 previas pero el 60 debuta. 3: 2 frente a 0. 7: 2 y 2.
    test_df = pd.DataFrame({"fight_id": [5, 3, 7, 999]})

    con_tramo = evaluate.attach_experience(test_df, fights)

    assert list(con_tramo["fight_id"]) == [5, 3, 7, 999]
    assert list(con_tramo["experience_tier"]) == [
        evaluate.TIER_DEBUTANT,
        evaluate.TIER_DEBUTANT,
        evaluate.TIER_ROOKIE,
        evaluate.TIER_UNKNOWN,  # no esta en fights: sin recuento
    ]
    assert con_tramo.loc[0, "blue_prior_fights"] == 4
    assert pd.isna(con_tramo.loc[3, "red_prior_fights"])


def test_sin_datos_de_la_base_todo_sale_unknown():
    test_df = pd.DataFrame({"fight_id": [1, 2, 3]})

    con_tramo = evaluate.attach_experience(test_df, pd.DataFrame())

    assert (con_tramo["experience_tier"] == evaluate.TIER_UNKNOWN).all()
    assert con_tramo["red_prior_fights"].isna().all()


# --- 4. Las metricas de un tramo ----------------------------------------------------


def test_metricas_de_un_tramo_calculadas_a_mano():
    y = np.array([1, 0, 1, 1])
    p = np.array([0.95, 0.85, 0.32, 0.38])

    m = evaluate.segment_metrics(y, p)

    assert m["n"] == 4
    assert m["accuracy"] == pytest.approx(0.25)  # predice [1, 1, 0, 0]
    assert m["brier"] == pytest.approx((0.05**2 + 0.85**2 + 0.68**2 + 0.62**2) / 4)
    assert m["log_loss"] == pytest.approx(log_loss(y, p, labels=[0, 1]))
    assert m["log_loss"] == pytest.approx(
        -(math.log(0.95) + math.log(0.15) + math.log(0.32) + math.log(0.38)) / 4
    )
    assert m["auc"] == pytest.approx(1 / 3)  # solo 0.95 supera al 0.85 negativo
    assert m["mean_predicted"] == pytest.approx(0.625)
    assert m["observed_rate"] == pytest.approx(0.75)
    # Bins de 0.1: [0.9,1) 0.95 vs 1; [0.8,0.9) 0.85 vs 0; [0.3,0.4) 0.35 vs 1 (x2).
    assert m["ece"] == pytest.approx((0.05 + 0.85 + 2 * 0.65) / 4)


def test_un_tramo_vacio_da_n_cero_y_ninguna_metrica():
    m = evaluate.segment_metrics(np.array([], dtype=int), np.array([], dtype=float))

    assert m["n"] == 0
    for clave in (
        "accuracy",
        "log_loss",
        "brier",
        "auc",
        "mean_predicted",
        "observed_rate",
        "ece",
    ):
        assert m[clave] is None, clave


def test_un_tramo_de_una_sola_clase_no_tiene_auc_pero_si_lo_demas():
    y = np.array([1, 1, 1])
    p = np.array([0.65, 0.75, 0.25])

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # ni un UndefinedMetricWarning
        m = evaluate.segment_metrics(y, p)

    assert m["auc"] is None
    assert m["n"] == 3
    assert m["accuracy"] == pytest.approx(2 / 3)
    assert m["log_loss"] == pytest.approx(log_loss(y, p, labels=[0, 1]))
    assert m["brier"] == pytest.approx((0.35**2 + 0.25**2 + 0.75**2) / 3)
    assert m["observed_rate"] == pytest.approx(1.0)
    assert m["ece"] == pytest.approx((0.35 + 0.25 + 0.75) / 3)


# --- 5. El desglose por tramos y variantes --------------------------------------------


def _test_slice():
    """8 combates: 2 de debutante, 2 de novato y 4 de veteranos; dos variantes."""
    test_df = pd.DataFrame(
        {
            "fight_id": [1, 2, 3, 4, 5, 6, 7, 8],
            "event_date": pd.to_datetime(["2024-01-01"] * 4 + ["2025-02-01"] * 4),
            "target": [1, 0, 1, 1, 0, 1, 0, 1],
            "experience_tier": [
                evaluate.TIER_DEBUTANT,
                evaluate.TIER_DEBUTANT,
                evaluate.TIER_ROOKIE,
                evaluate.TIER_ROOKIE,
                evaluate.TIER_VETERAN,
                evaluate.TIER_VETERAN,
                evaluate.TIER_VETERAN,
                evaluate.TIER_VETERAN,
            ],
        }
    )
    variants = {
        "raw_uncalibrated": np.array([0.7, 0.4, 0.45, 0.8, 0.3, 0.9, 0.55, 0.6]),
        "symmetrized_calibrated": np.array(
            [0.6, 0.45, 0.55, 0.7, 0.35, 0.8, 0.52, 0.58]
        ),
    }
    headline = variants["symmetrized_calibrated"]
    test_df["prob"] = headline
    test_df["pred"] = (headline >= 0.5).astype(int)
    test_df["year"] = test_df["event_date"].dt.year
    test_df["era"] = test_df["year"].apply(evaluate.era_bucket)
    test_df["division"] = "Lightweight"
    test_df["rounds_segment"] = pd.array([3] * 8, dtype="Int64")
    return test_df, variants, "symmetrized_calibrated"


def _fila(filas, tramo, variante):
    (fila,) = [f for f in filas if f["tier"] == tramo and f["variant"] == variante]
    return fila


def test_el_desglose_lista_los_tramos_fijos_en_orden_aunque_esten_vacios():
    test_df, variants, _ = _test_slice()
    solo_veteranos = test_df[test_df["experience_tier"] == evaluate.TIER_VETERAN]
    solo_veteranos = solo_veteranos.reset_index(drop=True)
    variants_veteranos = {clave: valor[4:] for clave, valor in variants.items()}

    filas = evaluate.experience_breakdown(solo_veteranos, variants_veteranos)

    assert [(f["tier"], f["variant"]) for f in filas] == [
        (evaluate.TIER_DEBUTANT, "raw_uncalibrated"),
        (evaluate.TIER_DEBUTANT, "symmetrized_calibrated"),
        (evaluate.TIER_ROOKIE, "raw_uncalibrated"),
        (evaluate.TIER_ROOKIE, "symmetrized_calibrated"),
        (evaluate.ROOKIES_PHASE3, "raw_uncalibrated"),
        (evaluate.ROOKIES_PHASE3, "symmetrized_calibrated"),
        (evaluate.TIER_VETERAN, "raw_uncalibrated"),
        (evaluate.TIER_VETERAN, "symmetrized_calibrated"),
    ]
    assert _fila(filas, evaluate.TIER_DEBUTANT, "raw_uncalibrated")["n"] == 0
    assert _fila(filas, evaluate.TIER_DEBUTANT, "raw_uncalibrated")["brier"] is None
    assert _fila(filas, evaluate.TIER_VETERAN, "raw_uncalibrated")["n"] == 4


def test_cada_variante_se_mide_con_sus_propias_probabilidades_y_sus_filas():
    test_df, variants, _ = _test_slice()

    filas = evaluate.experience_breakdown(test_df, variants)

    y = test_df["target"].to_numpy()
    for clave, prob in variants.items():
        esperado = evaluate.segment_metrics(y[4:], prob[4:])
        assert _fila(filas, evaluate.TIER_VETERAN, clave) == {
            "tier": evaluate.TIER_VETERAN,
            "variant": clave,
            **esperado,
        }
        esperado = evaluate.segment_metrics(y[:2], prob[:2])
        assert _fila(filas, evaluate.TIER_DEBUTANT, clave)["brier"] == esperado["brier"]


def test_novatos_fase_3_es_debutantes_mas_novatos():
    test_df, variants, _ = _test_slice()

    filas = evaluate.experience_breakdown(test_df, variants)

    y = test_df["target"].to_numpy()
    for clave, prob in variants.items():
        fila = _fila(filas, evaluate.ROOKIES_PHASE3, clave)
        assert fila["n"] == 4
        assert fila == {
            "tier": evaluate.ROOKIES_PHASE3,
            "variant": clave,
            **evaluate.segment_metrics(y[:4], prob[:4]),
        }


def test_las_filas_sin_recuento_salen_como_unknown_y_no_entran_en_novatos():
    test_df, variants, _ = _test_slice()
    test_df.loc[[0, 4], "experience_tier"] = evaluate.TIER_UNKNOWN

    filas = evaluate.experience_breakdown(test_df, variants)

    assert _fila(filas, evaluate.TIER_UNKNOWN, "raw_uncalibrated")["n"] == 2
    assert _fila(filas, evaluate.ROOKIES_PHASE3, "raw_uncalibrated")["n"] == 3
    assert _fila(filas, evaluate.TIER_VETERAN, "raw_uncalibrated")["n"] == 3


# --- 6. Lo que se imprime y lo que va al md -------------------------------------------


def test_la_seccion_del_md_trae_el_desglose_por_experiencia():
    test_df, variants, headline = _test_slice()
    test_df = test_df[test_df["experience_tier"] != evaluate.TIER_DEBUTANT]
    test_df = test_df.reset_index(drop=True)
    variants = {clave: valor[2:] for clave, valor in variants.items()}

    seccion = evaluate.build_section(test_df, variants, headline)

    assert "### Breakdown by UFC experience" in seccion
    tabla = seccion.split("### Breakdown by UFC experience", 1)[1]
    assert "| AUC |" in tabla and "| ECE |" in tabla
    # El tramo vacio sale con n=0 y guiones, sin romper la tabla.
    assert f"| {evaluate.TIER_DEBUTANT} | raw, uncalibrated | 0 | - |" in tabla
    assert evaluate.ROOKIES_PHASE3 in tabla
    # Sigue dentro de la seccion de evaluate.py: ningun encabezado de nivel 2 nuevo.
    assert seccion.count("\n## ") == 0


def test_el_resumen_impreso_trae_el_desglose_por_experiencia(capsys):
    test_df, variants, headline = _test_slice()

    evaluate.print_summary(test_df, variants, headline)

    salida = capsys.readouterr().out
    assert "UFC experience breakdown" in salida
    for tramo in (
        evaluate.TIER_DEBUTANT,
        evaluate.TIER_ROOKIE,
        evaluate.ROOKIES_PHASE3,
        evaluate.TIER_VETERAN,
    ):
        assert tramo in salida
    assert "ece=" in salida and "auc=" in salida
    assert "no prior-fight count" not in salida


def test_el_resumen_avisa_si_falta_el_recuento(capsys):
    # Medir la fase 4 sin DATABASE_URL deja todo en Unknown: que se vea en pantalla.
    test_df, variants, headline = _test_slice()
    test_df["experience_tier"] = evaluate.TIER_UNKNOWN

    evaluate.print_summary(test_df, variants, headline)

    salida = capsys.readouterr().out
    assert "Unknown = no prior-fight count: no DATABASE_URL" in salida


# --- 7. La bandera para no escribir model_metrics.md ----------------------------------


@pytest.fixture
def _sin_modelo(monkeypatch, tmp_path):
    """main() sin dataset, sin modelo y sin tocar el model_metrics.md del repo."""
    monkeypatch.setattr(evaluate, "build_test_predictions", lambda: _test_slice())
    destino = tmp_path / "model_metrics.md"
    monkeypatch.setattr(evaluate, "METRICS_PATH", destino)
    return destino


def test_con_no_write_no_toca_model_metrics(_sin_modelo, capsys):
    evaluate.main(["--no-write"])

    assert not _sin_modelo.exists()
    salida = capsys.readouterr().out
    assert "UFC experience breakdown" in salida
    assert "--no-write" in salida


def test_sin_la_bandera_escribe_la_seccion_como_siempre(_sin_modelo):
    evaluate.main([])

    texto = _sin_modelo.read_text(encoding="utf-8")
    assert texto.startswith(evaluate.SECTION_HEADER)
    assert "### Breakdown by UFC experience" in texto


# --- 8. La lectura de la base -------------------------------------------------------


def test_sin_database_url_no_abre_ninguna_conexion(monkeypatch, fakedb):
    import src.scrapers.db as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Se registra en vez de lanzar: la lectura captura cualquier excepcion, y un
    # raise aqui quedaria tapado por ese except.
    conexiones = []

    @contextmanager
    def _registra(url):
        conexiones.append(url)
        yield fakedb.Connection(lambda _sql, _params: [])

    monkeypatch.setattr(db, "connect", _registra)

    fights = evaluate.fetch_fights_for_experience()

    assert conexiones == []
    assert fights.empty
    assert list(fights.columns) == [
        "fight_id",
        "event_date",
        "fighter_red_id",
        "fighter_blue_id",
        "winner_id",
        "status",
    ]


def test_lo_que_lee_de_la_base_sirve_para_el_recuento(monkeypatch, fakedb):
    import src.scrapers.db as db

    filas = [
        {"fight_id": 1, "event_date": pd.Timestamp("2020-01-01").date(),
         "fighter_red_id": 10, "fighter_blue_id": 20, "winner_id": 10, "status": None},
        {"fight_id": 2, "event_date": pd.Timestamp("2020-06-01").date(),
         "fighter_red_id": 10, "fighter_blue_id": 30, "winner_id": 30, "status": None},
    ]
    conexion = fakedb.Connection(lambda _sql, _params: filas)

    @contextmanager
    def _falsa(_url):
        yield conexion

    monkeypatch.setenv("DATABASE_URL", "postgresql://no-usar")
    monkeypatch.setattr(db, "connect", _falsa)

    fights = evaluate.fetch_fights_for_experience()
    recuento = evaluate.count_prior_ufc_fights(fights).set_index("fight_id")

    assert recuento.loc[2, "red_prior_fights"] == 1
    assert fakedb.mutating_statements(conexion) == []
