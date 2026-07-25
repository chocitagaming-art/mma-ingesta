"""El reloj de ESPN es una CUENTA ATRAS, y fights.end_time es el TRANSCURRIDO.

Caso real (evento 1062, 25-jul-2026): 8 de los 12 combates quedaron con la hora
de finalizacion invertida en produccion. El estelar decia 2:17 cuando fue 2:41;
cinco pares sumaban 4:58-4:59, la firma inconfundible del reloj al reves.

Por que no se veia antes: el bucle sella is_final en la PRIMERA pasada 'post' y
ya no vuelve a pedir la pelea, asi que congela la cuenta atras. Con el intervalo
de 120 s del UFC 329 daba tiempo a que ESPN publicara el oficial; bajarlo a 20 s
lo provoco.

Por que no basta con invertir siempre: en las DECISIONES ESPN si manda el
transcurrido ('5:00'), y por eso 4 de los 12 estaban bien. Invertir a ciegas los
habria roto.
"""

import pytest

from src.scrapers.espn_live_results import elapsed_end_time


# --------------------------------------------------- finalizaciones (cuenta atras)


def test_countdown_is_converted_to_elapsed():
    # Estelar real: ESPN congelo 2:17 restantes; lo oficial fue 2:41.
    assert elapsed_end_time("2:17", "KO/TKO", previous_clock="2:36") == "2:43"


def test_countdown_conversion_matches_the_other_real_cases():
    # Los mismos pares del 1062. La conversion cae a 1-2 s de lo oficial: el
    # reloj de ESPN se congela en el instante en que EL registra el final, no en
    # el que lo canta el arbitro. Es una aproximacion honesta, y ufcstats la
    # sustituye por la oficial al consolidar.
    assert elapsed_end_time("0:37", "KO/TKO", previous_clock="1:05") == "4:23"
    assert elapsed_end_time("3:46", "KO/TKO", previous_clock="4:07") == "1:14"
    assert elapsed_end_time("3:37", "Submission", previous_clock="4:08") == "1:23"


def test_without_history_assumes_countdown():
    # Es lo que ESPN hace SIEMPRE mientras la pelea esta viva y al sellar: en
    # los 12 combates del 1062 no publico el transcurrido ni una vez.
    assert elapsed_end_time("2:17", "KO/TKO", previous_clock=None) == "2:43"


# ------------------------------------------- cuando ESPN SI manda el transcurrido


def test_value_that_breaks_the_countdown_is_taken_as_elapsed():
    # Patron Steveson documentado el 19-jul: 2:26 congelado y luego el oficial
    # 2:31. Una cuenta atras nunca SUBE dentro del asalto, asi que ese salto
    # delata que el valor ya es el transcurrido. Invertirlo lo corromperia.
    assert elapsed_end_time("2:31", "KO/TKO", previous_clock="2:26") == "2:31"


# ------------------------------------------------------------------- decisiones


def test_decision_is_the_full_round():
    # ESPN ya manda 5:00, y es correcto: una decision agota el asalto.
    assert elapsed_end_time("5:00", "Decision", previous_clock="0:13") == "5:00"


def test_decision_with_dash_is_repaired():
    # Las peleas 12871 y 12872 del 1062 guardaron un GUION literal como hora.
    # Una decision siempre agota el asalto, asi que el dato se conoce.
    assert elapsed_end_time("-", "Decision", previous_clock="0:11") == "5:00"
    assert elapsed_end_time(None, "Decision", previous_clock=None) == "5:00"


# ----------------------------------------------------------------- basura y bordes


@pytest.mark.parametrize("clock", ["-", "", None, "Final", "12", ":30"])
def test_unusable_clock_without_a_decision_writes_nothing(clock):
    # NULL es peor que un dato bueno, pero MUCHISIMO mejor que uno inventado:
    # end_time alimenta los segundos de combate (SLpM, duraciones, radar).
    assert elapsed_end_time(clock, "KO/TKO", previous_clock=None) is None


def test_method_unknown_still_converts_the_countdown():
    # ESPN a veces tarda en dar el metodo; el reloj sigue siendo cuenta atras.
    assert elapsed_end_time("1:00", None, previous_clock="1:30") == "4:00"


def test_zero_remaining_means_the_round_ran_out():
    assert elapsed_end_time("0:00", "KO/TKO", previous_clock="0:12") == "5:00"


def test_clock_longer_than_a_round_is_rejected():
    # 6:12 no es ni transcurrido ni restante de un asalto de 5 minutos.
    assert elapsed_end_time("6:12", "KO/TKO", previous_clock=None) is None
