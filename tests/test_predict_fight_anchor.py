"""The fight page asks for the prediction OF ITS OWN FIGHT (owner decision, 4-oct-2026).

"Mercado vs Modelo" on /fights/[id] sets the market against the model while
the fight is pending. Since decision nº 11 a pair with nothing pending is
predicted "as if they fought today", so the moment a result is written the
fight itself enters both histories and the model "predicts" a result it has
already seen: measured on UFC 332 after the results, the model favourite flipped
to the real winner in 6 of 12 bouts.

The request now carries the fight id (``fightId``) and the service anchors to
THAT fight (its event_date, weight class, scheduled rounds and title status)
even when it is already decided; the strict ``event_date <`` cut keeps its own
result, and everything after it, out of both histories.

A fightId that is absent, unknown, of a cancelled bout (the loader drops those,
so it is not in the frame) or of a bout between other fighters falls back to
the normal rule (the pending bout, else today): a cancelled bout has no result,
so the fallback cannot leak. ``context.anchor`` says which rule anchored
("fight", "pending", "today", "none") and ``context.anchorFightId`` which bout.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import src.prediction.service as service
from src.prediction import api
from src.prediction.features import (
    DEFAULT_SCHEDULED_ROUNDS,
    FEATURE_COLUMNS,
    build_fighter_history_dataframe,
)

TODAY = date(2026, 10, 4)


class _FrozenToday(date):
    """`date` with `today()` pinned to TODAY, patched into the api module."""

    @classmethod
    def today(cls) -> date:
        return TODAY


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(api, "date", _FrozenToday)
    return TODAY


# ------------------------------------------------ _get_latest_matchup_context

RED, BLUE, THIRD, FOURTH = 11, 22, 33, 44

OLD_MEETING = 400       # 2019, RED-BLUE, decided, 5-round title fight
DECIDED = 500           # yesterday's card, RED-BLUE, BLUE won
PENDING = 600           # booked ahead, RED-BLUE, no result yet
OTHER_PAIR = 700        # THIRD-FOURTH, decided
ONLY_ONE_OF_THEM = 800  # RED-THIRD, decided
UNKNOWN = 999_999       # not in the frame: unknown, or cancelled (the loader drops it)

DECIDED_DATE = date(2026, 10, 3)
PENDING_DATE = date(2026, 11, 21)


def _bout(
    fight_id: int,
    event_date: date,
    red: int,
    blue: int,
    winner: int | None,
    method: str | None,
    *,
    weight_class: str = "Lightweight",
    scheduled_rounds: int = 3,
    is_title_fight: bool = False,
) -> dict:
    return {
        "fight_id": fight_id,
        "event_date": event_date,
        "fighter_red_id": red,
        "fighter_blue_id": blue,
        "winner_id": winner,
        "method": method,
        "weight_class": weight_class,
        "scheduled_rounds": scheduled_rounds,
        "is_title_fight": is_title_fight,
    }


def _frame(*bouts: dict) -> pd.DataFrame:
    return pd.DataFrame(list(bouts))


def _decided_pair() -> pd.DataFrame:
    """RED and BLUE met in 2019 and again yesterday; nothing pending."""
    return _frame(
        _bout(
            OLD_MEETING, date(2019, 3, 2), BLUE, RED, RED, "U-DEC",
            weight_class="Featherweight", scheduled_rounds=5, is_title_fight=True,
        ),
        _bout(ONLY_ONE_OF_THEM, date(2025, 5, 5), RED, THIRD, RED, "KO/TKO"),
        _bout(OTHER_PAIR, date(2025, 8, 9), THIRD, FOURTH, THIRD, "SUB"),
        _bout(
            DECIDED, DECIDED_DATE, RED, BLUE, BLUE, "KO/TKO - Punches",
            weight_class="Welterweight",
        ),
    )


def _pending_pair() -> pd.DataFrame:
    """The same pair with a rematch booked ahead and nothing else changed."""
    return pd.concat(
        [
            _decided_pair(),
            _frame(
                _bout(
                    PENDING, PENDING_DATE, BLUE, RED, None, None,
                    weight_class="Middleweight", scheduled_rounds=5, is_title_fight=True,
                )
            ),
        ],
        ignore_index=True,
    )


def test_decided_fight_id_anchors_to_that_fight():
    """Yesterday's decided bout, asked for by its id: its own date, division,
    rounds and title status, not today's."""
    context = api._get_latest_matchup_context(
        _decided_pair(), RED, BLUE, fight_id=DECIDED
    )

    assert context == (DECIDED_DATE, "Welterweight", 3, False, "fight", DECIDED)
    assert context.anchor == "fight"
    assert context.anchor_fight_id == DECIDED


