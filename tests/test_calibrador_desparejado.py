"""La bomba del calibrador.

Un calibrador (CalibratedClassifierCV sobre FrozenEstimator(model), ver
calibrate.py) NO es un mapa suelto: lleva DENTRO su propia copia del modelo con
el que se calibro. api.py sirve `calibrator or model`, y train.py guardaba el
modelo nuevo conservando el calibrador viejo. Resultado: reentrenar sin
recalibrar dejaba produccion sirviendo el modelo ANTERIOR, en silencio, con los
tests y el despliegue en verde.

Aqui se prueba, con modelos pequenos DE VERDAD sobre datos sinteticos y con el
model.joblib commiteado:
- la funcion que dice si un calibrador envuelve EXACTAMENTE un modelo;
- que el bundle commiteado esta bien emparejado, y que esa guarda se pone roja
  con un reentreno sin recalibrar;
- que train.py ya no deja sobrevivir el calibrador viejo, y lo avisa;
- que api.py no sirve con un calibrador que no le corresponde;
- y que con el bundle de hoy las predicciones salen EXACTAMENTE iguales.

Nada de red ni de base de datos: lo que iria a Neon esta sustituido.
"""

import copy
import logging
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from src.prediction import api, train
from src.prediction.bundle_io import (
    CALIBRATED_MODELS,
    DISCARDED_CALIBRATORS_KEY,
    calibrator_wraps_model,
    save_bundle_preserving,
    stale_calibrators,
)
from src.prediction.calibrate import fit_prefit_calibrator
from src.prediction.features import (
    FEATURE_COLUMNS,
    FighterHistorySummary,
    build_feature_row,
)
from src.prediction.features.method_features import (
    METHOD_CLASSES,
    METHOD_FEATURE_COLUMNS,
    build_method_feature_row,
)
from src.prediction.split import TEST_END
from src.prediction.train_method import select_calibration

# Ruta absoluta: MODEL_PATH de api/train es relativa al directorio de trabajo.
MODELO_COMMITEADO = (
    Path(__file__).resolve().parents[1] / "src" / "prediction" / "model.joblib"
)
GANADOR = next(p for p in CALIBRATED_MODELS if p.calibrator_key == "calibrator")
RECALIBRAR = "python -m src.prediction.calibrate"


# --- Modelos pequenos de verdad ----------------------------------------------
# Con un solo hilo, XGBoost da el mismo modelo entreno tras entreno en cualquier
# maquina: el test de "mismo entreno, otro objeto" no depende de las CPUs del CI.


def _datos(semilla: int, n_columnas: int, n_filas: int = 240):
    rng = np.random.default_rng(semilla)
    x = rng.normal(size=(n_filas, n_columnas))
    y = (x[:, 0] - x[:, 1] + rng.normal(scale=0.8, size=n_filas) > 0).astype(int)
    return x, y


def _xgb(x, y) -> XGBClassifier:
    modelo = XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        n_estimators=12,
        max_depth=2,
        random_state=42,
        n_jobs=1,
    )
    modelo.fit(x, y)
    return modelo


def _xgb_metodo(x, y) -> XGBClassifier:
    modelo = XGBClassifier(
        objective="multi:softprob",
        eval_metric="mlogloss",
        n_estimators=8,
        max_depth=2,
        random_state=42,
        n_jobs=1,
    )
    modelo.fit(x, y)
    return modelo


def _lineal(x, y) -> Pipeline:
    # La mitad lineal del ensemble de metodo, como la monta train_method.py.
    modelo = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("logreg", LogisticRegression(C=0.001, max_iter=8000)),
        ]
    )
    modelo.fit(x, y)
    return modelo


@pytest.fixture(scope="module")
def ganador() -> SimpleNamespace:
    """Un modelo, su calibrador (hecho como en calibrate.py) y un reentreno."""
    x, y = _datos(1, n_columnas=6)
    viejo = _xgb(x, y)
    return SimpleNamespace(
        x=x,
        y=y,
        viejo=viejo,
        calibrador=fit_prefit_calibrator(viejo, "sigmoid", x, y),
        reentrenado=_xgb(*_datos(2, n_columnas=6)),
    )


