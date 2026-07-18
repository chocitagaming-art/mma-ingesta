"""UFC face-off (careo) matching: feed parse, conservative match guard, run,
and the first-writer-wins repository write. No network, no DB (fakedb recorder).

The XML fixture mirrors the real UFC channel Atom feed (yt:videoId + title +
published), verified against channel UCvgfXK4nTYKudb0rFR6noLA on 2026-07-18.
"""

from datetime import date

from src.scrapers import match_faceoffs
from src.scrapers.match_faceoffs import (
    FeedVideo,
    TargetEvent,
    event_city,
    match_event,
    parse_feed,
)
from src.scrapers.repositories.events import set_event_faceoff_video

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <yt:videoId>Vb_0zQ-hIzM</yt:videoId>
    <title>UFC Oklahoma City: Fighter Face-offs</title>
    <published>2026-07-17T20:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>zTqNg1ECqs4</yt:videoId>
    <title>UFC Oklahoma City: Ceremonial Weigh-In</title>
    <published>2026-07-17T18:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>ppv330face1</yt:videoId>
    <title>UFC 330: Fighter Face-offs</title>
    <published>2026-08-14T20:00:00+00:00</published>
  </entry>
</feed>
"""


def _feed():
    return parse_feed(FEED_XML)


def _oklahoma_event():
    return TargetEvent(
        id=1061,
        name="UFC Fight Night: Du Plessis vs. Usman",
        location="Paycom Center, Oklahoma City, OK, United States",
        event_date=date(2026, 7, 18),
    )


# --------------------------------------------------------------------- parsing


def test_parse_feed_extracts_id_title_date():
    videos = _feed()
    assert len(videos) == 3
    assert videos[0] == FeedVideo("Vb_0zQ-hIzM", "UFC Oklahoma City: Fighter Face-offs", date(2026, 7, 17))


def test_parse_feed_bad_xml_returns_empty():
    assert parse_feed("<not-a-feed") == []


def test_event_city_second_field_then_first():
    assert event_city("Paycom Center, Oklahoma City, OK, United States") == "Oklahoma City"
    assert event_city("Etihad Arena, Abu Dhabi, United Arab Emirates") == "Abu Dhabi"
    assert event_city("Las Vegas") == "Las Vegas"
    assert event_city(None) is None


# ----------------------------------------------------------------------- match


def test_match_by_city_picks_faceoff_over_weighin():
    # Both the face-off and the ceremonial weigh-in share the city and window;
    # only the face-off passes the title whitelist.
    assert match_event(_oklahoma_event(), _feed()) == "Vb_0zQ-hIzM"


def test_match_rejects_when_only_weighin_present():
    weighin_only = [v for v in _feed() if "Weigh-In" in v.title]
    assert match_event(_oklahoma_event(), weighin_only) is None


def test_match_by_ufc_number_for_ppv():
    ppv = TargetEvent(
        id=1064,
        name="UFC 330: Makhachev vs. Machado Garry",
        location="Xfinity Mobile Arena, Philadelphia, PA, United States",
        event_date=date(2026, 8, 15),
    )
    # City (Philadelphia) is NOT in the title; the 'UFC 330' token carries it.
    assert match_event(ppv, _feed()) == "ppv330face1"


def test_match_rejects_out_of_date_window():
    stale = TargetEvent(1061, _oklahoma_event().name, _oklahoma_event().location, date(2026, 1, 1))
    assert match_event(stale, _feed()) is None


def test_match_rejects_wrong_city_and_no_number():
    other = TargetEvent(9, "UFC Fight Night: Someone vs. Other", "Arena, Las Vegas, NV, USA", date(2026, 7, 18))
    assert match_event(other, _feed()) is None


def test_match_none_when_event_date_missing():
    undated = TargetEvent(9, "UFC Fight Night: X vs. Y", "Arena, Oklahoma City, OK", None)
    assert match_event(undated, _feed()) is None


# ------------------------------------------------------------------------- run


def _responder(update_result=None):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return [(
                1061,
                "UFC Fight Night: Du Plessis vs. Usman",
                "Paycom Center, Oklahoma City, OK, United States",
                date(2026, 7, 18),
            )]
        if "UPDATE" in upper:
            return update_result or []
        return []

    return responder


def test_run_matches_and_writes(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = match_faceoffs.run(conn, apply=True, feed=_feed())
    assert counts["matched"] == 1
    assert counts["written"] == 1
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 1
    assert "faceoff_video_id IS NULL" in updates[0]
    assert conn.commits == 1


def test_run_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = match_faceoffs.run(conn, apply=False, feed=_feed())
    assert counts["matched"] == 1
    assert counts["written"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


# ------------------------------------------------------------------ repository


def test_set_event_faceoff_video_first_writer_wins(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    set_event_faceoff_video(conn, 1061, "Vb_0zQ-hIzM")
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    assert "SET faceoff_video_id = %s" in sql
    assert "WHERE id = %s AND faceoff_video_id IS NULL" in sql


def test_set_event_faceoff_video_empty_is_noop(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert set_event_faceoff_video(conn, 1061, "") is False
    assert fakedb.mutating_statements(conn) == []