def test_fight_id_picks_that_meeting_not_the_latest():
    """The id names the bout: an older meeting of the same pair anchors to
    that one, with its own 5 title rounds, even though a later one exists."""
    context = api._get_latest_matchup_context(
        _decided_pair(), RED, BLUE, fight_id=OLD_MEETING
    )

    assert context == (date(2019, 3, 2), "Featherweight", 5, True, "fight", OLD_MEETING)


@pytest.mark.parametrize(("red", "blue"), [(RED, BLUE), (BLUE, RED)], ids=["stored", "swapped"])
def test_fight_id_matches_whatever_the_corner_order(red, blue):
    """OLD_MEETING is stored BLUE-RED and DECIDED RED-BLUE: the request's corner
    order does not have to match the stored one."""
    frame = _decided_pair()

    assert api._get_latest_matchup_context(frame, red, blue, fight_id=OLD_MEETING).anchor == "fight"
    assert api._get_latest_matchup_context(frame, red, blue, fight_id=DECIDED).anchor == "fight"


def test_fight_id_of_the_pending_bout_is_the_same_anchor_as_no_fight_id():
    """A pending fight asked for by its id gives exactly what the pair gives
    without it; only the label says the caller named it."""
    frame = _pending_pair()

    with_id = api._get_latest_matchup_context(frame, RED, BLUE, fight_id=PENDING)
    without = api._get_latest_matchup_context(frame, RED, BLUE)

    assert with_id[:4] == without[:4] == (PENDING_DATE, "Middleweight", 5, True)
    assert with_id.anchor_fight_id == without.anchor_fight_id == PENDING
    assert with_id.anchor == "fight"
    assert without.anchor == "pending"


@pytest.mark.parametrize(
    "fight_id",
    [None, UNKNOWN, OTHER_PAIR, ONLY_ONE_OF_THEM],
    ids=["absent", "unknown-or-cancelled", "other-pair", "only-one-of-them"],
)
@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (_pending_pair, (PENDING_DATE, "Middleweight", 5, True, "pending", PENDING)),
        (
            _decided_pair,
            (TODAY, "Welterweight", DEFAULT_SCHEDULED_ROUNDS, None, "today", None),
        ),
    ],
    ids=["pair-with-pending", "pair-without-pending"],
)
def test_fight_id_that_is_not_this_pairs_falls_back_to_the_normal_rule(
    fight_id, frame, expected
):
    """Not this pair's bout -> the normal rule, as if no id had been sent:
    the pending bout when there is one, else today."""
    context = api._get_latest_matchup_context(frame(), RED, BLUE, fight_id=fight_id)

    assert context == expected
    assert context == api._get_latest_matchup_context(frame(), RED, BLUE)


@pytest.mark.parametrize("fight_id", [None, UNKNOWN])
def test_neither_fighter_has_any_fight_is_anchor_none(fight_id):
    """Two debutants: today's date and no bout at all, said as "none"."""
    empty = pd.DataFrame(columns=list(_bout(1, TODAY, RED, BLUE, None, None)))

    context = api._get_latest_matchup_context(empty, RED, BLUE, fight_id=fight_id)

    assert context == (TODAY, None, DEFAULT_SCHEDULED_ROUNDS, None, "none", None)


# ------------------------------------------------- _build_feature_row, end to end

