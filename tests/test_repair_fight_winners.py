"""repair_fight_winners must never NULL a stored winner when the fight page has
no parseable W/L status (empty / no-contest / draw). Regression guard: previously
winner_corner=None fell through to update_fight_winner(..., None), wiping a win."""

from types import SimpleNamespace

from bs4 import BeautifulSoup

from src.scrapers import main


def _page(statuses_html: str):
    html = (
        '<div class="b-fight-details__person-name">'
        '<a href="/fighter-details/RED">Red</a></div>'
        '<div class="b-fight-details__person-name">'
        '<a href="/fighter-details/BLUE">Blue</a></div>'
        f"{statuses_html}"
    )
    return SimpleNamespace(soup=BeautifulSoup(html, "html.parser"))


class _FakeClient:
    def __init__(self, page):
        self._page = page

    def fetch(self, url):
        return self._page


class _FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _wire(monkeypatch, page) -> list[tuple[int, int | None]]:
    settings = SimpleNamespace(database_url="postgres://fake/db", source_name="ufcstats")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "UfcStatsClient", lambda _settings: _FakeClient(page))
    monkeypatch.setattr(main, "connect", lambda _url: _FakeConn())
    monkeypatch.setattr(main, "list_fights_for_winner_repair", lambda _c: [(1, "/fight-details/abc")])
    monkeypatch.setattr(main, "get_fight_corner_assignment", lambda _c, _fid: (10, 20))
    monkeypatch.setattr(main, "swap_fight_corners", lambda _c, _fid: None)
    id_by_src = {"/fighter-details/RED": 10, "/fighter-details/BLUE": 20}
    monkeypatch.setattr(
        main,
        "_resolve_fighter_id",
        lambda _c, _cache, _src, source_id: id_by_src.get(source_id),
    )
    winner_updates: list[tuple[int, int | None]] = []
    monkeypatch.setattr(
        main,
        "update_fight_winner",
        lambda _c, fight_id, winner_id: winner_updates.append((fight_id, winner_id)),
    )
    return winner_updates


def test_no_contest_status_does_not_null_winner(monkeypatch):
    page = _page(
        '<i class="b-fight-details__person-status">NC</i>'
        '<i class="b-fight-details__person-status">NC</i>'
    )
    winner_updates = _wire(monkeypatch, page)
    counts = main.repair_fight_winners()
    assert winner_updates == []  # never called -> stored winner preserved
    assert counts["repair_skipped_no_winner"] == 1
    assert counts["repair_updated"] == 0


def test_empty_status_does_not_null_winner(monkeypatch):
    page = _page("")  # no status nodes at all
    winner_updates = _wire(monkeypatch, page)
    counts = main.repair_fight_winners()
    assert winner_updates == []
    assert counts["repair_skipped_no_winner"] == 1
    assert counts["repair_updated"] == 0


def test_decided_status_still_sets_winner(monkeypatch):
    page = _page(
        '<i class="b-fight-details__person-status">W</i>'
        '<i class="b-fight-details__person-status">L</i>'
    )
    winner_updates = _wire(monkeypatch, page)
    counts = main.repair_fight_winners()
    assert winner_updates == [(1, 10)]  # red won -> stored red id, guard intact
    assert counts["repair_updated"] == 1
    assert counts["repair_skipped_no_winner"] == 0
