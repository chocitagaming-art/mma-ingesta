"""Regenerar el CSV de entrenamiento NO puede tocar la base de produccion.

main() llamaba incondicionalmente a create_output_table, que hace DROP TABLE +
CREATE + INSERT sobre Neon. La tabla no la lee nadie. Escribir en ella pasa a ser
opt-in explicito."""

import pandas as pd
import pytest

import src.prediction.features.output as output


@pytest.fixture
def _sin_base(monkeypatch, tmp_path):
    """Deja main() funcionando sin tocar la base ni el disco del repo."""
    dataset = pd.DataFrame(
        {"fight_id": [1], "event_date": ["2026-01-01"], "target": [1], "reach_cm_diff": [2.5]}
    )
    resultado = output.DatasetBuildResult(
        dataset=dataset,
        spot_checks=[],
        total_fights_seen=1,
        excluded_no_target=0,
        excluded_missing_history=0,
        excluded_missing_stats=0,
    )
    monkeypatch.setattr(output, "load_base_dataframe", lambda _url: pd.DataFrame())
    monkeypatch.setattr(output, "load_rankings_dataframe", lambda _url: pd.DataFrame())
    monkeypatch.setattr(output, "build_training_dataset", lambda *_a, **_k: resultado)
    monkeypatch.setattr(output, "OUTPUT_CSV_PATH", tmp_path / "training_dataset.csv")

    class _Settings:
        database_url = "postgresql://no-usar"

    monkeypatch.setattr(output, "get_settings", lambda: _Settings())
    return tmp_path


def test_por_defecto_no_toca_la_base(monkeypatch, _sin_base):
    llamadas = []
    monkeypatch.setattr(
        output, "create_output_table", lambda *a, **k: llamadas.append(a)
    )

    output.main()

    assert llamadas == []
    assert (_sin_base / "training_dataset.csv").exists()


def test_con_el_flag_si_escribe(monkeypatch, _sin_base):
    llamadas = []
    monkeypatch.setattr(
        output, "create_output_table", lambda *a, **k: llamadas.append(a)
    )

    output.main(write_table=True)

    assert len(llamadas) == 1
