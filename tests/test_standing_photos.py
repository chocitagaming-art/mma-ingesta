"""Round A (standing full-body photo) parsing + repository + backfill tests.

The HTML fixture mirrors a real ufc.com event bout view (verified on
/event/ufc-328): each div.c-listing-fight carries one standing full-body photo
per corner in .c-listing-fight__corner-image--{red,blue} img[src] (Drupal style
event_fight_card_upper_body_of_standing_athlete, host served WITHOUT www), with
the athlete's name as alt. No network, no DB: the backfill gets an injected
fetch() and writes go through the shared fakedb recorder.
"""

from datetime import date

from bs4 import BeautifulSoup

from src.scrapers import backfill_standing_photos
from src.scrapers.backfill_standing_photos import guess_ufc_slug
from src.scrapers.repositories.fighters import update_fighter_standing_photo
from src.scrapers.upcoming_events import ParsedBout, _parse_bouts

# ------------------------------------------------------------------- fixtures

STANDING_STYLE = "event_fight_card_upper_body_of_standing_athlete"

# Bout 1: both corners with a standing photo. Red arrives with the ufc.com
# host (no www, as really served); blue arrives as a relative path.
# Bout 2: no corner images at all (far-out card / debut fighters).
# Bout 3: red serves a silhouette placeholder, blue a non-standing style.
EVENT_HTML = f"""
<html><body>
<div class="c-listing-fight" data-fmid="12722">
  <div class="c-listing-fight__corner--red">
    <div class="c-listing-fight__corner-image--red">
      <a href="https://www.ufc.com/athlete/khamzat-chimaev">
        <div class="layout"><div class="layout__region">
          <img src="https://ufc.com/images/styles/{STANDING_STYLE}/s3/2025-05/CHIMAEV_KHAMZAT_L_10-26.png?itok=iv5"
               width="185" height="677" alt="Khamzat Chimaev" loading="lazy">
        </div></div>
      </a>
    </div>
    <div class="c-listing-fight__corner-name--red">Khamzat Chimaev</div>
  </div>
  <div class="c-listing-fight__corner--blue">
    <div class="c-listing-fight__corner-image--blue">
      <img src="/images/styles/{STANDING_STYLE}/s3/2026-05/STRICKLAND_SEAN_R_05-09.png?itok=yP"
           alt="Sean Strickland">
    </div>
    <div class="c-listing-fight__corner-name--blue">Sean Strickland</div>
  </div>
  <div class="c-listing-fight__class-text">Middleweight Title Bout</div>
</div>
<div class="c-listing-fight" data-fmid="12774">
  <div class="c-listing-fight__corner-name--red">New Comer</div>
  <div class="c-listing-fight__corner-name--blue">Other Debut</div>
  <div class="c-listing-fight__class-text">Flyweight Bout</div>
</div>
<div class="c-listing-fight" data-fmid="12775">
  <div class="c-listing-fight__corner-image--red">
    <img src="https://ufc.com/images/styles/{STANDING_STYLE}/s3/silhouette-full-body.png"
         alt="No photo">
  </div>
  <div class="c-listing-fight__corner-name--red">Silhouette Guy</div>
  <div class="c-listing-fight__corner-image--blue">
    <img src="https://ufc.com/images/styles/event_results_athlete_headshot/s3/2026-05/DOE_JOHN_HS.png"
         alt="John Doe">
  </div>
  <div class="c-listing-fight__corner-name--blue">John Doe</div>
  <div class="c-listing-fight__class-text">Lightweight Bout</div>
</div>
</body></html>
"""

RED_URL = (
    f"https://www.ufc.com/images/styles/{STANDING_STYLE}/s3/2025-05/"
    "CHIMAEV_KHAMZAT_L_10-26.png?itok=iv5"
)
BLUE_URL = (
    f"https://www.ufc.com/images/styles/{STANDING_STYLE}/s3/2026-05/"
    "STRICKLAND_SEAN_R_05-09.png?itok=yP"
)


def _soup() -> BeautifulSoup:
    return BeautifulSoup(EVENT_HTML, "lxml")


# ------------------------------------------------------- extraction + normalize


def test_parse_bouts_extracts_and_normalizes_standing_images():
    bouts = _parse_bouts(_soup(), "ufc-328")
    assert len(bouts) == 3
    bout = bouts[0]
    # Host without www -> https://www.ufc.com; relative path -> absolute.
    assert bout.red_image_url == RED_URL
    assert bout.blue_image_url == BLUE_URL
    # The rest of the bout parsing is untouched.
    assert (bout.red_name, bout.blue_name) == ("Khamzat Chimaev", "Sean Strickland")
    assert bout.fmid == "12722"