A, B, X, Y = 1, 2, 3, 4
A_BEATS_X = 10      # 2024-01-10
B_BEATS_Y = 11      # 2024-06-15
A_BEATS_Y = 12      # 2025-02-01
B_BEATS_X = 13      # 2025-03-01
THE_FIGHT = 20      # yesterday: B beats A, the result is already written
X_AFTER = 21        # 2026-10-03 too, another bout of the same card

STATS = {
    "sig_strikes_landed": 40,
    "sig_strikes_attempted": 90,
    "takedowns_landed": 1,
    "takedowns_attempted": 3,
    "submission_attempts": 1,
    "control_time_seconds": 120,
    "knockdowns": 0,
}


def _full_bout(fight_id: int, event_date: date, red: int, blue: int, winner: int) -> dict:
    """A decided bout with every column ``load_base_dataframe`` returns."""
    row = {
        **_bout(fight_id, event_date, red, blue, winner, "KO/TKO", weight_class="Welterweight"),
        "event_id": fight_id,
        "end_round": 2,
        "end_time": "3:10",
        "red_birth_date": date(1994, 1, 1),
        "red_height_cm": 180.0,
        "red_reach_cm": 185.0,
        "blue_birth_date": date(1995, 1, 1),
        "blue_height_cm": 178.0,
        "blue_reach_cm": 183.0,
    }
    for corner in ("red", "blue"):
        for stat, value in STATS.items():
            row[f"{corner}_{stat}"] = value
    return row


def _card() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _full_bout(A_BEATS_X, date(2024, 1, 10), A, X, A),
            _full_bout(B_BEATS_Y, date(2024, 6, 15), B, Y, B),
            _full_bout(A_BEATS_Y, date(2025, 2, 1), A, Y, A),
            _full_bout(B_BEATS_X, date(2025, 3, 1), X, B, B),
            _full_bout(THE_FIGHT, DECIDED_DATE, A, B, B),
            _full_bout(X_AFTER, DECIDED_DATE, X, Y, Y),
        ]
    )


EMPTY_RANKINGS = pd.DataFrame(columns=["fighter_id", "division", "rank_position", "snapshot_date"])


def _context(fight_id: int | None) -> dict:
    fights_df = _card()
    _row, _method_row, context, _low = api._build_feature_row(
        fights_df,
        EMPTY_RANKINGS,
        A,
        B,
        physical={},
        history_df=build_fighter_history_dataframe(fights_df),
        fight_id=fight_id,
    )
    return context


def test_decided_fight_id_keeps_its_own_result_out_of_both_histories():
    """Asked for by its id, yesterday's A-B bout is the anchor: both fighters
    come in with the two wins they had BEFORE it, and B's win over A is in
    neither history."""
    context = _context(THE_FIGHT)

    assert context["anchor"] == "fight"
    assert context["anchorFightId"] == THE_FIGHT
    assert context["matchupDate"] == DECIDED_DATE.isoformat()
    a, b = context["redHistory"], context["blueHistory"]
    assert a["total_prior_fights"] == b["total_prior_fights"] == 2
    assert a["win_streak"] == b["win_streak"] == 2
    assert a["wins_last_5"] == b["wins_last_5"] == 2
    assert a["latest_prior_fight_date"] == date(2025, 2, 1)
    assert b["latest_prior_fight_date"] == date(2025, 3, 1)


def test_without_fight_id_the_same_pair_is_predicted_today_with_the_result_in():
    """The leak the fight page closes: the same pair asked for without the id is
    predicted today, and the decided bout is history for both (A's loss breaks
    his streak, B's win extends it)."""
    context = _context(None)

    assert context["anchor"] == "today"
    assert context["anchorFightId"] is None
    assert context["matchupDate"] == TODAY.isoformat()
    a, b = context["redHistory"], context["blueHistory"]
    assert a["total_prior_fights"] == b["total_prior_fights"] == 3
    assert a["win_streak"] == 0
    assert b["win_streak"] == 3


# ----------------------------------------- POST /predict, real service + api.predict


class _Identity:
    def transform(self, frame: pd.DataFrame) -> np.ndarray:
        return frame.to_numpy(dtype=float, na_value=np.nan)