# --- La funcion pura ----------------------------------------------------------


def test_calibrado_sobre_el_mismo_modelo_coincide(ganador):
    assert calibrator_wraps_model(ganador.calibrador, ganador.viejo) is True


def test_reentrenar_sin_recalibrar_no_coincide(ganador):
    assert calibrator_wraps_model(ganador.calibrador, ganador.reentrenado) is False


def test_compara_el_contenido_y_no_la_identidad(ganador, tmp_path):
    """Dos objetos distintos con el mismo modelo dentro son el mismo modelo."""
    gemelo = _xgb(ganador.x, ganador.y)  # mismo entreno, otro objeto
    ruta = tmp_path / "copia.joblib"
    joblib.dump(ganador.viejo, ruta)
    copia = joblib.load(ruta)  # lo que pasa entre ficheros distintos

    assert gemelo is not ganador.viejo and copia is not ganador.viejo
    assert calibrator_wraps_model(ganador.calibrador, gemelo) is True
    assert calibrator_wraps_model(ganador.calibrador, copia) is True


def test_la_bomba_reentrenar_sin_recalibrar_no_llega_a_produccion(ganador):
    """El porque de todo esto, con la misma expresion que usa api.py al servir:
    tras cambiar el modelo, lo servido no cambia NI UN BIT."""
    antes = {"model": ganador.viejo, "calibrator": ganador.calibrador}
    despues = {**antes, "model": ganador.reentrenado}  # el calibrador se conservo

    def servido(bundle):
        estimador = bundle.get("calibrator") or bundle["model"]
        return estimador.predict_proba(ganador.x)[:, 1]

    assert np.array_equal(servido(despues), servido(antes))
    # ...y eso que el modelo nuevo predice otra cosa.
    assert not np.allclose(
        ganador.reentrenado.predict_proba(ganador.x)[:, 1],
        ganador.viejo.predict_proba(ganador.x)[:, 1],
    )


def test_evaluate_mide_lo_mismo_que_sirve_produccion(ganador, tmp_path, monkeypatch):
    """evaluate.py tiene su propio cargador: si no descartara el calibrador
    desparejado como api.py, su variante «calibrada» mediria el modelo VIEJO que
    el calibrador lleva dentro mientras produccion sirve el nuevo sin calibrar, y
    es el instrumento con el que se juzga la fase 4."""
    import src.prediction.evaluate as evaluate

    ruta = tmp_path / "model.joblib"
    base = {"imputer": SimpleImputer(), "feature_columns": ["a"]}
    monkeypatch.setattr(evaluate, "MODEL_PATH", ruta)

    joblib.dump({**base, "model": ganador.reentrenado, "calibrator": ganador.calibrador}, ruta)
    assert "calibrator" not in evaluate.load_model_bundle()

    joblib.dump({**base, "model": ganador.viejo, "calibrator": ganador.calibrador}, ruta)
    assert "calibrator" in evaluate.load_model_bundle()


def test_las_dos_parejas_del_modelo_de_metodo():
    """Multiclase y mitad lineal, calibradas con la funcion de train_method.py."""
    x, _ = _datos(3, n_columnas=5, n_filas=300)
    y = np.random.default_rng(3).integers(0, len(METHOD_CLASSES), size=300)
    x_otro, _ = _datos(4, n_columnas=5, n_filas=300)
    y_otro = np.random.default_rng(4).integers(0, len(METHOD_CLASSES), size=300)
    arboles, lineal = _xgb_metodo(x, y), _lineal(x, y)
    calibrador_arboles, _, _ = select_calibration(arboles, x, y)
    calibrador_lineal, _, _ = select_calibration(lineal, x, y)

    arboles_nuevos, lineal_nueva = _xgb_metodo(x_otro, y_otro), _lineal(x_otro, y_otro)

    assert calibrator_wraps_model(calibrador_arboles, arboles) is True
    assert calibrator_wraps_model(calibrador_lineal, lineal) is True
    assert calibrator_wraps_model(calibrador_arboles, arboles_nuevos) is False
    assert calibrator_wraps_model(calibrador_lineal, lineal_nueva) is False
    assert calibrator_wraps_model(calibrador_arboles, lineal) is False


