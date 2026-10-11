"""El 21,17 % de las filas de fight_history_espn son POSTERIORES al debut UFC del
luchador. Usarlas para predecir una pelea anterior es mirar el futuro. Este test es
la unica red que lo impide.

Portado sin tocar de docs/experiments/preufc-dwcs-2026-09-30/test_corte_temporal.py:
solo cambia el import (la funcion vive ahora en src/prediction/features/preufc.py)."""

from datetime import date

import pandas as pd

from src.prediction.features.preufc import espn_features_for_fighter


def _historial():
    return pd.DataFrame(
        {
            "fighter_id": [1, 1, 1],
            "event_date": [date(2018, 1, 1), date(2019, 1, 1), date(2025, 1, 1)],
            "result": ["W", "L", "W"],
            "method": ["KO/TKO", "U-DEC", "SUB - Armbar"],
            "is_title_fight": [False, False, True],
        }
    )


def test_solo_usa_peleas_anteriores_al_corte():
    fila = espn_features_for_fighter(_historial(), fighter_id=1, corte=date(2020, 1, 1))

    assert fila["espn_prev_fights"] == 2
    assert fila["espn_win_rate"] == 0.5


def test_la_pelea_del_futuro_no_entra_ni_en_el_win_rate():
    con_futuro = espn_features_for_fighter(_historial(), fighter_id=1, corte=date(2026, 1, 1))
    sin_futuro = espn_features_for_fighter(_historial(), fighter_id=1, corte=date(2020, 1, 1))

    assert con_futuro["espn_prev_fights"] == 3
    assert sin_futuro["espn_prev_fights"] == 2
    assert con_futuro["espn_win_rate"] != sin_futuro["espn_win_rate"]


def test_una_pelea_el_mismo_dia_del_corte_NO_entra():
    fila = espn_features_for_fighter(_historial(), fighter_id=1, corte=date(2019, 1, 1))

    assert fila["espn_prev_fights"] == 1


def test_sin_historial_devuelve_nulos_no_ceros():
    vacio = pd.DataFrame(columns=["fighter_id", "event_date", "result", "method", "is_title_fight"])

    fila = espn_features_for_fighter(vacio, fighter_id=99, corte=date(2020, 1, 1))

    assert fila["espn_prev_fights"] == 0
    assert fila["espn_win_rate"] is None
    assert fila["espn_has_history"] == 0
