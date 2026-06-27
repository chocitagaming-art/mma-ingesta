"""recover_winners_eventpage must never clear a stored winner by absence, and is
dry-run by default (issue #1)."""

from src.scrapers import recover_winners_eventpage as recover
from src.scrapers.recover_winners_eventpage import compute_updates


def _fight(**overrides) -> dict:
    base = {
        "id": 1,
        "source_id": "f1",
        "fighter_red_id": 10,
        "fighter_blue_id": 20,
        "red_src": "ra",
        "blue_src": "rb",
        "winner_id": None,
    }
    base.update(overrides)
    return base


def test_skips_fights_not_present_in_scrape():
    fights = [_fight(winner_id=10)]
    updates, counts = compute_updates(winner_map={}, fighter_by_src={}, fights=fights)
    assert updates == []  # untouched -> a missing event page cannot wipe anything
    assert counts["skipped_not_scraped"] == 1


def test_never_nulls_existing_winner_when_status_undetermined():
    fights = [_fight(winner_id=10)]
    winner_map = {"f1": {"winner_src": None, "status": "undetermined"}}
    updates, counts = compute_updates(winner_map, fighter_by_src={}, fights=fights)
    assert len(updates) == 1
    _, _, new_winner, fight_id = updates[0]
    assert fight_id == 1
    assert new_winner == 10  # preserved, not nulled
    assert counts["winner_preserved"] == 1


def test_never_nulls_existing_winner_when_winner_unresolved():
    fights = [_fight(winner_id=10)]
    # decided, but the scraped winner is not in our DB -> must keep existing winner
    winner_map = {"f1": {"winner_src": "ghost", "status": "decided"}}
    updates, counts = compute_updates(winner_map, fighter_by_src={}, fights=fights)
    assert updates[0][2] == 10
    assert counts["winner_fighter_not_in_db"] == 1


def test_sets_winner_for_decided_fight():
    fights = [_fight(winner_id=None)]
    winner_map = {"f1": {"winner_src": "ra", "status": "decided"}}
    updates, counts = compute_updates(winner_map, fighter_by_src={"ra": 10}, fights=fights)
    assert updates[0][2] == 10
    assert counts["decided"] == 1


def test_clears_winner_only_on_confirmed_draw():
    fights = [_fight(winner_id=10)]
    winner_map = {"f1": {"winner_src": None, "status": "draw_nc"}}
    updates, counts = compute_updates(winner_map, fighter_by_src={}, fights=fights)
    assert updates[0][2] is None  # a confirmed draw legitimately clears the winner
    assert counts["draw_nc"] == 1


def test_apply_updates_dry_run_writes_nothing(monkeypatch, fakedb):
    fighters_rows = [{"id": 10, "source_id": "ra"}, {"id": 20, "source_id": "rb"}]
    fight_rows = [_fight(winner_id=10)]

    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "FROM fighters WHERE source = 'ufcstats'" in flat:
            return fighters_rows
        if "FROM fights f" in flat:
            return fight_rows
        return []

    conn = fakedb.Connection(responder)
    monkeypatch.setattr(recover, "connect", lambda url: conn)

    class _Settings:
        database_url = "postgres://fake/db"

    recover.apply_updates({"f1": {"winner_src": "ra", "status": "decided"}}, apply=False, settings=_Settings())

    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