def test_un_calibrador_con_modelos_propios_no_envuelve_el_del_bundle(ganador):
    """Con validacion cruzada, CalibratedClassifierCV entrena SUS copias por
    pliegue y predice con ellas: no sirve el modelo que tiene al lado."""
    por_pliegues = CalibratedClassifierCV(ganador.viejo, cv=3, method="sigmoid")
    por_pliegues.fit(ganador.x, ganador.y)
    assert calibrator_wraps_model(por_pliegues, ganador.viejo) is False


def test_lo_que_no_es_un_calibrador_ajustado_no_envuelve_nada(ganador):
    sin_ajustar = CalibratedClassifierCV(ganador.viejo)
    assert calibrator_wraps_model(None, ganador.viejo) is False
    assert calibrator_wraps_model("tampoco", ganador.viejo) is False
    assert calibrator_wraps_model(sin_ajustar, ganador.viejo) is False
    assert calibrator_wraps_model(ganador.calibrador, None) is False


def test_stale_calibrators_nombra_solo_los_desparejados(ganador):
    bundle = {
        "model": ganador.viejo,
        "calibrator": ganador.calibrador,  # bien emparejado
        "method_model": ganador.reentrenado,
        "method_calibrator": ganador.calibrador,  # lleva dentro otro modelo
        "method_model_linear": ganador.viejo,  # sin calibrador: no es desparejo
    }
    assert stale_calibrators(bundle) == ["method_calibrator"]


# --- Al guardar (lo que usa train.py) ----------------------------------------


def test_guardar_un_modelo_nuevo_quita_el_calibrador_que_ya_no_le_corresponde(
    tmp_path, ganador
):
    ruta = tmp_path / "model.joblib"
    joblib.dump(
        {
            "model": ganador.viejo,
            "calibrator": ganador.calibrador,
            "calibration_method": "sigmoid",
            "method_model": "no me toques",
        },
        ruta,
    )

    devuelto = save_bundle_preserving(
        ruta, {"model": ganador.reentrenado, "trained_at": "2026-09-30"}
    )

    for bundle in (devuelto, joblib.load(ruta)):
        assert "calibrator" not in bundle
        assert "calibration_method" not in bundle
        assert bundle["method_model"] == "no me toques"
        assert bundle["trained_at"] == "2026-09-30"


def test_guardar_el_mismo_modelo_conserva_su_calibrador(tmp_path, ganador):
    """Si lo que se guarda es el MISMO modelo (aunque sea otro objeto), su
    calibrador sigue valiendo y se queda."""
    ruta = tmp_path / "model.joblib"
    joblib.dump(
        {
            "model": ganador.viejo,
            "calibrator": ganador.calibrador,
            "calibration_method": "sigmoid",
        },
        ruta,
    )

    save_bundle_preserving(ruta, {"model": copy.deepcopy(ganador.viejo)})

    guardado = joblib.load(ruta)
    assert guardado["calibration_method"] == "sigmoid"
    assert calibrator_wraps_model(guardado["calibrator"], guardado["model"]) is True


def test_guardar_modelo_y_calibrador_juntos_respeta_la_pareja(tmp_path, ganador):
    """Quien escribe las dos piezas a la vez decide el: no se le quita nada."""
    ruta = tmp_path / "model.joblib"
    joblib.dump({"model": ganador.viejo, "calibrator": ganador.calibrador}, ruta)
    calibrador_nuevo = fit_prefit_calibrator(
        ganador.reentrenado, "sigmoid", ganador.x, ganador.y
    )

    save_bundle_preserving(
        ruta, {"model": ganador.reentrenado, "calibrator": calibrador_nuevo}
    )

    guardado = joblib.load(ruta)
    assert calibrator_wraps_model(guardado["calibrator"], guardado["model"]) is True


# --- El model.joblib commiteado ----------------------------------------------


