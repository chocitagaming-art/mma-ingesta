"""Tests del import de historial ESPN (S3-G): parser, normalización de
métodos, etiquetas de promoción y el loop de backfill contra el fake DB.

La forma de los payloads reproduce el endpoint real common/v3 (sondado el
11-jul-2026 con Amosov 4275020 y Jim Miller 2335718): events = lista de uids
`s:3301~l:<liga>~e:<evento>~c:<competición>` y eventsMap con gameDate,
gameResult (W/L/D), opponent, status.{period,displayClock,result} y
titleFight. La liga 3321 es UFC (SIEMPRE saltada), 3323 Bellator y el resto
regionales (3335, 3339, 3359...).
"""

from __future__ import annotations

from datetime import date

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
    game_date: str | None = "2023-11-17T03:00:00.000+00:00",
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
        )
        bouts, counts = parse_career(payload)
        assert bouts == []
        assert counts["skipped_ufc_named"] == 1

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
        conn = fakedb.Connection(_Responder([(6191, "Yaroslav Amosov", "4275020")]))
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
        conn = fakedb.Connection(_Responder([(6191, "Yaroslav Amosov", "4275020")]))
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

    def test_backfill_dry_run_never_writes(self, fakedb, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        conn = fakedb.Connection(_Responder([(6191, "Yaroslav Amosov", "4275020")]))
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
