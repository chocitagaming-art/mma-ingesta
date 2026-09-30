"""Tests del import de historial ESPN (S3-G): parser, normalización de
métodos, etiquetas de promoción y el loop de backfill contra el fake DB.

La forma de los payloads reproduce el endpoint real common/v3 (sondado el
11-jul-2026 con Amosov 4275020 y Jim Miller 2335718): events = lista de uids
`s:3301~l:<liga>~e:<evento>~c:<competición>` y eventsMap con gameDate,
gameResult (W/L/D), opponent, status.{period,displayClock,result} y
titleFight. La liga 3321 es UFC (saltada, salvo el Contender Series que ESPN
también cuelga de ella: ver TestContenderSeries), 3323 Bellator y el resto
regionales (3335, 3339, 3359...).
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from src.scrapers import espn_fight_history
from src.scrapers.espn_fight_history import (
    backfill,
    canonical_method,
    parse_career,
    promotion_label,
)


def entry(
    uid: str,
    *,
    name: str = "Bellator 301: Amosov vs. Jackson",
    short_name: str = "Bellator 301",
    game_result: str | None = "W",
    result_token: str = "decision---unanimous",
    result_display: str = "Unanimous Decision",
    period: int | None = 3,
    clock: str | None = "5:00",
    opponent_id: str | None = "5088766",
    opponent_name: str | None = "Jason Jackson",
    title: bool = False,
    # Timestamp UTC real de una velada del viernes noche US (17-nov-2023 en
    # hora del Este): el parser debe devolver la fecha del EVENTO, no la UTC.
    game_date: str | None = "2023-11-18T03:00:00.000+00:00",
) -> dict:
    status: dict = {}
    if period is not None:
        status["period"] = period
    if clock is not None:
        status["displayClock"] = clock
    if result_token:
        status["result"] = {"name": result_token, "displayName": result_display}
    payload = {
        "id": uid.split("e:")[1].split("~")[0] if "e:" in uid else "0",
        "uid": uid,
        "name": name,
        "shortName": short_name,
        "status": status,
        "titleFight": title,
    }
    if game_result is not None:
        payload["gameResult"] = game_result
    if game_date is not None:
        payload["gameDate"] = game_date
    if opponent_id or opponent_name:
        payload["opponent"] = {"id": opponent_id, "displayName": opponent_name}
    return payload


def career(*entries: dict) -> dict:
    return {
        "athlete": {"displayName": "Yaroslav Amosov"},
        "events": [item["uid"] for item in entries],
        "eventsMap": {item["uid"]: item for item in entries},
    }


BELLATOR_UID = "s:3301~l:3323~e:600000001~c:400000001"
UFC_UID = "s:3301~l:3321~e:600000002~c:400000002"
REGIONAL_UID = "s:3301~l:3359~e:600000003~c:400000003"


class TestParseCareer:
    def test_skips_ufc_league_and_keeps_the_rest(self):
        payload = career(
            entry(UFC_UID, name="UFC 328: Chimaev vs. Strickland", short_name="UFC 328"),
            entry(BELLATOR_UID),
            entry(REGIONAL_UID, name="CFFC 140: Rzgoev vs. Roberts", short_name="CFFC 140"),
        )
        bouts, counts = parse_career(payload)
        assert counts["skipped_ufc"] == 1
        assert [bout.promotion for bout in bouts] == ["Bellator", "CFFC"]
        assert all(bout.league_id != "3321" for bout in bouts)

    def test_defensive_skip_when_ufc_named_event_comes_under_other_league(self):
        payload = career(
            entry(REGIONAL_UID, name="UFC Fight Night: Prelims", short_name="UFC Fight Night"),
            entry(
                "s:3301~l:3359~e:600000011~c:400000011",
                name="TUF 33 Finale: Team A vs Team B",
                short_name="TUF 33",
            ),
        )
        bouts, counts = parse_career(payload)
        assert bouts == []
        assert counts["skipped_ufc_named"] == 2

    def test_word_boundary_spares_real_promotions_tuff_n_uff_and_ufcf(self):
        # Hallazgo de la revisión adversarial: sin límite de palabra, el
        # prefijo "TUF" descartaba Tuff-N-Uff (Kai Kamaka, Naimov...) y el
        # prefijo "UFC" descartaba la noventera UFCF (Josh Barnett).
        payload = career(
            entry(
                REGIONAL_UID,
                name="Tuff-N-Uff 143: Future Stars",
                short_name="Tuff-N-Uff 143",
            ),
            entry(
                "s:3301~l:3359~e:600000012~c:400000012",
                name="UFCF: Clash of the Titans",
                short_name="UFCF",
            ),
        )
        bouts, counts = parse_career(payload)
        assert counts["skipped_ufc_named"] == 0
        assert [bout.promotion for bout in bouts] == ["Tuff-N-Uff", "UFCF"]

    def test_row_fields_are_normalized(self):
        payload = career(
            entry(
                BELLATOR_UID,
                result_token="submission-anaconda-choke",
                result_display="Submission (Anaconda Choke)",
                period=1,
                clock="4:18",
                title=True,
            )
        )
        (bout,), _ = parse_career(payload)
        assert bout.espn_competition_id == "400000001"
        assert bout.espn_event_id == "600000001"
        assert bout.event_date == date(2023, 11, 17)
        assert bout.result == "win"
        assert bout.method == "SUB - Anaconda Choke"
        assert bout.end_round == 1
        assert bout.end_time == "4:18"
        assert bout.is_title_fight is True
        assert bout.opponent_name == "Jason Jackson"
        assert bout.opponent_espn_id == "5088766"

    def test_result_mapping_and_no_contest(self):
        payload = career(
            entry(BELLATOR_UID, game_result="L"),
            entry(REGIONAL_UID, game_result="D", name="RF 14: Fall Brawl", short_name="RF 14"),
            entry(
                "s:3301~l:3359~e:600000004~c:400000004",
                game_result="D",
                result_token="no-contest",
                result_display="No Contest",
                name="CFFC 5: Two Worlds, One Cage",
                short_name="CFFC 5",
            ),
        )
        bouts, _ = parse_career(payload)
        assert [bout.result for bout in bouts] == ["loss", "draw", "nc"]
        assert bouts[2].method == "CNC"

    def test_scheduled_fight_without_result_is_skipped(self):
        payload = career(
            entry(BELLATOR_UID, game_result=None, result_token="", result_display=""),
        )
        bouts, counts = parse_career(payload)
        assert bouts == []
        assert counts["skipped_no_result"] == 1

    def test_malformed_uid_and_missing_map_entry_are_counted(self):
        good = entry(BELLATOR_UID)
        payload = {
            "athlete": {"displayName": "X"},
            "events": ["s:3301~mal-formado", "s:3301~l:3323~e:9~c:9", good["uid"]],
            "eventsMap": {good["uid"]: good, "s:3301~mal-formado": {"name": "?"}},
        }
        bouts, counts = parse_career(payload)
        assert len(bouts) == 1
        assert counts["bad_uid"] == 1
        assert counts["missing_entry"] == 1

    def test_bad_clock_and_period_are_nulled(self):
        payload = career(entry(BELLATOR_UID, period=0, clock="LIVE"))
        (bout,), _ = parse_career(payload)
        assert bout.end_round is None
        assert bout.end_time is None

    def test_game_date_uses_us_eastern_convention(self):
        # Cartelera del sábado noche US: llega como domingo 02:00Z pero el
        # evento es del sábado (convención de espn_live_results). Un mediodía
        # europeo (21:00Z) no cruza medianoche del Este: mismo día.
        payload = career(
            entry(BELLATOR_UID, game_date="2020-11-13T02:00:00.000+00:00"),
            entry(REGIONAL_UID, game_date="2026-05-09T21:00:00.000+00:00",
                  name="CFFC 140: X vs Y", short_name="CFFC 140"),
        )
        bouts, _ = parse_career(payload)
        assert bouts[0].event_date == date(2020, 11, 12)
        assert bouts[1].event_date == date(2026, 5, 9)


class TestPromotionLabel:
    def test_known_league_wins_over_name(self):
        assert promotion_label("3323", "Whatever: Card", "Whatever") == "Bellator"

    def test_prefix_before_colon_without_trailing_number(self):
        assert promotion_label("3359", "CFFC 140: Rzgoev vs. Roberts", "CFFC 140") == "CFFC"
        assert promotion_label("3359", "Tech-Krep FC: Prime Selection 17", None) == "Tech-Krep FC"
        assert promotion_label("3339", "IFL: New Jersey", "IFL") == "IFL"

    def test_falls_back_to_short_name_then_regional(self):
        assert promotion_label("3359", None, "ROC 18") == "ROC"
        assert promotion_label("3359", None, None) == "Regional"

    def test_sub_brand_suffix_after_dash_is_dropped(self):
        assert promotion_label(
            "3359",
            "Tech-Krep FC - Ermak Prime Challenge: Prime Selection",
            None,
        ) == "Tech-Krep FC"
        # El guion SIN espacios es parte del nombre, no sub-marca.
        assert promotion_label("3359", "K-1: World GP", None) == "K-1"


class TestCanonicalMethod:
    def test_decisions(self):
        assert canonical_method("decision---unanimous", "Unanimous Decision") == "U-DEC"
        assert canonical_method("unanimous-decision", "Unanimous Decision") == "U-DEC"
        assert canonical_method("decision---split", "Split Decision") == "S-DEC"
        assert canonical_method("decision---majority", "Majority Decision") == "M-DEC"

    def test_knockouts(self):
        assert canonical_method("kotko", "KO/TKO") == "KO/TKO"
        assert canonical_method("ko", "KO") == "KO/TKO"
        assert canonical_method("tko---doctors-stoppage", "TKO (Doctor's Stoppage)") == (
            "KO/TKO - Doctor's Stoppage"
        )
        assert canonical_method("tko-doctor-stoppage", "") == "KO/TKO - Doctor Stoppage"

    def test_submissions(self):
        assert canonical_method("submission", "Submission") == "SUB"
        assert canonical_method("submission-rear-naked-choke", "Submission (Rear Naked Choke)") == (
            "SUB - Rear Naked Choke"
        )
        assert canonical_method("submission-armbar", "") == "SUB - Armbar"

    def test_special_and_unknown(self):
        assert canonical_method("no-contest", "No Contest") == "CNC"
        assert canonical_method("dq", "Disqualification") == "DQ"
        assert canonical_method("something-new", "Something New") == "Something New"
        assert canonical_method(None, None) is None


class TestTransferEspnAssets:
    """El CASCADE de 016 no debe destruir el historial del duplicado fusionado:
    los 3 scripts de fusión llaman a transfer_espn_assets antes de su DELETE."""

    @staticmethod
    def _responder(sql, params):
        flat = " ".join(sql.split())
        if "to_regclass" in flat:
            return [(True,)]
        if "SELECT espn_id FROM fighters" in flat:
            return [("4275020",)]
        return [(1,)]

    def test_moves_history_links_and_identity_to_keeper(self, fakedb):
        from src.scrapers.repositories.espn_history import transfer_espn_assets

        conn = fakedb.Connection(self._responder)
        with conn.cursor() as cursor:
            transfer_espn_assets(cursor, 153, 6191)
        statements = [" ".join(sql.split()) for sql, _ in conn.cursors[0].executed]
        # Duplicadas exactas fuera, resto reasignado, enlaces de rival movidos,
        # self-links anulados, espn_id liberado del duplicado y heredado.
        assert any(s.startswith("DELETE FROM fight_history_espn") for s in statements)
        assert any("SET fighter_id" in s and "fight_history_espn" in s for s in statements)
        assert any("SET opponent_fighter_id = %s" in s for s in statements)
        assert any("opponent_fighter_id = NULL" in s for s in statements)
        assert any("SET espn_id = NULL" in s for s in statements)
        inherit = [s for s in statements if "espn_history_checked_at = NULL" in s]
        assert inherit and "espn_id IS NULL" in inherit[0]

    def test_noop_when_migration_016_not_applied(self, fakedb):
        from src.scrapers.repositories.espn_history import transfer_espn_assets

        conn = fakedb.Connection(
            lambda sql, params: [(False,)] if "to_regclass" in sql else [(1,)]
        )
        with conn.cursor() as cursor:
            transfer_espn_assets(cursor, 153, 6191)
        assert fakedb.mutating_statements(conn) == []


class _Responder:
    """Responder del fake DB: despacha por fragmento de SQL."""

    def __init__(self, targets):
        self.targets = targets

    def __call__(self, sql, params):
        flat = " ".join(sql.split())
        if "FROM fighters f" in flat and "espn_id IS NOT NULL" in flat and "SELECT f.id" in flat:
            return self.targets
        if "SELECT source_id, id FROM fighters" in flat:
            return []
        if "SELECT espn_id, id FROM fighters" in flat:
            return [("5088766", 777)]  # el rival Jason Jackson existe en BD
        if "INSERT INTO fight_history_espn" in flat:
            return [(1,)]  # rowcount 1
        if "SET espn_history_checked_at" in flat:
            return [(1,)]
        return []


class TestBackfill:
    def test_backfill_writes_non_ufc_rows_links_opponent_and_stamps(self, fakedb, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([(6191, "Yaroslav Amosov", "4275020", False, None)]))
        payload = career(entry(UFC_UID, name="UFC 328: X vs Y", short_name="UFC 328"), entry(BELLATOR_UID))

        counts = backfill(
            conn,
            fetcher=lambda session, espn_id: payload,
            sleeper=lambda seconds: None,
        )

        assert counts["written"] == 1
        assert counts["skipped_ufc"] == 1
        assert counts["fighters_with_history"] == 1
        inserts = [s for s in fakedb.mutating_statements(conn) if "fight_history_espn" in s]
        assert len(inserts) == 1
        insert_params = next(
            params for cur in conn.cursors for sql, params in cur.executed
            if "INSERT INTO fight_history_espn" in sql
        )
        assert insert_params[0] == 6191  # fighter_id
        assert insert_params[9] == 777  # opponent_fighter_id enlazado por espn_id
        assert any("espn_history_checked_at" in s for s in fakedb.mutating_statements(conn))
        assert conn.commits >= 1

    def test_backfill_identity_mismatch_writes_nothing_and_does_not_stamp(self, fakedb, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([(6191, "Yaroslav Amosov", "4275020", False, None)]))
        payload = career(entry(BELLATOR_UID))
        payload["athlete"] = {"displayName": "Someone Else Entirely"}

        counts = backfill(
            conn,
            fetcher=lambda session, espn_id: payload,
            sleeper=lambda seconds: None,
        )

        assert counts["name_mismatch"] == 1
        assert counts["written"] == 0
        assert fakedb.mutating_statements(conn) == []

    def test_backfill_seeded_id_trusts_espn_rename(self, fakedb, monkeypatch):
        # Caso real "Zachary Reese" -> ESPN "Zach Reese" (fold_ratio 0.87):
        # con espn_id sembrado desde source_id el enlace es correcto por
        # construcción, así que el guard de nombre no bloquea el import.
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([(9027, "Zachary Reese", "5143223", True, None)]))
        payload = career(entry(BELLATOR_UID))
        payload["athlete"] = {"displayName": "Zach Reese"}

        counts = backfill(
            conn,
            fetcher=lambda session, espn_id: payload,
            sleeper=lambda seconds: None,
        )

        assert counts["name_mismatch"] == 0
        assert counts["written"] == 1
        assert any("espn_history_checked_at" in s for s in fakedb.mutating_statements(conn))

    def test_backfill_dry_run_never_writes(self, fakedb, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([(6191, "Yaroslav Amosov", "4275020", False, None)]))
        payload = career(entry(BELLATOR_UID), entry(REGIONAL_UID, name="CFFC 5: X", short_name="CFFC 5"))

        counts = backfill(
            conn,
            dry_run=True,
            fetcher=lambda session, espn_id: payload,
            sleeper=lambda seconds: None,
        )

        assert counts["would_write"] == 2
        assert fakedb.mutating_statements(conn) == []
        assert conn.commits == 0
        assert conn.rollbacks >= 1


class TestBackfillIdentityGuard:
    """La guarda de identidad EN SU SITIO (t4-9-2): backfill() tiene que usar
    history_identity_ok con las fechas de nacimiento de los dos lados.

    Guarda de mutación: si la condición vuelve al `fold_ratio(...) <
    IDENTITY_THRESHOLD` a secas, los tres primeros tests fallan (Aswell Jr.
    da 0,903 y Spohn 0,857, que la vieja rechazaba; Jr. contra Sr. da 0,941,
    que la vieja aceptaba). Si se deja de leer la fecha de alguno de los dos
    lados, falla el de Spohn. Si el veto del año vuelve a ir antes del ratio,
    falla el de Nicoll; si se quita la lista de pares verificados, el de Souza.
    """

    @staticmethod
    def _run(fakedb, monkeypatch, target, athlete, *, dry_run=False):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([target]))
        payload = career(entry(BELLATOR_UID))
        payload["athlete"] = athlete
        counts = backfill(
            conn,
            dry_run=dry_run,
            fetcher=lambda session, espn_id: payload,
            sleeper=lambda seconds: None,
        )
        return conn, counts

    def test_jr_on_our_side_only_imports(self, fakedb, monkeypatch):
        # Caso real: 6534 es «Michael Aswell Jr.» y ESPN 5212738 «Michael Aswell».
        conn, counts = self._run(
            fakedb, monkeypatch,
            (6534, "Michael Aswell Jr.", "5212738", False, date(2000, 9, 27)),
            {"displayName": "Michael Aswell", "displayDOB": "27/9/2000"},
        )
        assert counts["name_mismatch"] == 0
        assert counts["written"] == 1
        assert any("espn_history_checked_at" in s for s in fakedb.mutating_statements(conn))

    def test_jr_against_sr_is_rejected(self, fakedb, monkeypatch):
        conn, counts = self._run(
            fakedb, monkeypatch,
            (6534, "Michael Aswell Jr.", "5212738", False, None),
            {"displayName": "Michael Aswell Sr."},
        )
        assert counts["name_mismatch"] == 1
        assert counts["written"] == 0
        assert fakedb.mutating_statements(conn) == []

    def test_same_birth_date_and_a_shared_word_imports(self, fakedb, monkeypatch):
        # Caso real: 8219 «Daniel Spohn», ESPN 3024141 «Dan Spohn», los dos
        # nacidos el 12-10-1984 (ESPN lo sirve como '12/10/1984').
        conn, counts = self._run(
            fakedb, monkeypatch,
            (8219, "Daniel Spohn", "3024141", False, date(1984, 10, 12)),
            {"displayName": "Dan Spohn", "displayDOB": "12/10/1984"},
        )
        assert counts["name_mismatch"] == 0
        assert counts["written"] == 1

    def test_shared_word_without_espn_birth_date_is_rejected(self, fakedb, monkeypatch):
        conn, counts = self._run(
            fakedb, monkeypatch,
            (8219, "Daniel Spohn", "3024141", False, date(1984, 10, 12)),
            {"displayName": "Dan Spohn"},
        )
        assert counts["name_mismatch"] == 1
        assert fakedb.mutating_statements(conn) == []

    def test_identical_name_with_another_birth_year_still_imports(self, fakedb, monkeypatch):
        # Caso real: Stewart Nicoll, en activo. UFCStats dice 11-04-1996 y
        # ESPN '4/11/1994'. La guarda vieja lo aceptaba; si el veto del año
        # lo tumba, su historial deja de actualizarse sin ningún aviso.
        conn, counts = self._run(
            fakedb, monkeypatch,
            (7000, "Stewart Nicoll", "4410000", False, date(1996, 4, 11)),
            {"displayName": "Stewart Nicoll", "displayDOB": "4/11/1994"},
        )
        assert counts["name_mismatch"] == 0
        assert counts["written"] == 1

    def test_father_and_son_through_a_relaxed_rule_are_rejected(self, fakedb, monkeypatch):
        # «Aswell Jr.» contra «Aswell» solo pasa por la regla nueva del
        # sufijo, y ahí 30 años de diferencia son padre e hijo.
        conn, counts = self._run(
            fakedb, monkeypatch,
            (6534, "Michael Aswell Jr.", "5212738", False, date(2000, 9, 27)),
            {"displayName": "Michael Aswell", "displayDOB": "27/9/1970"},
        )
        assert counts["name_mismatch"] == 1
        assert fakedb.mutating_statements(conn) == []

    def test_twins_with_the_same_birth_date_are_rejected(self, fakedb, monkeypatch):
        # Los Ellenberger están los dos en nuestra base, nacidos el
        # 28-03-1985: un espn_id cruzado no puede meter la carrera del otro.
        conn, counts = self._run(
            fakedb, monkeypatch,
            (1500, "Joe Ellenberger", "2500000", False, date(1985, 3, 28)),
            {"displayName": "Jake Ellenberger", "displayDOB": "28/3/1985"},
        )
        assert counts["name_mismatch"] == 1
        assert fakedb.mutating_statements(conn) == []

    def test_verified_pair_imports_despite_another_name(self, fakedb, monkeypatch):
        # Caso real: 9084 «Jose Souza» es el ESPN 5080485 «Jose Henrique»
        # (rival UFC Ding Meng 4813565, nacido 23-05-2002 en los dos lados).
        # Ninguna regla general lo separa de MacDonald/Lambert: va por la
        # lista de pares verificados a mano.
        assert (9084, "5080485") in espn_fight_history.VERIFIED_IDENTITY_PAIRS
        conn, counts = self._run(
            fakedb, monkeypatch,
            (9084, "Jose Souza", "5080485", False, date(2002, 5, 23)),
            {"displayName": "Jose Henrique", "displayDOB": "23/5/2002"},
        )
        assert counts["name_mismatch"] == 0
        assert counts["written"] == 1

    @pytest.mark.parametrize(
        "target",
        [
            # La misma pareja de nombres, pero otra ficha o otro id: fuera.
            (9085, "Jose Souza", "5080485", False, date(2002, 5, 23)),
            (9084, "Jose Souza", "5080486", False, date(2002, 5, 23)),
        ],
    )
    def test_the_verified_pair_is_by_both_ids(self, fakedb, monkeypatch, target):
        conn, counts = self._run(
            fakedb, monkeypatch,
            target,
            {"displayName": "Jose Henrique", "displayDOB": "23/5/2002"},
        )
        assert counts["name_mismatch"] == 1
        assert fakedb.mutating_statements(conn) == []

    def test_seeded_rows_are_imported_exactly_as_before(self, fakedb, monkeypatch):
        # Sembrada (source='espn'): se importa aunque la guarda nueva rechace.
        conn, counts = self._run(
            fakedb, monkeypatch,
            (9027, "Michael Aswell Jr.", "5143223", True, date(1993, 9, 4)),
            {"displayName": "Michael Aswell Sr.", "displayDOB": "4/9/1963"},
        )
        assert counts["name_mismatch"] == 0
        assert counts["written"] == 1

    @pytest.mark.parametrize("all_scope", [False, True])
    def test_target_query_selects_our_birth_date(self, fakedb, all_scope):
        conn = fakedb.Connection(lambda sql, params: [])
        espn_fight_history._get_target_fighters(conn, all_scope=all_scope)
        (sql,) = fakedb.executed_statements(conn)
        select_clause = " ".join(sql.split()).split(" FROM ")[0]
        assert "f.birth_date" in select_clause


# Contender Series con datos REALES (sonda del 29-sep-2026): Tommy Gantt
# sometió a Adam Livingston en la Season 9, Week 6. ESPN publica las 99
# veladas del DWCS (2017-2026) bajo la liga UFC (l:3321) y ufcstats no las
# tiene: saltarlas como UFC las perdía de todas partes.
DWCS_UID = "s:3301~l:3321~e:600055031~c:401794769"
DWCS_NAME = "Dana White's Contender Series: Season 9, Week 6"
DWCS_SHORT = "Dana White's Contender Series"
# Martes 16-sep-2025 por la noche en el Este: ESPN lo da ya en miércoles UTC.
DWCS_GAME_DATE = "2025-09-17T00:00:00.000+00:00"


def dwcs_entry(uid: str = DWCS_UID, **overrides) -> dict:
    """entry() con la velada real de Gantt; cada test cambia solo lo suyo."""
    fields: dict = {
        "name": DWCS_NAME,
        "short_name": DWCS_SHORT,
        "result_token": "submission",
        "result_display": "Submission",
        "opponent_name": "Adam Livingston",
        "game_date": DWCS_GAME_DATE,
    }
    fields.update(overrides)
    return entry(uid, **fields)


class TestContenderSeries:
    """El DWCS va en l:3321 pero NO está en `fights`: se guarda con
    promotion='Contender Series' y su liga real. El resto de la 3321 se sigue
    saltando y las regionales de nombre parecido no cambian."""

    def test_dwcs_in_ufc_league_is_kept_as_contender_series(self):
        payload = career(
            entry(UFC_UID, name="UFC Fight Night: Allen vs. Costa",
                  short_name="UFC Fight Night"),
            dwcs_entry(),
            entry(BELLATOR_UID),
        )
        bouts, counts = parse_career(payload)
        assert [bout.promotion for bout in bouts] == ["Contender Series", "Bellator"]
        assert counts["skipped_ufc"] == 1
        assert counts["contender_series"] == 1
        dwcs = bouts[0]
        # Conserva su liga REAL: la alarma post-run la distingue por promotion.
        assert dwcs.league_id == "3321"
        assert dwcs.espn_event_id == "600055031"
        assert dwcs.espn_competition_id == "401794769"
        assert dwcs.event_name == DWCS_NAME
        assert dwcs.event_date == date(2025, 9, 16)
        assert (dwcs.result, dwcs.method) == ("win", "SUB")

    def test_brazil_edition_is_recognized(self):
        # Las 3 veladas de Brasil (ago-2018) no siguen el «Season N, Week M».
        # Johnny Walker salió de la Brazil 2.
        payload = career(
            dwcs_entry(
                "s:3301~l:3321~e:401074497~c:265434",
                name="Dana White's Contender Series: Brazil 2",
                result_token="decision---unanimous",
                result_display="Decision - Unanimous",
                opponent_name="Henrique da Silva",
                game_date="2018-08-12T01:00:00.000+00:00",
            )
        )
        (bout,), counts = parse_career(payload)
        assert bout.promotion == "Contender Series"
        assert bout.event_name == "Dana White's Contender Series: Brazil 2"
        assert bout.event_date == date(2018, 8, 11)
        assert counts["contender_series"] == 1

    def test_curly_apostrophe_in_name_and_in_short_name(self):
        # Una entrada con el apóstrofo curvo solo en name y otra solo en
        # shortName: cada campo tiene que bastar por sí mismo.
        payload = career(
            dwcs_entry(name=DWCS_NAME.replace("'", "\u2019"), short_name=""),
            dwcs_entry(
                "s:3301~l:3321~e:600055031~c:401819852",
                name="",
                short_name=DWCS_SHORT.replace("'", "\u2019"),
            ),
        )
        bouts, counts = parse_career(payload)
        assert [bout.promotion for bout in bouts] == ["Contender Series"] * 2
        assert counts["skipped_ufc"] == 0
        assert counts["contender_series"] == 2

    def test_recognized_by_short_name_alone_or_by_name_alone(self):
        payload = career(
            dwcs_entry(name="", short_name=DWCS_SHORT),
            dwcs_entry("s:3301~l:3321~e:600055031~c:401819852", short_name=""),
        )
        bouts, counts = parse_career(payload)
        assert [bout.promotion for bout in bouts] == ["Contender Series"] * 2
        assert bouts[0].event_name is None
        assert bouts[1].event_name == DWCS_NAME
        assert counts["contender_series"] == 2

    @pytest.mark.parametrize(
        "name",
        [
            "Dana White's Tuesday Night Contender Series: Week 1",
            "DANA WHITE'S CONTENDER SERIES: SEASON 2, WEEK 1",
            "Dana Whites Contender Series: Season 3, Week 1",
            "Dana White Contender Series 2018: Week 1",
            "Contender Series Brasil 1",
        ],
    )
    def test_name_variants_in_ufc_league(self, name):
        # shortName vacío: el nombre tiene que bastar por sí solo.
        (bout,), counts = parse_career(career(dwcs_entry(name=name, short_name="")))
        assert bout.promotion == "Contender Series"
        assert bout.league_id == "3321"
        assert counts["skipped_ufc"] == 0

    def test_dwcs_under_another_league_passes_the_name_guard(self):
        # Fuera de l:3321 lo tiraba la guarda de NOMBRE (_UFC_NAME_RE incluye
        # DANA WHITE): el DWCS tiene que saltarse las dos guardas, no solo la
        # de liga.
        payload = career(dwcs_entry("s:3301~l:3359~e:600055031~c:401794769"))
        (bout,), counts = parse_career(payload)
        assert bout.promotion == "Contender Series"
        assert bout.league_id == "3359"
        assert counts["skipped_ufc_named"] == 0
        assert counts["contender_series"] == 1

    def test_loss_and_no_contest(self):
        # Belgaroui perdió a los puntos en la Season 7, Week 4; Holobaugh hizo
        # un no contest en la Season 1, Week 1.
        payload = career(
            dwcs_entry(
                "s:3301~l:3321~e:600036494~c:401581402",
                name="Dana White's Contender Series: Season 7, Week 4",
                game_result="L",
                result_token="decision---unanimous",
                result_display="Decision - Unanimous",
                game_date="2023-08-30T00:00:00.000+00:00",
            ),
            dwcs_entry(
                "s:3301~l:3321~e:400961602~c:237192",
                name="Dana White's Contender Series: Season 1, Week 1",
                game_result="D",
                result_token="no-contest",
                result_display="No Contest",
                game_date="2017-07-11T19:00:00.000+00:00",
            ),
        )
        bouts, counts = parse_career(payload)
        assert [bout.result for bout in bouts] == ["loss", "nc"]
        assert bouts[0].method == "U-DEC"
        assert bouts[1].method == "CNC"
        assert counts["contender_series"] == 2

    def test_scheduled_dwcs_without_result_is_not_counted(self):
        # contender_series cuenta peleas GUARDADAS: una programada cae antes,
        # en el filtro de resultado.
        payload = career(
            dwcs_entry(game_result=None, result_token="", result_display="")
        )
        bouts, counts = parse_career(payload)
        assert bouts == []
        assert counts["skipped_no_result"] == 1
        assert counts["contender_series"] == 0

    @pytest.mark.parametrize(
        ("name", "short_name"),
        [
            ("UFC 290: Volkanovski vs. Rodriguez", "UFC 290"),
            ("UFC Fight Night: Allen vs. Costa", "UFC Fight Night"),
            ("Noche UFC: Silva vs. Delgado", "Noche UFC"),
            ("UFC 306 \u2013 Riyadh Season Noche UFC: O\u2019Malley vs. Dvalishvili",
             "UFC 306"),
            ("UFC Freedom 250: Topuria vs. Gaethje", "UFC Freedom 250"),
            ("The Ultimate Fighter 31 Semifinal: McGregor vs. Chandler",
             "The Ultimate Fighter 31"),
            ("The Ultimate Fighter 25 Finale", "The Ultimate Fighter 25 Finale"),
            # «Aldana» lleva «dana» dentro: una búsqueda sin anclar picaría.
            ("UFC Fight Night: Holm vs. Aldana", "UFC Fight Night"),
        ],
    )
    def test_real_ufc_events_in_ufc_league_are_still_skipped(self, name, short_name):
        payload = career(entry(UFC_UID, name=name, short_name=short_name))
        bouts, counts = parse_career(payload)
        assert bouts == []
        assert counts["skipped_ufc"] == 1
        assert counts["contender_series"] == 0

    def test_road_to_ufc_keeps_its_current_label(self):
        payload = career(
            entry(REGIONAL_UID, name="Road to UFC: Season 3, Episode 6",
                  short_name="Road to UFC")
        )
        (bout,), counts = parse_career(payload)
        assert bout.promotion == "Road to UFC"
        assert counts["skipped_ufc_named"] == 0
        assert counts["contender_series"] == 0

    # Regionales REALES de la tabla, con la etiqueta que tienen hoy (consultada
    # el 29-sep-2026). La BFC (2 filas) y la CBF (1) son las 3 filas que la
    # regex SIN anclar de la migración 028 habría tomado por el DWCS.
    @pytest.mark.parametrize(
        ("league_id", "name", "short_name", "expected"),
        [
            ("3359", "BFC Contender Series 7: Belarusian Fighting Championship",
             "BFC Contender Series 7", "BFC Contender Series"),
            ("3359", "Caged Steel Contenders 2", "", "Caged Steel Contenders"),
            ("3359", "100% Fight: Contenders 33", "100% Fight", "100% Fight"),
            ("3359", "Contenders 31: Contenders London", "Contenders 31", "Contenders"),
            ("3359", "FCC 40: Full Contact Contender 40", "FCC 40", "FCC"),
            ("3327", "TPF 9: The Contenders", "TPF 9", "TPF"),
            ("3359", "CBF MMA 1: Dana White Lookin' For A Fight",
             "CBF MMA 1", "CBF MMA"),
        ],
    )
    def test_regional_lookalikes_keep_their_label(
        self, league_id, name, short_name, expected
    ):
        uid = f"s:3301~l:{league_id}~e:600000020~c:400000020"
        payload = career(entry(uid, name=name, short_name=short_name))
        (bout,), counts = parse_career(payload)
        assert bout.promotion == expected
        assert bout.league_id == league_id
        assert counts["skipped_ufc_named"] == 0
        assert counts["contender_series"] == 0

    @pytest.mark.parametrize(
        ("league_id", "event_name", "short_name", "expected"),
        [
            # Fuera de la liga UFC, «Contender Series» a secas puede ser
            # cualquier regional.
            ("3359", "Contender Series 5: Regional Night", "Contender Series 5", False),
            # «Dana White» sin «Contender Series» detrás no es el DWCS.
            ("3359", "Dana White's Lookin' for a Fight", None, False),
            # Dentro de la 3321 no hay regionales: ahí sí basta.
            ("3321", "Contender Series Brasil 1", None, True),
            # El nombre completo vale bajo cualquier liga.
            ("3359", DWCS_NAME, None, True),
            # Vacíos fuera, y el ancla se aplica después de quitar espacios.
            ("3321", None, None, False),
            ("3321", "  " + DWCS_NAME + "  ", "", True),
        ],
    )
    def test_is_contender_series(self, league_id, event_name, short_name, expected):
        is_dwcs = espn_fight_history.is_contender_series
        assert is_dwcs(league_id, event_name, short_name) is expected

    def test_backfill_writes_the_dwcs_row(self, fakedb, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([(9080, "Tommy Gantt", "5307814", False, None)]))
        payload = career(
            entry(UFC_UID, name="UFC Fight Night: Allen vs. Costa",
                  short_name="UFC Fight Night"),
            dwcs_entry(),  # rival 5088766 -> 777 en el fake
            entry(BELLATOR_UID),
        )
        payload["athlete"] = {"displayName": "Tommy Gantt"}

        counts = backfill(
            conn,
            fetcher=lambda session, espn_id: payload,
            sleeper=lambda seconds: None,
        )

        assert counts["written"] == 2
        assert counts["skipped_ufc"] == 1
        assert counts["contender_series"] == 1
        dwcs_inserts = [
            params for cur in conn.cursors for sql, params in cur.executed
            if "INSERT INTO fight_history_espn" in sql and params[1] == "401794769"
        ]
        assert len(dwcs_inserts) == 1
        # Posiciones del INSERT de repositories/espn_history.py.
        params = dwcs_inserts[0]
        assert params[0] == 9080  # fighter_id
        assert params[3] == "3321"  # league_id real
        assert params[4] == "Contender Series"  # promotion
        assert params[5] == DWCS_NAME  # event_name
        assert params[6] == date(2025, 9, 16)  # event_date
        assert params[9] == 777  # opponent_fighter_id enlazado por espn_id
        assert params[10] == "win"  # result
        mutations = fakedb.mutating_statements(conn)
        assert any("espn_history_checked_at" in s for s in mutations)

    def test_backfill_dry_run_counts_dwcs_but_never_writes(self, fakedb, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([(9080, "Tommy Gantt", "5307814", False, None)]))
        payload = career(dwcs_entry(), entry(BELLATOR_UID))
        payload["athlete"] = {"displayName": "Tommy Gantt"}

        counts = backfill(
            conn,
            dry_run=True,
            fetcher=lambda session, espn_id: payload,
            sleeper=lambda seconds: None,
        )

        assert counts["would_write"] == 2
        assert counts["contender_series"] == 1
        assert fakedb.mutating_statements(conn) == []
        assert conn.commits == 0

    def test_count_ufc_league_leaks(self, fakedb):
        conn = fakedb.Connection(lambda sql, params: [(3,)])
        assert espn_fight_history.count_ufc_league_leaks(conn) == 3
        (sql,) = fakedb.executed_statements(conn)
        flat = " ".join(sql.split())
        assert "FROM fight_history_espn" in flat
        assert "league_id = '3321'" in flat
        assert "IS DISTINCT FROM 'Contender Series'" in flat
        assert fakedb.mutating_statements(conn) == []

    @staticmethod
    def _prepare_main(monkeypatch, fakedb, *, leaks: int, argv: list[str]):
        """main() con la base y el backfill falsos: la consulta de la alarma
        responde `leaks` y el resumen trae un DWCS."""
        conn = fakedb.Connection(
            lambda sql, params: [(leaks,)] if "fight_history_espn" in sql else []
        )

        @contextmanager
        def fake_connect(database_url):
            yield conn

        settings = SimpleNamespace(database_url="x")
        monkeypatch.setattr(espn_fight_history, "get_settings", lambda: settings)
        monkeypatch.setattr(espn_fight_history, "connect", fake_connect)
        monkeypatch.setattr(espn_fight_history, "configure_logging", lambda: None)
        monkeypatch.setattr(
            espn_fight_history,
            "backfill",
            lambda connection, **kwargs: Counter(
                targets=1, would_write=1, contender_series=1
            ),
        )
        monkeypatch.setattr(sys, "argv", ["espn_fight_history", *argv])
        return conn

    def test_main_goes_red_when_ufc_rows_leak_even_in_dry_run(
        self, fakedb, monkeypatch, capsys, caplog
    ):
        conn = self._prepare_main(
            monkeypatch, fakedb, leaks=2, argv=["--dry-run"]
        )

        with pytest.raises(SystemExit) as excinfo:
            espn_fight_history.main()

        # Código 1 para que el workflow se ponga en rojo y avise.
        assert excinfo.value.code == 1
        # El resumen sale igual que siempre, antes de la alarma.
        assert '"contender_series": 1' in capsys.readouterr().out
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and " 2 " in errors[0]
        assert fakedb.mutating_statements(conn) == []

    def test_main_stays_green_without_leaks(self, fakedb, monkeypatch, capsys):
        conn = self._prepare_main(monkeypatch, fakedb, leaks=0, argv=[])

        espn_fight_history.main()  # sin SystemExit

        assert '"contender_series": 1' in capsys.readouterr().out
        assert any("fight_history_espn" in s for s in fakedb.executed_statements(conn))