def _problema(bundle: dict, pareja) -> str | None:
    """Lo que impide dar por bueno un modelo del bundle, o None si esta bien:
    tiene que estar, tiene que llevar su calibrador, y ese calibrador tiene que
    envolver ESE modelo y no otro."""
    modelo = bundle.get(pareja.model_key)
    calibrador = bundle.get(pareja.calibrator_key)
    if modelo is None:
        return f"el bundle no trae {pareja.model_key!r}"
    if calibrador is None:
        return (
            f"{pareja.model_key!r} se serviria SIN calibrar: falta "
            f"{pareja.calibrator_key!r}. Arreglo: {pareja.recalibrate_command}"
        )
    if not calibrator_wraps_model(calibrador, modelo):
        return (
            f"{pareja.calibrator_key!r} lleva dentro OTRO modelo, y produccion "
            f"serviria ESE en vez de {pareja.model_key!r}. "
            f"Arreglo: {pareja.recalibrate_command}"
        )
    return None


@pytest.fixture(scope="module")
def bundle_commiteado() -> dict:
    return joblib.load(MODELO_COMMITEADO)


@pytest.mark.parametrize(
    "pareja", CALIBRATED_MODELS, ids=lambda pareja: pareja.calibrator_key
)
def test_el_bundle_commiteado_calibra_exactamente_el_modelo_que_sirve(
    bundle_commiteado, pareja
):
    problema = _problema(bundle_commiteado, pareja)
    assert problema is None, problema


def test_la_guarda_del_commiteado_se_pone_roja_con_un_reentreno_sin_recalibrar(
    tmp_path,
):
    """Una guarda que nunca se ha visto en rojo no guarda nada. Se simula un
    reentreno sobre una COPIA del bundle commiteado, guardado como lo hacia
    train.py hasta hoy y como lo hace ahora: las dos cosas tienen que caer."""
    bundle = joblib.load(MODELO_COMMITEADO)
    reentrenado = _xgb(*_datos(9, n_columnas=len(bundle["feature_columns"])))

    # Hasta hoy: el modelo nuevo y, al lado, el calibrador de antes.
    como_antes = {**bundle, "model": reentrenado}
    assert "OTRO modelo" in _problema(como_antes, GANADOR)

    # Ahora: save_bundle_preserving quita el calibrador viejo.
    ruta = tmp_path / "model.joblib"
    shutil.copyfile(MODELO_COMMITEADO, ruta)
    como_ahora = save_bundle_preserving(ruta, {"model": reentrenado})
    assert "SIN calibrar" in _problema(como_ahora, GANADOR)


# --- train.py de verdad --------------------------------------------------------


