"""The fights upsert must never degrade stored winner/result/enrichment columns to
NULL on a re-scrape (#20). A re-scrape that no longer resolves a winner (or that
always passes NULL for separately-enriched odds/weight) must keep the stored value.
"""

from src.scrapers.models import FightRecord
from src.scrapers.repositories.fights import upsert_fight


def _fight(winner_id):
    return FightRecord(
        event_id=1,
        fighter_red_id=10,
        fighter_blue_id=20,
        weight_class="Lightweight",
        weight_grams=None,
        scheduled_rounds=3,
        winner_id=winner_id,
        method="Decision",
        end_round=3,
        end_time="5:00",
        odds_red=None,
        odds_blue=None,
        source="ufcstats",
        source_id="/fight-details/abc",
    )


def _normalize(sql):
    return " ".join(sql.split())


def test_upsert_preserves_winner_id_with_coalesce(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    upsert_fight(conn, _fight(winner_id=None))
    sql = _normalize(fakedb.executed_statements(conn)[0])
    assert "winner_id = COALESCE(EXCLUDED.winner_id, fights.winner_id)" in sql


def test_upsert_passes_winner_id_value_when_present(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    fight_id = upsert_fight(conn, _fight(winner_id=42))
    assert fight_id == 1
    _, params = conn.cursors[0].executed[0]
    # The supplied winner flows in as EXCLUDED.winner_id, so COALESCE updates it.
    assert 42 in params


def test_upsert_preserves_other_enrichment_columns(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    upsert_fight(conn, _fight(winner_id=None))
    sql = _normalize(fakedb.executed_statements(conn)[0])
    for col in ("odds_red", "odds_blue", "weight_grams", "method", "end_round", "end_time"):
        assert f"{col} = COALESCE(EXCLUDED.{col}, fights.{col})" in sql
