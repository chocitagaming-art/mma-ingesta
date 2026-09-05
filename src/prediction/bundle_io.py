"""Lectura y escritura del bundle del modelo.

POR QUE EXISTE ESTE FICHERO. model.joblib es UN diccionario con dos modelos dentro:
el de ganador (model, imputer, feature_columns, trained_at), su calibrador
(calibrator, calibration_method) y el de metodo (12 claves con prefijo method_).
Quien guarde solo sus claves borra las del otro EN SILENCIO, sin error y sin que
ningun test lo note; se descubriria en directo un sabado. train_method.py ya hacia
lo correcto a mano: esto lo extrae para que lo usen los dos.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import joblib


def save_bundle_preserving(path: Path, new_keys: Mapping[str, Any]) -> dict[str, Any]:
    """Escribe `new_keys` en el bundle de `path` SIN borrar las claves que ya tenia.

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

    # Este mismo fichero tiene el modelo que sirve produccion: escribir en el sitio
    # significa que un fallo a mitad del volcado lo destruye. Se vuelca a un hermano
    # y se renombra, que es atomico dentro del mismo sistema de ficheros.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(bundle, tmp_path)
    tmp_path.replace(path)
    return bundle