def _dataset_sintetico() -> pd.DataFrame:
    # One fight every 2 days up to the frozen metro's TEST_END (split.py): the
    # three partitions get rows and the test clears MIN_TEST_ROWS.
    fechas = pd.date_range(end=TEST_END, periods=2_200, freq="2D")
    n_filas = len(fechas)
    rng = np.random.default_rng(3)
    datos = pd.DataFrame(
        rng.normal(size=(n_filas, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS
    )
    ruido = rng.normal(scale=1.0, size=n_filas)
    datos["target"] = (datos["height_cm_diff"] + ruido > 0).astype(int)
    datos["event_date"] = fechas
    datos["fight_id"] = np.arange(n_filas)
    return datos


def test_train_quita_el_calibrador_viejo_y_lo_avisa_lo_ultimo(
    tmp_path, monkeypatch, capsys
):
    """train.main() sobre una COPIA del bundle commiteado, con datos sinteticos
    (nada de reentrenar el modelo de verdad)."""
    ruta = tmp_path / "model.joblib"
    shutil.copyfile(MODELO_COMMITEADO, ruta)
    monkeypatch.setattr(train, "MODEL_PATH", ruta)
    monkeypatch.setattr(train, "METRICS_PATH", tmp_path / "model_metrics.md")
    monkeypatch.setattr(train, "load_dataset", _dataset_sintetico)
    # La rejilla entera son 108 combinaciones x 3 pliegues: aqui no aporta.
    monkeypatch.setattr(
        train,
        "cross_validate_params",
        lambda *_args, **_kwargs: {"n_estimators": 20, "max_depth": 2},
    )

    train.main()

    guardado = joblib.load(ruta)
    assert "calibrator" not in guardado
    assert "calibration_method" not in guardado
    # El modelo de metodo no es asunto de train.py: sigue ahi y emparejado.
    for pareja in CALIBRATED_MODELS:
        if pareja is not GANADOR:
            assert _problema(guardado, pareja) is None
    # Y la guarda del bundle commiteado queda en ROJO hasta que se recalibre.
    assert "SIN calibrar" in _problema(guardado, GANADOR)

    salida = capsys.readouterr().out
    assert RECALIBRAR in salida
    # Lo ultimo que se ve: no enterrado entre las metricas que imprime despues.
    assert salida.rindex(RECALIBRAR) > salida.rindex("Feature importance:")


# --- api.py al servir ------------------------------------------------------------


def _historial(semilla: float) -> FighterHistorySummary:
    return FighterHistorySummary(
        total_prior_fights=int(5 + semilla),
        total_rounds_fought=int(12 + 2 * semilla),
        sig_strikes_landed_per_fight=40.0 + 3 * semilla,
        sig_strike_accuracy=0.40 + semilla / 50,
        knockdowns_per_fight=0.2 + semilla / 10,
        takedowns_landed_per_fight=1.0 + semilla / 5,
        takedown_accuracy=0.35 + semilla / 40,
        submission_attempts_per_fight=0.5 + semilla / 10,
        control_time_seconds_per_fight=100.0 + 7 * semilla,
        win_streak=int(semilla) % 4,
        wins_last_5=min(5, int(semilla)),
        pct_wins_by_ko=0.4 + semilla / 100,
        pct_wins_by_submission=0.3 - semilla / 200,
        pct_wins_by_decision=0.3,
        days_since_last_fight=int(150 + 10 * semilla),
        ranking_position=None,
        sig_strikes_absorbed_per_fight=30.0 + semilla,
        sig_strike_defense=0.55,
        takedowns_absorbed_per_fight=1.2,
        takedown_defense=0.6,
        avg_opponent_prior_win_rate=0.5 + semilla / 100,
        latest_prior_fight_date=date(2025, 1, 1),
    )


# (rojo, azul) -> semillas de sus historiales; None = sin historial (debutante)
CRUCES = {(1, 2): (4.0, 1.0), (3, 4): (0.5, 6.0), (5, 6): (3.0, None)}


def _filas(rojo: int, azul: int):
    semilla_roja, semilla_azul = CRUCES[(rojo, azul)]
    historial_rojo = _historial(semilla_roja)
    historial_azul = None if semilla_azul is None else _historial(semilla_azul)
    debutante = historial_azul is None
    fila = build_feature_row(
        historial_rojo,
        historial_azul,
        red_height_cm=180.0 + semilla_roja,
        blue_height_cm=None if debutante else 178.0 + semilla_azul,
        red_reach_cm=185.0,
        blue_reach_cm=None if debutante else 183.0,
        red_age=30.0,
        blue_age=None if debutante else 27.5,
    )
    fila_metodo = build_method_feature_row(
        fila,
        historial_rojo,
        historial_azul,
        scheduled_rounds=3,
        weight_class="Lightweight",
    )
    return fila, fila_metodo, debutante


@pytest.fixture
def sin_base_de_datos(monkeypatch):
    """api.predict() sin Neon: los sitios que irian a la base, sustituidos."""

    def construir(_fights, _rankings, rojo, azul, _fisico, history_df=None, fight_id=None):
        fila, fila_metodo, debutante = _filas(rojo, azul)
        return fila, fila_metodo, {"lowConfidence": debutante}, debutante

    def perfiles(_database_url, ids):
        return {
            fighter_id: api.FighterPredictionProfile(
                id=fighter_id,
                name=f"Luchador {fighter_id}",
                nickname=None,
                headshot_url=None,
                wins=10,
                losses=2,
                draws=0,
                height_cm=180.0,
                reach_cm=183.0,
                stance="Orthodox",
                latest_weight_class="Lightweight",
                aggregate_stats={},
            )
            for fighter_id in ids
        }

    monkeypatch.setattr(
        api, "get_settings", lambda: SimpleNamespace(database_url="postgresql://no")
    )
    monkeypatch.setattr(api, "_load_fighter_physical", lambda _url, _ids: {})
    monkeypatch.setattr(api, "_build_feature_row", construir)
    monkeypatch.setattr(api, "_load_fighter_profiles", perfiles)


def _predecir(rojo: int, azul: int, bundle: dict) -> dict:
    return api.predict(
        rojo,
        azul,
        bundle=bundle,
        fights_df=pd.DataFrame(),
        rankings_df=pd.DataFrame(),
        history_df=None,
    )


@pytest.fixture(scope="module")
def ganador_servible() -> SimpleNamespace:
    """Modelo de ganador pequeno con las columnas de verdad, para api.predict()."""
    columnas = list(FEATURE_COLUMNS)

    def datos(semilla):
        x, y = _datos(semilla, n_columnas=len(columnas), n_filas=300)
        return pd.DataFrame(x, columns=columnas), y

    frame_viejo, y_viejo = datos(5)
    imputer = SimpleImputer(strategy="median").fit(frame_viejo)
    x_viejo = imputer.transform(frame_viejo)
    viejo = _xgb(x_viejo, y_viejo)
    frame_nuevo, y_nuevo = datos(6)
    return SimpleNamespace(
        columnas=columnas,
        imputer=imputer,
        x=x_viejo,
        y=y_viejo,
        viejo=viejo,
        calibrador=fit_prefit_calibrator(viejo, "sigmoid", x_viejo, y_viejo),
        nuevo=_xgb(imputer.transform(frame_nuevo), y_nuevo),
    )


def _avisos(caplog, nivel: int = logging.WARNING) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= nivel]


def test_la_api_descarta_al_cargar_el_calibrador_desparejado(
    tmp_path, monkeypatch, caplog, ganador_servible, sin_base_de_datos
):
    sin_calibrador = {
        "model": ganador_servible.nuevo,
        "imputer": ganador_servible.imputer,
        "feature_columns": ganador_servible.columnas,
    }
    con_la_bomba = {**sin_calibrador, "calibrator": ganador_servible.calibrador}
    ruta = tmp_path / "model.joblib"
    joblib.dump(con_la_bomba, ruta)
    monkeypatch.setattr(api, "MODEL_PATH", ruta)

    with caplog.at_level(logging.WARNING):
        bundle = api._load_model_bundle()

    assert "calibrator" not in bundle
    assert bundle[DISCARDED_CALIBRATORS_KEY] == ["calibrator"]
    # Aviso fuerte (ERROR) en el log, diciendo cual y con el arreglo.
    assert any(
        "'calibrator'" in error and RECALIBRAR in error
        for error in _avisos(caplog, logging.ERROR)
    )

    # Se sirve el modelo NUEVO sin calibrar, no el viejo que iba dentro.
    servido = _predecir(1, 2, bundle)["redProbability"]
    assert servido == _predecir(1, 2, sin_calibrador)["redProbability"]
    assert servido != _predecir(1, 2, con_la_bomba)["redProbability"]


@pytest.fixture(scope="module")
def metodo_servible() -> SimpleNamespace:
    """Las dos mitades del modelo de metodo, pequenas, con sus columnas reales."""
    columnas = list(METHOD_FEATURE_COLUMNS)

    def datos(semilla):
        rng = np.random.default_rng(semilla)
        frame = pd.DataFrame(rng.normal(size=(300, len(columnas))), columns=columnas)
        return frame, rng.integers(0, len(METHOD_CLASSES), size=300)

    frame, y = datos(11)
    imputer = SimpleImputer(strategy="median").fit(frame)
    x = imputer.transform(frame)
    arboles, lineal = _xgb_metodo(x, y), _lineal(x, y)
    frame_nuevo, y_nuevo = datos(12)
    return SimpleNamespace(
        columnas=columnas,
        imputer=imputer,
        arboles=arboles,
        lineal=lineal,
        calibrador_arboles=select_calibration(arboles, x, y)[0],
        calibrador_lineal=select_calibration(lineal, x, y)[0],
        arboles_nuevos=_xgb_metodo(imputer.transform(frame_nuevo), y_nuevo),
    )


def test_la_api_descarta_tambien_un_calibrador_desparejado_del_metodo(
    tmp_path, monkeypatch, caplog, metodo_servible
):
    con_la_bomba = {
        "method_model": metodo_servible.arboles_nuevos,
        "method_calibrator": metodo_servible.calibrador_arboles,  # de otro modelo
        "method_model_linear": metodo_servible.lineal,
        "method_calibrator_linear": metodo_servible.calibrador_lineal,  # este si
        "method_imputer": metodo_servible.imputer,
        "method_feature_columns": metodo_servible.columnas,
        "method_classes": list(METHOD_CLASSES),
        "method_ensemble_weights": [0.5, 0.5],
    }
    ruta = tmp_path / "model.joblib"
    joblib.dump(con_la_bomba, ruta)
    monkeypatch.setattr(api, "MODEL_PATH", ruta)

    with caplog.at_level(logging.WARNING):
        bundle = api._load_model_bundle()

    assert bundle[DISCARDED_CALIBRATORS_KEY] == ["method_calibrator"]
    assert "method_calibrator" not in bundle
    assert bundle.get("method_calibrator_linear") is not None  # el emparejado queda
    assert any(
        "'method_calibrator'" in error for error in _avisos(caplog, logging.ERROR)
    )

    _, fila_metodo, _ = _filas(1, 2)
    sin_el_desparejado = {
        key: value for key, value in con_la_bomba.items() if key != "method_calibrator"
    }
    servido = api._predict_method(bundle, fila_metodo)
    assert servido == api._predict_method(sin_el_desparejado, fila_metodo)
    assert servido != api._predict_method(con_la_bomba, fila_metodo)


def test_con_el_bundle_de_hoy_las_predicciones_salen_exactamente_iguales(
    monkeypatch, caplog, sin_base_de_datos
):
    """La guarda no puede cambiar ni un bit de lo que se sirve hoy. api.predict()
    con el bundle tal cual sale de joblib.load (lo que devolvia _load_model_bundle
    hasta hoy) frente al que devuelve ahora, cruce a cruce y con igualdad EXACTA."""
    monkeypatch.setattr(api, "MODEL_PATH", MODELO_COMMITEADO)
    tal_cual = joblib.load(MODELO_COMMITEADO)

    with caplog.at_level(logging.WARNING):
        vigilado = api._load_model_bundle()

    assert vigilado[DISCARDED_CALIBRATORS_KEY] == []
    assert _avisos(caplog) == []
    for pareja in CALIBRATED_MODELS:
        assert vigilado.get(pareja.calibrator_key) is not None
    for rojo, azul in CRUCES:
        ahora = _predecir(rojo, azul, vigilado)
        assert ahora["methodPrediction"] is not None  # tambien el camino de metodo
        assert ahora == _predecir(rojo, azul, tal_cual)


def test_la_comprobacion_se_paga_al_cargar_y_no_en_cada_prediccion(
    tmp_path, monkeypatch, ganador_servible, sin_base_de_datos
):
    """Comparar contenido cuesta un hash por modelo: se paga UNA vez, al cargar
    el bundle, y /predict no lo repite."""
    # Calibrado sobre una COPIA: mismo contenido y otro objeto, asi que la
    # comprobacion no puede ir por el atajo de la identidad y tiene que hashear.
    calibrador = fit_prefit_calibrator(
        copy.deepcopy(ganador_servible.viejo),
        "sigmoid",
        ganador_servible.x,
        ganador_servible.y,
    )
    ruta = tmp_path / "model.joblib"
    joblib.dump(
        {
            "model": ganador_servible.viejo,
            "calibrator": calibrador,
            "imputer": ganador_servible.imputer,
            "feature_columns": ganador_servible.columnas,
        },
        ruta,
    )
    monkeypatch.setattr(api, "MODEL_PATH", ruta)
    hashes: list[int] = []
    hash_de_verdad = joblib.hash
    monkeypatch.setattr(
        joblib, "hash", lambda *a, **k: hashes.append(1) or hash_de_verdad(*a, **k)
    )

    bundle = api._load_model_bundle()
    assert bundle.get("calibrator") is not None  # mismo modelo: se usa
    assert hashes, "al cargar, la comprobacion tenia que mirar el contenido"

    hashes.clear()
    for rojo, azul in CRUCES:
        _predecir(rojo, azul, bundle)
    assert hashes == []
