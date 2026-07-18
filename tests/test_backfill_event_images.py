"""Poster backfill for PAST events: slug derivation, the soft-404 fetch guard,
the dry-run/apply loop, and the first-writer-wins repository write. No network,
no DB (fakedb recorder + a fake requests session)."""

from datetime import date

import requests

from src.scrapers.backfill_event_images import (
    MONTHS,
    derive_slug,
    fetch_poster,
    run,
)
from src.scrapers.config import Settings
from src.scrapers.repositories.events import set_event_image

# background_image is the Drupal style ufc.com uses for the event poster.
_POSTER = (
    "https://ufc.com/images/styles/background_image_sm/s3/2025-12/"
    "013125-ufc-325-volkanovski-vs-lopes-2-EVENT-ART.jpg"
)


def _settings():
    # request_delay_seconds=0 so the tests never actually sleep.
    return Settings(database_url="x", anthropic_api_key=None, request_delay_seconds=0)


# ----------------------------------------------------------------- derive_slug


def test_derive_slug_prefers_stored_ufc_slug():
    assert derive_slug(
        "UFC Fight Night: Song vs. Figueiredo", "ufc.com",
        "ufc-fight-night-may-30-2026", date(2026, 5, 30),
    ) == ("ufc-fight-night-may-30-2026", "slug")


def test_derive_slug_numbered_ppv():
    assert derive_slug(
        "UFC 325: Volkanovski vs. Lopes 2", None, None, date(2026, 1, 31)
    ) == ("ufc-325", "ppv")


def test_derive_slug_ppv_beats_fight_night_branch():
    # A numbered event resolves as ppv even though the FN branch exists.
    slug, strategy = derive_slug("UFC 300: Pereira vs. Hill", None, None, date(2026, 4, 13))
    assert (slug, strategy) == ("ufc-300", "ppv")


def test_derive_slug_fight_night_by_date_is_zero_padded():
    assert derive_slug(
        "UFC Fight Night: Della Maddalena vs. Prates", None, None, date(2026, 5, 2)
    ) == ("ufc-fight-night-may-02-2026", "fn")


def test_derive_slug_undeivable_for_tuf_and_broadcaster_cards():
    assert derive_slug(
        "The Ultimate Fighter: Redemption Finale", None, None, date(2017, 7, 7)
    ) == (None, "undeivable")
    assert derive_slug(
        "UFC on FOX: Johnson vs. Reis", None, None, date(2017, 4, 15)
    ) == (None, "undeivable")


def test_derive_slug_undeivable_when_fight_night_has_no_date():
    assert derive_slug("UFC Fight Night: X vs. Y", None, None, None) == (None, "undeivable")


def test_derive_slug_covers_every_month():
    for month in range(1, 13):
        slug, strategy = derive_slug("UFC Fight Night: X vs. Y", None, None, date(2026, month, 5))
        assert strategy == "fn"
        assert f"-{MONTHS[month - 1]}-05-2026" in slug


# ----------------------------------------------------------------- fetch_poster


class _FakeResp:
    def __init__(self, status_code=200, url="", text="", ok=None):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.ok = ok if ok is not None else (200 <= status_code < 400)
        self.encoding = None


class _FakeSession:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._resp


def test_fetch_poster_returns_background_image():
    html = f'<html><body><img src="{_POSTER}"></body></html>'
    resp = _FakeResp(200, "https://www.ufc.com/event/ufc-325", html)
    session = _FakeSession(resp)
    img = fetch_poster(session, _settings(), "ufc-325")
    assert img == _POSTER
    assert session.calls == ["https://www.ufc.com/event/ufc-325"]


def test_fetch_poster_rejects_search_redirect():
    # A bad slug returns HTTP 200 but its final URL is /search, not /event/.
    resp = _FakeResp(200, "https://www.ufc.com/search?query=event%20ufc%209999", f'<img src="{_POSTER}">')
    assert fetch_poster(_FakeSession(resp), _settings(), "ufc-9999") is None


def test_fetch_poster_none_on_404():
    resp = _FakeResp(404, "https://www.ufc.com/event/nope", "")
    assert fetch_poster(_FakeSession(resp), _settings(), "nope") is None


def test_fetch_poster_none_on_request_error():
    session = _FakeSession(exc=requests.ConnectionError("boom"))
    assert fetch_poster(session, _settings(), "nope") is None


def test_fetch_poster_none_when_page_has_no_poster():
    resp = _FakeResp(200, "https://www.ufc.com/event/ufc-325", "<html><body>no image</body></html>")
    assert fetch_poster(_FakeSession(resp), _settings(), "ufc-325") is None


# ------------------------------------------------------------------------- run


def _responder(rows, update_rows=None):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return rows
        if "UPDATE" in upper:
            return update_rows if update_rows is not None else [(1,)]
        return []

    return responder


