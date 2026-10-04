"""Lectura y escritura del bundle del modelo.

POR QUE EXISTE ESTE FICHERO. model.joblib es UN diccionario con dos modelos dentro:
el de ganador (model, imputer, feature_columns, trained_at), su calibrador
(calibrator, calibration_method) y el de metodo (12 claves con prefijo method_).
Quien guarde solo sus claves borra las del otro EN SILENCIO, sin error y sin que
ningun test lo note; se descubriria en directo un sabado. train_method.py ya hacia
lo correcto a mano: esto lo extrae para que lo usen los dos.

Y LA SEGUNDA BOMBA, LA DEL CALIBRADOR. Un calibrador (CalibratedClassifierCV sobre
FrozenEstimator(model), ver calibrate.py) no es un mapa suelto: lleva DENTRO su
propia copia del modelo con el que se calibro, y api.py sirve `calibrator or
model`. Si se cambia el modelo y se conserva el calibrador, produccion sigue
sirviendo el modelo ANTERIOR, en silencio, con los tests y el despliegue en verde.
calibrator_wraps_model dice si un calibrador envuelve EXACTAMENTE el modelo que
tiene al lado, y con eso save_bundle_preserving no conserva el calibrador de un
modelo que se sustituye, y api.py no sirve con uno que no le corresponde
(discard_stale_calibrators).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, NamedTuple

import joblib
from sklearn.frozen import FrozenEstimator

LOGGER = logging.getLogger(__name__)


class CalibratedModel(NamedTuple):
    calibrator_key: str
    model_key: str
    method_key: str  # donde se guarda si es isotonic o sigmoid
    recalibrate_command: str


# Los tres calibradores del bundle, cada uno con el modelo que tiene que envolver.
# Los del metodo no tienen script propio: los ajusta train_method.py al entrenar.
CALIBRATED_MODELS: tuple[CalibratedModel, ...] = (
    CalibratedModel(
        "calibrator",
        "model",
        "calibration_method",
        "python -m src.prediction.calibrate",
    ),
    CalibratedModel(
        "method_calibrator",
        "method_model",
        "method_calibration_method",
        "python -m src.prediction.train_method",
    ),
    CalibratedModel(
        "method_calibrator_linear",
        "method_model_linear",
        "method_linear_calibration_method",
        "python -m src.prediction.train_method",
    ),
)

# Clave que api.py anade al bundle EN MEMORIA (nunca al fichero) con los
# calibradores que descarto al cargarlo, para que /health lo pueda contar.
DISCARDED_CALIBRATORS_KEY = "discarded_calibrators"

# How the winner model was trained. train.py writes these four keys since phase 4
# (--feature-set, --nan-policy, the XGBoost hyperparameters it really used and the
# final-fit seed). A bundle written before (the 27-jun one) has none of them: it was
# trained on the 20 legacy diffs with the median imputer, and that is what a missing
# key means. Its hyperparameters and seed were never recorded (None).
PRE_PHASE4_TRAINING_CONFIG: dict[str, Any] = {
    "feature_set": "legacy",
    "nan_policy": "median",
    "xgb_params": None,
    "train_seed": None,
}


def winner_training_config(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """feature_set, nan_policy, xgb_params and train_seed of the winner model,
    with the pre-phase-4 meaning for every key the bundle does not carry."""
    return {
        key: bundle.get(key, default)
        for key, default in PRE_PHASE4_TRAINING_CONFIG.items()
    }


def _models_it_predicts_with(calibrator: Any) -> list[Any]:
    """Los modelos a los que el calibrador llama DE VERDAD al predecir.

    predict_proba de CalibratedClassifierCV promedia sus calibrated_classifiers_,
    y cada uno consulta su .estimator. Con FrozenEstimator (calibrate.py y
    train_method.py) ese estimator es el envoltorio y el modelo va en su
    .estimator; con el antiguo cv="prefit" es el modelo tal cual. Lista vacia si
    no es un calibrador ajustado."""
    models = []
    for calibrated in getattr(calibrator, "calibrated_classifiers_", None) or []:
        estimator = getattr(calibrated, "estimator", None)
        while isinstance(estimator, FrozenEstimator):
            estimator = estimator.estimator
        models.append(estimator)
    return models


def calibrator_wraps_model(calibrator: Any, model: Any) -> bool:
    """True solo si TODO lo que el calibrador consulta al predecir es `model`.

    Compara CONTENIDO, no identidad: dos objetos distintos con el mismo modelo
    dentro (un volcado y su carga, o un reentreno identico) cuentan como el mismo,
    y un reentreno de verdad no. La identidad es solo un atajo para no calcular:
    es el caso del bundle que escribe calibrate.py, porque joblib guarda UNA vez
    el objeto compartido y al cargar lo vuelve a compartir."""
    if calibrator is None or model is None:
        return False
    inner_models = _models_it_predicts_with(calibrator)
    if not inner_models:
        return False
    model_hash = None
    for inner in inner_models:
        if inner is model:
            continue
        if inner is None:
            return False
        if model_hash is None:
            model_hash = joblib.hash(model)
        if joblib.hash(inner) != model_hash:
            return False
    return True


def stale_calibrators(bundle: Mapping[str, Any]) -> list[str]:
    """Claves de los calibradores PRESENTES que no envuelven a su modelo."""
    return [
        pair.calibrator_key
        for pair in CALIBRATED_MODELS
        if bundle.get(pair.calibrator_key) is not None
        and not calibrator_wraps_model(
            bundle[pair.calibrator_key], bundle.get(pair.model_key)
        )
    ]


def discard_stale_calibrators(bundle: dict[str, Any]) -> list[str]:
    """Para SERVIR: quita del bundle EN MEMORIA cada calibrador desparejado.

    Lo llama api.py al cargar model.joblib: una vez por carga, nunca por
    prediccion. Sin su calibrador, `calibrator or model` sirve el modelo del bundle
    sin calibrar: peor calibrado, pero ES el modelo que toca y no el viejo que el
    calibrador llevaba dentro. Cada descarte deja un ERROR en el log con el
    arreglo, y la lista queda en bundle[DISCARDED_CALIBRATORS_KEY] para /health.
    El fichero no se toca. Devuelve las claves descartadas."""
    stale = stale_calibrators(bundle)
    for pair in CALIBRATED_MODELS:
        if pair.calibrator_key not in stale:
            continue
        del bundle[pair.calibrator_key]
        LOGGER.error(
            "CALIBRADOR DESPAREJADO: %r lleva dentro un modelo que NO es el %r que "
            "se sirve a su lado, y usarlo seria servir en silencio el modelo con el "
            "que se calibro. Descartado: se sirve %r SIN calibrar. Arreglo: %s",
            pair.calibrator_key,
            pair.model_key,
            pair.model_key,
            pair.recalibrate_command,
        )
    bundle[DISCARDED_CALIBRATORS_KEY] = stale
    return stale


def save_bundle_preserving(path: Path, new_keys: Mapping[str, Any]) -> dict[str, Any]:
    """Escribe `new_keys` en el bundle de `path` SIN borrar las claves que ya tenia.

    Con UNA excepcion: si `new_keys` cambia un modelo y no trae su calibrador, el
    calibrador que habia se quita (con la clave de su metodo) cuando ya no envuelve
    al modelo nuevo. Quien lo necesite sabe que falta mirando el bundle devuelto.

    Devuelve el bundle resultante. Si el fichero no existe, se crea con `new_keys`.
    """
    path = Path(path)
    bundle: dict[str, Any] = {}

    if path.exists():
        loaded = joblib.load(path)
        if not isinstance(loaded, dict):
            raise RuntimeError(
                f"El bundle de {path} esta malformado (se esperaba un diccionario, "
                f"llego {type(loaded).__name__}); no se sobrescribe."
            )
        bundle = loaded

    bundle.update(new_keys)

    # LA BOMBA DEL CALIBRADOR. Conservar el calibrador de un modelo que se acaba de
    # sustituir dejaria produccion sirviendo el modelo viejo que lleva dentro. Se
    # quita en vez de marcarlo: asi CUALQUIER lector (api.py, evaluate.py, el
    # servicio que ya esta desplegado) sirve el modelo nuevo sin tener que saber de
    # marcas, y el calibrador viejo no le sirve a nadie (calibrate.py hace uno
    # nuevo desde cero, y volver atras es recuperar el model.joblib anterior de git).
    for pair in CALIBRATED_MODELS:
        if pair.model_key not in new_keys or pair.calibrator_key in new_keys:
            continue
        calibrator = bundle.get(pair.calibrator_key)
        if calibrator is not None and not calibrator_wraps_model(
            calibrator, bundle[pair.model_key]
        ):
            del bundle[pair.calibrator_key]
            bundle.pop(pair.method_key, None)

    # Este mismo fichero tiene el modelo que sirve produccion: escribir en el sitio
    # significa que un fallo a mitad del volcado lo destruye. Se vuelca a un hermano
    # y se renombra, que es atomico dentro del mismo sistema de ficheros.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(bundle, tmp_path)
    tmp_path.replace(path)
    return bundle
