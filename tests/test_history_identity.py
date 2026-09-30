"""La guarda de identidad del historial ESPN (t4-9-2): `history_identity_ok`.

Hasta hoy la guarda era un `fold_ratio(nuestro, ESPN) >= 0.92` a secas, y eso
fallaba por los dos lados, con casos reales de la base:

  * rechazaba a gente correcta: «Michael Aswell Jr.» contra el «Michael Aswell»
    de ESPN da 0,903; «Xiong Jingnan» contra «Jingnan Xiong» (orden chino);
    «Daniel Spohn» contra «Dan Spohn» y «Jose Souza» contra «Jose Henrique»,
    que ESPN nombra distinto pero con la MISMA fecha de nacimiento;
  * aceptaba a gente distinta: «X Jr.» contra «X Sr.» da 0,941, y padre e hijo
    con el mismo nombre dan 1,0.

Todos los nombres de aquí son los de la base o los que sirve ESPN (sondado el
30-sep-2026: el displayDOB de Spohn es '12/10/1984', el de Jose Henrique
'23/5/2002': día y mes cambian de orden según lang/region).
"""

from __future__ import annotations

from datetime import date

import pytest

from src.scrapers.matching import (
    IDENTITY_THRESHOLD,
    birth_key,
    fold_ratio,
    history_identity_ok,
    split_generational_suffix,
)


class TestSplitGenerationalSuffix:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Michael Aswell Jr.", ("michael aswell", "jr")),
            ("Michael Aswell Jr", ("michael aswell", "jr")),
            ("Marcio Alexandre Junior", ("marcio alexandre", "jr")),
            ("Joe Smith Sr.", ("joe smith", "sr")),
            ("Joe Smith Senior", ("joe smith", "sr")),
            ("Joe Smith II", ("joe smith", "ii")),
            ("Joe Smith III", ("joe smith", "iii")),
            ("Joe Smith IV", ("joe smith", "iv")),
            # Neto y Filho son apellidos en Brasil: NO son sufijo.
            ("Antonio Rogerio Nogueira Neto", ("antonio rogerio nogueira neto", None)),
            ("Ronaldo Souza Filho", ("ronaldo souza filho", None)),
            # Solo la ÚLTIMA palabra, y solo si quedan al menos 2.
            ("Junior dos Santos", ("junior dos santos", None)),
            ("Aswell Jr.", ("aswell jr", None)),
            ("Michael Jr. Aswell", ("michael jr aswell", None)),
        ],
    )
    def test_split(self, name, expected):
        assert split_generational_suffix(name) == expected


class TestBirthKey:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (date(1984, 10, 12), (1984, (10, 12))),
            ("12/10/1984", (1984, (10, 12))),
            ("10/12/1984", (1984, (10, 12))),
            ("23/5/2002", (2002, (5, 23))),
            ("5/23/2002", (2002, (5, 23))),
            ("2002-05-23", (2002, (5, 23))),
            ("2002-05-23T07:00Z", (2002, (5, 23))),
            (None, None),
            ("", None),
            ("unknown", None),
            ("31/31/2002", None),
        ],
    )
    def test_birth_key(self, value, expected):
        assert birth_key(value) == expected