class _Coin:
    """No booster (no attributions) and a flat 50/50: the test is about the
    context the request produces, not about the numbers."""

    def predict_proba(self, rows) -> np.ndarray:
        return np.tile([0.5, 0.5], (len(rows), 1))


@pytest.fixture
def client(monkeypatch):
    """The REAL FastAPI app and the REAL api.predict over the in-memory card;
    only what would open a socket to Neon is stubbed."""
    fights_df = _card()
    history_df = build_fighter_history_dataframe(fights_df)
    bundle = {
        "feature_columns": list(FEATURE_COLUMNS),
        "imputer": _Identity(),
        "model": _Coin(),
        "trained_at": "2026-10-01",
    }
    monkeypatch.setattr(
        api, "get_settings", lambda: SimpleNamespace(database_url="postgresql://stub.invalid")
    )
    monkeypatch.setattr(api, "_load_fighter_physical", lambda _url, _ids: {})
    monkeypatch.setattr(
        api,
        "_load_fighter_profiles",
        lambda _url, ids: {
            fighter_id: api.FighterPredictionProfile(
                id=fighter_id, name=f"F{fighter_id}", nickname=None, headshot_url=None,
                wins=1, losses=0, draws=0, height_cm=None, reach_cm=None, stance=None,
                latest_weight_class=None, aggregate_stats={},
            )
            for fighter_id in ids
        },
    )
    monkeypatch.setattr(service, "_existing_fighter_ids", lambda ids: set(ids))
    monkeypatch.setattr(service, "_get_bundle", lambda: bundle)
    monkeypatch.setattr(
        service, "_get_dataframes", lambda: (fights_df, EMPTY_RANKINGS, history_df)
    )
    for name in service.API_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PREDICTION_ENV", raising=False)
    return TestClient(service.app, raise_server_exceptions=False)


def _anchor(response) -> tuple[str, int | None, str]:
    assert response.status_code == 200, response.text
    context = response.json()["context"]
    return context["anchor"], context["anchorFightId"], context["matchupDate"]


@pytest.mark.parametrize(("red", "blue"), [(A, B), (B, A)], ids=["stored", "swapped"])
def test_endpoint_with_fight_id_anchors_to_that_fight(client, red, blue):
    response = client.post("/predict", json={"red": red, "blue": blue, "fightId": THE_FIGHT})

    assert _anchor(response) == ("fight", THE_FIGHT, DECIDED_DATE.isoformat())


@pytest.mark.parametrize(
    "body",
    [
        {"red": A, "blue": B},
        {"red": A, "blue": B, "fightId": None},
        {"red": A, "blue": B, "fightId": UNKNOWN},
        {"red": A, "blue": B, "fightId": X_AFTER},
    ],
    ids=["absent", "null", "unknown-or-cancelled", "other-pair"],
)
def test_endpoint_without_this_pairs_fight_id_predicts_today(client, body):
    assert _anchor(client.post("/predict", json=body)) == ("today", None, TODAY.isoformat())


def test_endpoint_still_ignores_unknown_fields(client):
    """A newer web may send more than this service knows: 200, as before."""
    response = client.post(
        "/predict", json={"red": A, "blue": B, "fightId": THE_FIGHT, "somethingNew": [1, 2]}
    )

    assert _anchor(response) == ("fight", THE_FIGHT, DECIDED_DATE.isoformat())


def test_endpoint_rejects_a_fight_id_that_is_not_a_number(client):
    response = client.post("/predict", json={"red": A, "blue": B, "fightId": "yesterday"})

    assert response.status_code == 400
    assert "error" in response.json()


def test_request_model_ignores_extra_fields():
    """Deploy order is web first: the web then sends ``fightId`` to a service
    that may not know it yet. That only works because pydantic v2's default is
    ``extra='ignore'``; pinned here so nobody forbids extras by accident."""
    assert service.PredictRequest.model_config.get("extra", "ignore") == "ignore"
    request = service.PredictRequest.model_validate({"red": 1, "blue": 2, "unknown": True})
    assert (request.red, request.blue, request.fightId) == (1, 2, None)
