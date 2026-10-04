"""El bundle del modelo es UN diccionario con dos modelos dentro: el de ganador y
el de metodo (12 claves con prefijo method_). Guardar solo las claves propias borra
las ajenas en silencio y tumba methodPrediction en produccion. Estos tests impiden
que vuelva a pasar.

Lo unico que NO se conserva por las buenas es el calibrador de un modelo que se
sustituye: lleva dentro el modelo viejo (test_calibrador_desparejado.py)."""

import joblib
import pytest

from src.prediction.bundle_io import save_bundle_preserving


def test_conserva_las_claves_ajenas(tmp_path):
    ruta = tmp_path / "model.joblib"
    joblib.dump(
        {
            "model": "viejo",
            "method_model": "no me toques",
            "method_classes": ["KO", "SUB", "DEC"],
            "method_calibrator": "tampoco",
        },
        ruta,
    )

    save_bundle_preserving(ruta, {"model": "nuevo", "trained_at": "2026-09-06"})

    resultado = joblib.load(ruta)
    assert resultado["model"] == "nuevo"
    assert resultado["trained_at"] == "2026-09-06"
    assert resultado["method_model"] == "no me toques"
    assert resultado["method_classes"] == ["KO", "SUB", "DEC"]
    assert resultado["method_calibrator"] == "tampoco"


def test_conserva_el_calibrador_si_no_se_toca_su_modelo(tmp_path):
    ruta = tmp_path / "model.joblib"
    joblib.dump(
        {"model": "viejo", "calibrator": "tampoco", "calibration_method": "sigmoid"},
        ruta,
    )

    save_bundle_preserving(ruta, {"trained_at": "2026-09-06"})

    resultado = joblib.load(ruta)
    assert resultado["calibrator"] == "tampoco"
    assert resultado["calibration_method"] == "sigmoid"


def test_funciona_si_no_hay_bundle_previo(tmp_path):
    ruta = tmp_path / "nuevo.joblib"

    save_bundle_preserving(ruta, {"model": "primero"})

    assert joblib.load(ruta) == {"model": "primero"}


def test_no_deja_fichero_temporal(tmp_path):
    ruta = tmp_path / "model.joblib"
    joblib.dump({"model": "viejo"}, ruta)

    save_bundle_preserving(ruta, {"model": "nuevo"})

    assert list(tmp_path.iterdir()) == [ruta]


def test_rechaza_un_bundle_previo_corrupto(tmp_path):
    ruta = tmp_path / "model.joblib"
    joblib.dump(["esto no es un diccionario"], ruta)

    with pytest.raises(RuntimeError, match="malformado"):
        save_bundle_preserving(ruta, {"model": "nuevo"})
