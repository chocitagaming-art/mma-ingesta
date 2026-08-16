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

Segunda vuelta (UFC 330, evento 1064): el discriminador era la IGUALDAD EXACTA
con el ultimo reloj de 'in', y ESPN se corrige a si mismo al pasar a 'post'. La
15318 congelo 3:18 y publico 3:24: seis segundos de deriva bastaron para darlo
por oficial y sellar 3:24 cuando fue 1:36. Ahora se comparan las dos hipotesis
entre si (la mas cercana al ultimo 'in' gana) en vez de exigir igualdad.
"""

import pytest

from src.scrapers.espn_live_results import elapsed_end_time


# --------------------------------------------------- finalizaciones (cuenta atras)


def test_frozen_countdown_is_converted_to_elapsed():
    # Estelar real: ESPN congelo 2:17 restantes; lo oficial fue 2:41. El reloj
    # de 'post' es EXACTAMENTE el ultimo de 'in': esa igualdad es la firma de
    # que esta congelado.
    assert elapsed_end_time("2:17", "KO/TKO", previous_clock="2:17") == "2:43"


def test_frozen_conversion_matches_the_other_real_cases():
    # Los mismos pares del 1062. La conversion cae a 1-2 s de lo oficial: el
    # reloj se congela en el instante en que ESPN registra el final, no en el
    # que lo canta el arbitro. ufcstats la sustituye al consolidar.
    assert elapsed_end_time("0:37", "KO/TKO", previous_clock="0:37") == "4:23"
    assert elapsed_end_time("3:46", "KO/TKO", previous_clock="3:46") == "1:14"
    assert elapsed_end_time("3:37", "Submission", previous_clock="3:37") == "1:23"


def test_espn_correcting_its_own_frozen_clock_is_still_a_countdown():
    # LA 15318 DEL UFC 330 (16-ago-2026), el fallo que rompio la igualdad
    # exacta. Serie real de las muestras:
    #   in   R3 3:18 STATUS_END_OF_ROUND 00:28:52Z  <- el reloj se para aqui
    #   in   R3 3:18 STATUS_END_OF_ROUND 00:30:23Z  <- previous_clock
    #   post R3 3:24 STATUS_FINAL        00:30:54Z  <- se sello ESTE
    #   post R3 1:36 STATUS_FINAL        02:31:19Z  <- el oficial, 2 h despues
    # Los 6 s de deriva son ESPN corrigiendo su propio congelado, no un tiempo
    # oficial: con la igualdad exacta se guardo 3:24 en vez de 1:36 (108 s).
    assert elapsed_end_time("3:24", "Submission", previous_clock="3:18") == "1:36"


def test_one_second_of_drift_is_still_a_frozen_countdown():
    # No fue un accidente de la 15318: la misma deriva, de un solo segundo,
    # ya habia invertido estas tres en eventos anteriores. El valor esperado es
    # el que ufcstats escribio despues, que es la verdad.
    assert elapsed_end_time("4:25", "KO/TKO", previous_clock="4:26") == "0:35"  # 13925
    assert elapsed_end_time("3:10", "Submission", previous_clock="3:09") == "1:50"  # 14231
    assert elapsed_end_time("2:13", "KO/TKO", previous_clock="2:12") == "2:47"  # 12845


def test_late_official_time_is_not_re_inverted():
    # La SEGUNDA muestra 'post' de la 15318, dos horas despues: ESPN ya sirve el
    # transcurrido bueno (1:36) mientras el ultimo 'in' sigue siendo 3:18. La
    # hipotesis del transcurrido queda a 6 s del ultimo 'in' y la del congelado
    # a 102 s, asi que se respeta. Si la ingesta llega tarde, no lo estropea.
    assert elapsed_end_time("1:36", "Submission", previous_clock="3:18") == "1:36"


def test_without_history_nothing_is_written():
    # ANTES esto devolvia "2:43": sin historial se asumia cuenta atras. Se midio
    # el 16-ago-2026 y la suposicion era falsa. Las 12 finalizaciones del evento
    # 1063 que pasaron por esta rama (un solo sondeo a las 19:43Z encontro el
    # cartel ya terminado, 0 muestras 'in') tienen hoy un end_time que coincide
    # con el reloj CRUDO en las 12 y con el invertido en NINGUNA; los errores de
    # la inversion iban de 16 s a 262 s, o sea hasta cuatro minutos y medio.
    #
    # No se devuelve el crudo aunque acertara en esas 12, porque aqui no se
    # puede distinguir "llegamos tarde" (ESPN ya da el transcurrido) de "estamos
    # en directo" (lo daria congelado), y del segundo caso no hay ni un ejemplo
    # medido. Se prefiere el hueco: ufcstats rellena end_time unas horas
    # despues, y NULL se ve, mientras que un 3:24 falso se lee como verdad.
    assert elapsed_end_time("2:17", "KO/TKO", previous_clock=None) is None


# ------------------------------------------- cuando ESPN SI manda el transcurrido


def test_value_that_differs_from_the_frozen_one_is_taken_as_elapsed():
    # Patron Steveson documentado el 19-jul: 2:26 congelado y luego el oficial
    # 2:31.
    #
    # ESTE CASO ES LA BARRERA CONTRA CUALQUIER TOLERANCIA FIJA. Es tentador
    # arreglar la deriva de la 15318 con un "si difiere en menos de 10 s sigue
    # congelado", pero aqui la diferencia es de 5 s y el valor bueno es el
    # oficial: esa regla devolveria 2:29. Lo que decide no es el tamano de la
    # diferencia sino cual de las dos hipotesis queda mas cerca del ultimo
    # 'in': 300-146 = 2:34 esta a 3 s de 2:31, y el congelado 2:26 a 5 s.
    assert elapsed_end_time("2:31", "KO/TKO", previous_clock="2:26") == "2:31"


def test_small_official_time_is_not_mistaken_for_a_countdown():
    # REGRESION de un fallo real cazado en la prueba de humo. Horas despues del
    # evento ESPN ya sirve el oficial, y la 12865 acabo en 1:12 — un valor
    # pequeno y legitimo. Con la regla vieja ("menor o igual que el anterior")
    # se tomaba por cuenta atras y se escribia 3:48. Con la igualdad, no.
    assert elapsed_end_time("1:12", "KO/TKO", previous_clock="3:46") == "1:12"
    assert elapsed_end_time("2:41", "KO/TKO", previous_clock="2:17") == "2:41"


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


def test_method_unknown_still_converts_the_frozen_countdown():
    # ESPN a veces tarda en dar el metodo; el reloj sigue siendo cuenta atras.
    assert elapsed_end_time("1:00", None, previous_clock="1:00") == "4:00"


def test_zero_remaining_means_the_round_ran_out():
    assert elapsed_end_time("0:00", "KO/TKO", previous_clock="0:00") == "5:00"


def test_clock_longer_than_a_round_is_rejected():
    # 6:12 no es ni transcurrido ni restante de un asalto de 5 minutos.
    assert elapsed_end_time("6:12", "KO/TKO", previous_clock=None) is None


# ------------------------------------------------- limites conocidos del criterio


def test_the_tie_is_only_possible_at_2_30_and_falls_on_the_elapsed_side():
    # El empate solo puede cambiar la respuesta cuando el ultimo 'in' marca
    # 2:30 exacto: ahi 'previous' y '300 - previous' son el mismo numero y no
    # hay forma de distinguir las dos hipotesis con estos tres datos. (Tambien
    # empatan cuando el reloj de 'post' es 2:30, pero entonces las dos dan el
    # mismo valor y da igual cual gane.) El empate se resuelve hacia el
    # transcurrido, igual que hacia el codigo anterior, porque es el lado
    # barato: si nos equivocamos el error queda acotado por la deriva de ESPN
    # (1-6 s medidos), y del otro lado lo estaria por el intervalo de muestreo
    # (~30 s, hasta 60 s de error).
    assert elapsed_end_time("3:00", "KO/TKO", previous_clock="2:30") == "3:00"
    # Con el reloj congelado a 2:30 las dos lecturas coinciden, asi que el
    # empate no puede hacer dano en el unico caso donde de verdad se cruzan.
    assert elapsed_end_time("2:30", "KO/TKO", previous_clock="2:30") == "2:30"


def test_the_ambiguous_band_around_the_midpoint_is_a_known_limitation():
    # FALLO CONOCIDO Y ACOTADO, escrito aqui a proposito para que nadie lo
    # descubra por sorpresa. Cerca de la mitad del asalto (2:30) las dos
    # hipotesis son casi la misma y el criterio puede elegir mal: si la pelea
    # acabo a los 2:40 y el ultimo 'in' se vio a 2:35, se devuelve 2:20.
    # El error maximo en esa franja es |300 - 2E|, unos 60 s con el intervalo
    # real de muestreo, y solo afecta a peleas que acaben entre 2:30 y 3:00.
    # No es arreglable con estos tres datos: haria falta saber si la muestra
    # 'post' venia de un STATUS_FINAL tardio. Se acepta porque el fallo que
    # sustituye invertia el reloj entero (hasta 300 s) en cualquier minuto.
    assert elapsed_end_time("2:40", "KO/TKO", previous_clock="2:35") == "2:20"