class TestHistoryIdentityOk:
    @pytest.mark.parametrize(
        ("ours", "espn", "our_birth", "espn_birth", "expected"),
        [
            # Hoy: nombres casi iguales pasan.
            ("Yaroslav Amosov", "Yaroslav Amosov", None, None, True),
            ("Jiří Procházka", "Jiri Prochazka", None, None, True),
            # a/c. El sufijo de un solo lado no resta.
            ("Michael Aswell Jr.", "Michael Aswell", None, None, True),
            ("Michael Aswell", "Michael Aswell Jr.", None, None, True),
            ("Michael Aswell Jr.", "Michael Aswell", date(2000, 9, 27), "27/9/2000", True),
            # jr == junior.
            ("Marcio Alexandre Junior", "Marcio Alexandre Jr.", None, None, True),
            # ESPN da 3/5/1989 y nosotros 5-5-1989: mismo año, pasa por la base.
            ("Marcio Alexandre Junior", "Marcio Alexandre Jr.", date(1989, 5, 5), "3/5/1989", True),
            # a. Sufijos distintos en los dos lados: rechazo aunque el ratio pase.
            ("Michael Aswell Jr.", "Michael Aswell Sr.", None, None, False),
            ("Joe Smith Junior", "Joe Smith Senior", None, None, False),
            ("Joe Smith II", "Joe Smith III", None, None, False),
            # c. Padre e hijo con el mismo nombre: los años se llevan más de 1.
            ("Michael Aswell", "Michael Aswell", date(1960, 1, 1), "1/1/1990", False),
            ("Michael Aswell Jr.", "Michael Aswell", date(1990, 3, 4), "4/3/1960", False),
            # ... pero un año de diferencia se tolera (zonas horarias, erratas).
            ("Michael Aswell", "Michael Aswell", date(1990, 1, 1), "31/12/1989", True),
            # Si falta la fecha en un lado, la regla del año no aplica.
            ("Michael Aswell", "Michael Aswell", date(1960, 1, 1), None, True),
            ("Michael Aswell", "Michael Aswell", None, "1/1/1990", True),
            # d. Mismas palabras en otro orden.
            ("Xiong Jingnan", "Jingnan Xiong", None, None, True),
            ("Xiong Jingnan", "Jingnan Xiong", date(1988, 5, 1), "1/5/1988", True),
            # e. Misma fecha exacta + una palabra de 3+ letras en común.
            ("Daniel Spohn", "Dan Spohn", date(1984, 10, 12), "12/10/1984", True),
            ("Daniel Spohn", "Dan Spohn", date(1984, 10, 12), "10/12/1984", True),
            ("Jose Souza", "Jose Henrique", date(2002, 5, 23), "23/5/2002", True),
            # e. Sin la fecha, Spohn sigue fuera (como hoy).
            ("Daniel Spohn", "Dan Spohn", None, None, False),
            ("Daniel Spohn", "Dan Spohn", date(1984, 10, 12), None, False),
            # e. Misma fecha pero ninguna palabra en común: rechazo.
            ("Daniel Spohn", "Marcus Brown", date(1984, 10, 12), "12/10/1984", False),
            # e. La palabra en común tiene que tener 3+ letras (y no ser partícula).
            ("Al Iaquinta", "Al Brown", date(1987, 4, 30), "30/4/1987", False),
            ("Juan del Rio", "Pedro del Toro", date(1990, 1, 2), "2/1/1990", False),
            # e. Palabra en común pero fecha distinta (mismo año): rechazo.
            ("Daniel Spohn", "Dan Spohn", date(1984, 10, 12), "13/10/1984", False),
            # e. Palabra en común pero año distinto: rechazo.
            ("Jose Souza", "Jose Henrique", date(2002, 5, 23), "23/5/2003", False),
            # Neto / Filho no se quitan: dos personas distintas siguen distintas.
            ("Antonio Silva Neto", "Antonio Silva Filho", None, None, False),
            # Nombres sin relación.
            ("Yaroslav Amosov", "Someone Else Entirely", None, None, False),
        ],
    )
    def test_rules(self, ours, espn, our_birth, espn_birth, expected):
        assert (
            history_identity_ok(ours, espn, our_birth=our_birth, espn_birth=espn_birth)
            is expected
        )

    def test_the_cases_really_needed_a_new_rule(self):
        """Si alguno de estos pasara ya con el ratio a secas, los tests de
        arriba no demostrarían nada: la guarda vieja tiene que fallarlos."""
        assert fold_ratio("Michael Aswell Jr.", "Michael Aswell") < IDENTITY_THRESHOLD
        assert fold_ratio("Xiong Jingnan", "Jingnan Xiong") < IDENTITY_THRESHOLD
        assert fold_ratio("Daniel Spohn", "Dan Spohn") < IDENTITY_THRESHOLD
        assert fold_ratio("Jose Souza", "Jose Henrique") < IDENTITY_THRESHOLD
        # Y este lo aceptaba la vieja siendo dos personas.
        assert fold_ratio("Michael Aswell Jr.", "Michael Aswell Sr.") >= IDENTITY_THRESHOLD