_PPV_ROW = (304, "UFC 323: Dvalishvili vs. Yan 2", None, None, date(2025, 12, 6))
_FN_ROW = (303, "UFC Fight Night: Royval vs. Kape", None, None, date(2025, 12, 13))
_UNDEIVABLE_ROW = (1084, "UFC Freedom 250", None, None, date(2026, 6, 14))


def test_run_apply_writes_resolved_and_skips_undeivable(fakedb):
    conn = fakedb.Connection(_responder([_PPV_ROW, _UNDEIVABLE_ROW], update_rows=[(1,)]))
    counts = run(conn, apply=True, fetch_image=lambda slug: _POSTER)
    assert counts["targets"] == 2
    assert counts["skip_undeivable"] == 1
    assert counts["try_ppv"] == 1
    assert counts["resolved"] == 1
    assert counts["written"] == 1
    assert conn.commits == 1
    muts = fakedb.mutating_statements(conn)
    assert len(muts) == 1
    assert "image_url IS NULL OR image_url = ''" in muts[0]


def test_run_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder([_PPV_ROW]))
    counts = run(conn, apply=False, fetch_image=lambda slug: _POSTER)
    assert counts["resolved"] == 1
    assert counts["written"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_run_no_image_is_counted_not_written(fakedb):
    conn = fakedb.Connection(_responder([_PPV_ROW]))
    counts = run(conn, apply=True, fetch_image=lambda slug: None)
    assert counts["try_ppv"] == 1
    assert counts["no_image"] == 1
    assert counts["resolved"] == 0
    assert counts["written"] == 0


def test_run_fetch_error_is_counted_not_fatal(fakedb):
    # A raising fetch on one event must not abort the batch; the next is processed.
    conn = fakedb.Connection(_responder([_PPV_ROW, _FN_ROW], update_rows=[(1,)]))
    calls = []

    def fetch(slug):
        calls.append(slug)
        if slug == "ufc-323":
            raise RuntimeError("boom")
        return _POSTER

    counts = run(conn, apply=True, fetch_image=fetch)
    assert calls == ["ufc-323", "ufc-fight-night-december-13-2025"]
    assert counts["errors"] == 1
    assert counts["written"] == 1
    assert conn.rollbacks >= 1  # the read-txn close + the failed-event cleanup


def test_run_apply_commits_even_on_zero_row_update(fakedb):
    # A 0-row UPDATE (first-writer-wins miss) must still COMMIT to close the txn.
    conn = fakedb.Connection(_responder([_PPV_ROW], update_rows=[]))
    counts = run(conn, apply=True, fetch_image=lambda slug: _POSTER)
    assert counts["resolved"] == 1
    assert counts["written"] == 0  # rowcount 0 -> not counted as written
    assert conn.commits == 1  # but the transaction WAS closed


def test_run_strategy_filter_only_fetches_selected(fakedb):
    conn = fakedb.Connection(_responder([_PPV_ROW, _FN_ROW]))
    called = []

    def fetch(slug):
        called.append(slug)
        return None

    counts = run(conn, apply=False, fetch_image=fetch, strategies=("ppv",))
    assert counts["try_ppv"] == 1
    assert counts["skip_filtered"] == 1
    assert called == ["ufc-323"]


def test_run_record_sink_captures_resolved_and_gap(fakedb):
    conn = fakedb.Connection(_responder([_PPV_ROW, _UNDEIVABLE_ROW]))
    sink: list = []
    run(conn, apply=False, fetch_image=lambda slug: _POSTER, record_sink=sink)
    assert len(sink) == 2
    ppv = next(r for r in sink if r["id"] == 304)
    assert ppv["resolved"] is True and ppv["strategy"] == "ppv" and ppv["slug"] == "ufc-323"
    undeivable = next(r for r in sink if r["id"] == 1084)
    assert undeivable["resolved"] is False and undeivable["strategy"] == "undeivable"
    assert undeivable["slug"] is None


def test_run_limit_caps_candidates_processed(fakedb):
    rows = [
        (304, "UFC 323: A", None, None, date(2025, 12, 6)),
        (305, "UFC 322: B", None, None, date(2025, 11, 15)),
        (306, "UFC 321: C", None, None, date(2025, 10, 25)),
    ]
    conn = fakedb.Connection(_responder(rows))
    called = []
    counts = run(conn, apply=False, fetch_image=lambda slug: called.append(slug) or None, limit=2)
    assert len(called) == 2
    assert counts["targets"] == 3


# ------------------------------------------------------------------ repository


def test_set_event_image_first_writer_wins(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert set_event_image(conn, 304, _POSTER) is True
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    assert "SET image_url = %s" in sql
    assert "WHERE id = %s AND (image_url IS NULL OR image_url = '')" in sql


def test_set_event_image_empty_is_noop(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert set_event_image(conn, 304, "") is False
    assert fakedb.mutating_statements(conn) == []