def test_parse_bouts_without_corner_images_yields_none():
    bout = _parse_bouts(_soup(), "ufc-328")[1]
    assert bout.red_image_url is None
    assert bout.blue_image_url is None
    assert (bout.red_name, bout.blue_name) == ("New Comer", "Other Debut")


def test_parse_bouts_rejects_placeholder_and_non_standing_styles():
    bout = _parse_bouts(_soup(), "ufc-328")[2]
    # Silhouette placeholder rejected even under the standing style path.
    assert bout.red_image_url is None
    # A different Drupal style (headshot) is never captured as standing photo.
    assert bout.blue_image_url is None


def test_parsed_bout_image_fields_are_optional_for_old_constructions():
    bout = ParsedBout(
        card_segment=None, bout_order=1, weight_class=None,
        scheduled_rounds=3, red_name="A", blue_name="B", fmid="x",
    )
    assert bout.red_image_url is None
    assert bout.blue_image_url is None


# ------------------------------------------- repository: new value wins, no NULL


def test_update_standing_photo_new_value_wins(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert update_fighter_standing_photo(conn, 7, RED_URL) is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # Plain SET (no COALESCE keeping the old value): the newest scrape WINS.
    assert "SET standing_body_url = %s" in flat
    assert "COALESCE" not in flat
    # Identical values skip the row churn.
    assert "standing_body_url IS DISTINCT FROM %s" in flat
    assert params == (RED_URL, 7, RED_URL)


def test_update_standing_photo_null_or_empty_never_wipes(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert update_fighter_standing_photo(conn, 7, None) is False
    assert update_fighter_standing_photo(conn, 7, "") is False
    # Guarded in Python: no SQL at all, so a stored URL can never be wiped.
    assert fakedb.mutating_statements(conn) == []


def test_update_standing_photo_same_value_reports_no_update(fakedb):
    # rowcount 0 simulates IS DISTINCT FROM matching no row (value unchanged).
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert update_fighter_standing_photo(conn, 7, RED_URL) is False


# ------------------------------------------------------------------- backfill


_EVENT_ROWS = [(10, "ufc-328")]
# get_all_fighters row shape: (id, name, nickname, nationality, birth_date,
# height_cm, reach_cm, weight_grams, stance).
_FIGHTER_ROWS = [
    (1, "Khamzat Chimaev", None, None, None, None, None, None, None),
    (2, "Sean Strickland", None, None, None, None, None, None, None),
]


def _responder(
    fight_rows,
    update_result=((1,),),
    fighter_rows=_FIGHTER_ROWS,
    event_rows=_EVENT_ROWS,
    deduced_rows=(),
    claim_result=((1,),),
):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT") and "FROM events" in flat:
            # The lookback query (round B) filters on status='completed'; the
            # source='ufc.com' target query carries no status filter.
            if "status = 'completed'" in flat:
                return list(deduced_rows)
            return list(event_rows)
        if flat.startswith("SELECT") and "FROM fighters" in flat:
            return fighter_rows
        if flat.startswith("SELECT") and "FROM fights" in flat:
            return fight_rows
        if "UPDATE fighters" in flat:
            return list(update_result)
        if "UPDATE events" in flat:
            return list(claim_result)
        return []

    return responder


def _fetch(url: str) -> BeautifulSoup:
    assert url == "https://www.ufc.com/event/ufc-328"
    return _soup()


def _standing_updates(fakedb, conn):
    return [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if "UPDATE fighters" in sql
    ]


def test_backfill_updates_both_corners_via_fights_source_id(fakedb):
    conn = fakedb.Connection(_responder(fight_rows=[("12722", 1, 2)]))
    counts = backfill_standing_photos.backfill(conn, _fetch)
    assert counts["events"] == 1
    assert counts["images_found"] == 2
    assert counts["matched"] == 2
    assert counts["updated"] == 2
    assert counts["unmatched"] == 0
    updates = _standing_updates(fakedb, conn)
    assert [(p[0], p[1]) for _sql, p in updates] == [(RED_URL, 1), (BLUE_URL, 2)]
    assert conn.commits == 1  # one commit per event


def test_backfill_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder(fight_rows=[("12722", 1, 2)]))
    counts = backfill_standing_photos.backfill(conn, _fetch, dry_run=True)
    assert counts["images_found"] == 2
    assert counts["matched"] == 2
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    assert conn.rollbacks == 1  # read-only snapshot released


def test_backfill_falls_back_to_name_matcher_when_corner_id_missing(fakedb):
    # The fights row exists but both corner ids are NULL (unmatched at scrape
    # time): the central matcher resolves them by the parsed corner names.
    conn = fakedb.Connection(_responder(fight_rows=[("12722", None, None)]))
    counts = backfill_standing_photos.backfill(conn, _fetch)
    assert counts["matched"] == 2
    updates = _standing_updates(fakedb, conn)
    assert [p[1] for _sql, p in updates] == [1, 2]


def test_backfill_counts_unmatched_corner_without_writing(fakedb):
    # No fights row for the fmid and the blue name is unknown to the matcher.
    fighter_rows = [(1, "Khamzat Chimaev", None, None, None, None, None, None, None)]
    conn = fakedb.Connection(_responder(fight_rows=[], fighter_rows=fighter_rows))
    counts = backfill_standing_photos.backfill(conn, _fetch)
    assert counts["matched"] == 1
    assert counts["unmatched"] == 1
    updates = _standing_updates(fakedb, conn)
    assert [p[1] for _sql, p in updates] == [1]


def test_backfill_counts_fetch_errors_and_continues(fakedb):
    conn = fakedb.Connection(_responder(fight_rows=[("12722", 1, 2)]))

    def boom(url: str) -> BeautifulSoup:
        raise RuntimeError("HTTP 500")

    counts = backfill_standing_photos.backfill(conn, boom)
    assert counts["fetch_errors"] == 1
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_backfill_targets_ufc_events_newest_first_with_limit(fakedb):
    conn = fakedb.Connection(_responder(fight_rows=[]))
    events = backfill_standing_photos._get_target_events(conn, limit=5)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "WHERE source = %s" in flat
    # No status filter: completed ufc.com events keep live pages (/event/ufc-328).
    assert "status" not in flat
    assert "ORDER BY event_date DESC NULLS LAST" in flat
    assert flat.endswith("LIMIT %s")
    assert params == ("ufc.com", 5)
    assert events == [(10, "ufc-328")]


# --------------------------------------------------------------- slug deduction


def test_guess_ufc_slug_numbered_card():
    slugs = guess_ufc_slug("UFC 328: Chimaev vs Strickland", date(2026, 5, 9))
    assert slugs == ["ufc-328"]


def test_guess_ufc_slug_fight_night_uses_english_month_and_unpadded_day():
    # The real ufc.com format (HEAD /event/ufc-fight-night-april-25-2026 -> 200).
    slugs = guess_ufc_slug("UFC Fight Night: Sterling vs. Zalal", date(2026, 4, 25))
    assert slugs == ["ufc-fight-night-april-25-2026"]


def test_guess_ufc_slug_short_day_adds_zero_padded_second_candidate():
    slugs = guess_ufc_slug("UFC Fight Night: A vs B", date(2026, 4, 5))
    assert slugs == [
        "ufc-fight-night-april-5-2026",
        "ufc-fight-night-april-05-2026",
    ]


def test_guess_ufc_slug_other_names_fall_back_to_fight_night_pattern():
    assert guess_ufc_slug("UFC on ABC: X vs Y", date(2026, 3, 14)) == [
        "ufc-fight-night-march-14-2026"
    ]
    # "The Ultimate Fighter 34 Finale" is not "UFC <n>": date fallback too.
    assert guess_ufc_slug("The Ultimate Fighter 34 Finale", date(2026, 8, 1)) == [
        "ufc-fight-night-august-1-2026",
        "ufc-fight-night-august-01-2026",
    ]


def test_guess_ufc_slug_without_date_returns_no_candidates():
    assert guess_ufc_slug("UFC Fight Night: X vs Y", None) == []


# ----------------------------------------------- backfill via deduced slug (B)


# ufcstats-imported completed event: no source, no slug. Fight rows carry no
# ufc.com fmid either, so corners resolve through the central name matcher.
_DEDUCED_ROWS = [(20, "UFC Fight Night: Sterling vs. Zalal", date(2026, 4, 25))]
_DEDUCED_URL = "https://www.ufc.com/event/ufc-fight-night-april-25-2026"


def _fetch_deduced(url: str) -> BeautifulSoup:
    assert url == _DEDUCED_URL
    return _soup()


def _event_claims(conn):
    return [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if "UPDATE events" in sql
    ]


def test_backfill_resolves_completed_event_without_source_via_deduced_slug(fakedb):
    conn = fakedb.Connection(
        _responder(fight_rows=[], event_rows=[], deduced_rows=_DEDUCED_ROWS)
    )
    counts = backfill_standing_photos.backfill(conn, _fetch_deduced)
    assert counts["events"] == 1
    assert counts["deduced_resolved"] == 1
    assert counts["images_found"] == 2
    assert counts["matched"] == 2  # via the name matcher: no ufc.com fmids here
    assert counts["updated"] == 2
    # The event row is claimed so future sweeps target it directly...
    assert counts["source_claimed"] == 1
    claims = _event_claims(conn)
    assert len(claims) == 1
    flat, params = claims[0]
    assert "SET source = %s, source_id = %s" in flat
    # ...but ONLY when both fields are still NULL (never clobbers a linkage),
    # and never colliding with a ufc.com twin on the (source, source_id) index.
    assert "source IS NULL AND source_id IS NULL" in flat
    assert "NOT EXISTS" in flat
    assert params[:3] == ("ufc.com", "ufc-fight-night-april-25-2026", 20)


def test_backfill_deduced_lookback_query_shape(fakedb):
    conn = fakedb.Connection(_responder(fight_rows=[], deduced_rows=_DEDUCED_ROWS))
    rows = backfill_standing_photos._get_recent_events_without_source(conn, 120)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "status = 'completed'" in flat
    assert "(source IS NULL OR source <> %s)" in flat
    assert "event_date >= CURRENT_DATE - %s" in flat
    assert "ORDER BY event_date DESC" in flat
    assert params == ("ufc.com", 120)
    assert rows == [(20, "UFC Fight Night: Sterling vs. Zalal", date(2026, 4, 25))]


def test_backfill_deduced_tries_next_candidate_after_miss(fakedb):
    rows = [(21, "UFC Fight Night: A vs B", date(2026, 4, 5))]
    conn = fakedb.Connection(_responder(fight_rows=[], event_rows=[], deduced_rows=rows))
    calls = []

    def fetch(url: str) -> BeautifulSoup:
        calls.append(url)
        if url.endswith("ufc-fight-night-april-5-2026"):
            raise RuntimeError("HTTP 404")  # miss: probe the padded variant
        return _soup()

    counts = backfill_standing_photos.backfill(conn, fetch)
    assert calls == [
        "https://www.ufc.com/event/ufc-fight-night-april-5-2026",
        "https://www.ufc.com/event/ufc-fight-night-april-05-2026",
    ]
    assert counts["deduced_resolved"] == 1
    assert counts["fetch_errors"] == 0  # candidate misses are not fetch errors
    assert counts["updated"] == 2
    assert [p[1] for _flat, p in _event_claims(conn)] == ["ufc-fight-night-april-05-2026"]


def test_backfill_deduced_treats_boutless_page_as_unresolved(fakedb):
    # Unknown slugs do NOT 404: ufc.com 302-redirects them to /search (200),
    # whose page parses to zero bouts -> the candidate must count as a miss.
    conn = fakedb.Connection(
        _responder(fight_rows=[], event_rows=[], deduced_rows=_DEDUCED_ROWS)
    )

    def fetch(url: str) -> BeautifulSoup:
        return BeautifulSoup("<html><body>search results</body></html>", "lxml")

    counts = backfill_standing_photos.backfill(conn, fetch)
    assert counts["deduced_unresolved"] == 1
    assert counts["events"] == 0
    assert counts["source_claimed"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_backfill_deduced_never_overwrites_existing_source(fakedb):
    # claim_result=() -> rowcount 0: the guarded UPDATE matched no row because
    # the event already carried a non-NULL source (e.g. another provider).
    # Photos still flow; the (source, source_id) linkage is left untouched.
    conn = fakedb.Connection(
        _responder(
            fight_rows=[], event_rows=[], deduced_rows=_DEDUCED_ROWS, claim_result=()
        )
    )
    counts = backfill_standing_photos.backfill(conn, _fetch_deduced)
    assert counts["updated"] == 2
    assert counts["source_claimed"] == 0
    (flat, _params), = _event_claims(conn)
    assert "source IS NULL AND source_id IS NULL" in flat  # SQL-level guard


def test_backfill_deduced_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(
        _responder(fight_rows=[], event_rows=[], deduced_rows=_DEDUCED_ROWS)
    )
    counts = backfill_standing_photos.backfill(conn, _fetch_deduced, dry_run=True)
    assert counts["matched"] == 2
    assert counts["updated"] == 0
    assert counts["source_claimed"] == 0
    assert fakedb.mutating_statements(conn) == []  # no photo writes, no claim
    assert conn.commits == 0
    assert conn.rollbacks == 1
