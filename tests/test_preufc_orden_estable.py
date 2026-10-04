"""The 6-sep result moved with the order of same-day rows (the same CSV gave from
+0,0146 to +0,0202). Two fights the same day are ordered by row id. Pure: no database.

The two streak tests of docs/experiments/preufc-dwcs-2026-09-30/test_orden_estable.py,
ported unchanged: only the import differs (the other five tests of that file belong to
the experiment's bench and split, not to the ported function)."""

from datetime import date

import pandas as pd

from src.prediction.features.preufc import espn_features_for_fighter


# --- 1. The ESPN history: two fights the same day are ordered by row id -----------

def test_la_racha_con_dos_peleas_el_mismo_dia_sigue_el_orden_del_id():
    # Llegan al reves (id 11 antes que id 10). Por id, la ultima es la victoria.
    historial = pd.DataFrame(
        {
            "id": [11, 10],
            "fighter_id": [1, 1],
            "event_date": [date(2019, 5, 1), date(2019, 5, 1)],
            "result": ["win", "loss"],
            "method": ["KO/TKO", "U-DEC"],
            "is_title_fight": [False, False],
        }
    )

    fila = espn_features_for_fighter(historial, fighter_id=1, corte=date(2020, 1, 1))

    assert fila["espn_streak"] == 1


def test_la_racha_no_depende_del_orden_de_entrada():
    base = pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "fighter_id": [1, 1, 1, 1],
            "event_date": [date(2018, 1, 1), date(2019, 5, 1), date(2019, 5, 1), date(2019, 5, 1)],
            "result": ["loss", "win", "loss", "win"],
            "method": ["U-DEC", "SUB", "U-DEC", "KO/TKO"],
            "is_title_fight": [False, False, False, False],
        }
    )
    resultados = {
        espn_features_for_fighter(base.sample(frac=1, random_state=s), 1, date(2020, 1, 1))["espn_streak"]
        for s in range(20)
    }

    assert resultados == {1}
