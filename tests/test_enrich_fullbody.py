"""Phase 2 (full-body/leg-reach/gym) parsing + backfill tests.

The HTML fixtures mirror a real ufc.com/athlete page: the full-body photo lives
in div.hero-profile__image-wrap > img.hero-profile__image[src] (URL NOT
constructible a priori) and the bio block exposes 'Leg reach' / 'Trains at' in
div.c-bio__field ('Age' comes nested in div.field--name-age and is ignored).
No network, no DB: resolve_athlete gets a fake session and writes go through the
shared fakedb recorder.
"""

from datetime import date, datetime, timezone
from pathlib import Path

from src.scrapers import enrich_fullbody
from src.scrapers.enrich_photos_ufc import (
    AthleteData,
    _extract_bio_fields,
    _extract_full_body,
    _extract_headshot,
    _is_placeholder_image,
    _parse_ufc_date,
    resolve_athlete,
)
from src.scrapers.repositories.fighters import update_fighter_enrichment

# ------------------------------------------------------------------- fixtures

_BIO_BLOCK = """
<div class="c-bio__field">
  <div class="c-bio__label">Height</div>
  <div class="c-bio__text">67.00</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Reach</div>
  <div class="c-bio__text">70.50</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Leg reach</div>
  <div class="c-bio__text">41.50</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Trains at</div>
  <div class="c-bio__text">Millennia MMA, Rancho, CA</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Age</div>
  <div class="c-bio__text"><div class="field field--name-age">32</div></div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Place of Birth</div>
  <div class="c-bio__text">Chone, Ecuador</div>
</div>
<div class="c-bio__field">
  <div class="c-bio__label">Octagon Debut</div>
  <div class="c-bio__text">Nov. 15, 2014</div>
</div>
"""

# Representative athlete page: hero full-body image (relative src, as served) +
# a headshot-style image elsewhere (the current headshot pipeline's pick).
ATHLETE_HTML = f"""
<html><head><title>Marlon Vera | UFC</title></head><body>
<div class="hero-profile">
  <div class="hero-profile__image-wrap">
    <img class="hero-profile__image"
         src="/images/styles/athlete_bio_full_body/s3/2025-04/VERA_MARLON_L_04-12.png?itok=abc123"
         alt="Marlon Vera">
  </div>
  <h1 class="hero-profile__name">Marlon  Vera</h1>
  <p class="hero-profile__division-body">23-9-1 (W-L-D)</p>
</div>
<img src="/images/styles/event_results_athlete_headshot/s3/2025-04/VERA_MARLON_L_04-12_HS.png?itok=hs1">
{_BIO_BLOCK}
</body></html>
"""

# Variant: UFC serves a generic silhouette when the fighter has no real photo.
PLACEHOLDER_HTML = f"""
<html><body>
<div class="hero-profile__image-wrap">
  <img class="hero-profile__image"
       src="/images/styles/athlete_bio_full_body/s3/silhouette-full-body.png"
       alt="No photo">
</div>
{_BIO_BLOCK}
</body></html>
"""


class _FakeResponse:
    # `url` is the URL AFTER redirects, which is how resolve_athlete spots an
    # unknown slug: ufc.com answers those with 200 on /search instead of a 404.
    # It defaults to the requested URL so every existing test keeps meaning
    # "the page we asked for is the page we got".
    # `headers` only exists when a test passes it: without one the double has
    # no attribute at all, like the doubles of the other suites, and
    # resolve_athlete must cope (it then dates the page by today, UTC).
    def __init__(
        self,
        text: str,
        status_code: int = 200,
        url: str = "https://www.ufc.com/athlete/x",
        headers: dict[str, str] | None = None,
    ):
        self.text = text
        self.status_code = status_code
        self.ok = status_code < 400
        self.url = url
        if headers is not None:
            self.headers = headers


class _FakeSession:
    def __init__(
        self,
        html: str,
        final_url: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self._html = html
        self._final_url = final_url
        self._headers = headers
        self.requested: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        final_url = self._final_url or url
        return _FakeResponse(self._html, url=final_url, headers=self._headers)


# ------------------------------------------------------------ full-body photo


def test_full_body_url_extracted_and_normalized_from_relative_src():
    url = _extract_full_body(ATHLETE_HTML, "Marlon Vera")
    assert url == (
        "https://www.ufc.com/images/styles/athlete_bio_full_body/s3/2025-04/"
        "VERA_MARLON_L_04-12.png?itok=abc123"
    )


def test_full_body_url_normalizes_host_without_www():
    html = ATHLETE_HTML.replace(
        'src="/images/styles/athlete_bio_full_body',
        'src="https://ufc.com/images/styles/athlete_bio_full_body',
    )
    url = _extract_full_body(html, "Marlon Vera")
    assert url is not None
    assert url.startswith("https://www.ufc.com/images/styles/athlete_bio_full_body/")


def test_full_body_url_normalizes_protocol_relative_src():
    html = ATHLETE_HTML.replace(
        'src="/images/styles/athlete_bio_full_body',
        'src="//www.ufc.com/images/styles/athlete_bio_full_body',
    )
    url = _extract_full_body(html, "Marlon Vera")
    assert url is not None
    assert url.startswith("https://www.ufc.com/images/styles/athlete_bio_full_body/")


def test_full_body_placeholder_silhouette_rejected():
    assert _extract_full_body(PLACEHOLDER_HTML, "Marlon Vera") is None


# UFC also serves generic "shadow / full-length" silhouettes whose filenames
# carry NONE of the old placeholder tokens (no "silhouette"/"nophoto"), so the
# original guard stored them and heroes rendered a black silhouette.
SHADOW_PLACEHOLDER_HTML = f"""
<html><body>
<div class="hero-profile__image-wrap">
  <img class="hero-profile__image"
       src="/images/styles/athlete_bio_full_body/s3/image/fighter_images/SHADOW_Fighter_fullLength_RED.png?itok=x">
</div>
{_BIO_BLOCK}
</body></html>
"""

SHADOW_WOMAN_PLACEHOLDER_HTML = SHADOW_PLACEHOLDER_HTML.replace(
    "SHADOW_Fighter_fullLength_RED.png",
    "Fighter_fullLength_Shadow-woman-blue.png",
)


def test_full_body_shadow_fulllength_placeholder_rejected():
    # Both real-world variants that slipped past the old guard must be rejected.
    assert _extract_full_body(SHADOW_PLACEHOLDER_HTML, "Marlon Vera") is None
    assert _extract_full_body(SHADOW_WOMAN_PLACEHOLDER_HTML, "Marlon Vera") is None


def test_is_placeholder_image_matches_shadow_variants():
    base = "https://www.ufc.com/images/styles/athlete_bio_full_body/s3/image/fighter_images/"
    assert _is_placeholder_image(base + "SHADOW_Fighter_fullLength_RED.png")
    assert _is_placeholder_image(base + "Fighter_fullLength_Shadow-woman-blue.png")
    # A real, named athlete photo is NOT a placeholder.
    assert not _is_placeholder_image(
        "https://www.ufc.com/images/styles/athlete_bio_full_body/s3/2025-04/VERA_MARLON_L_04-12.png"
    )


def test_full_body_falls_back_to_named_candidate_when_hero_missing():
    # No hero <img>, but the page still embeds the athlete's own bio_full_body
    # URL elsewhere; an opponent's file must NOT be picked.
    html = """
    <img src="/images/styles/athlete_bio_full_body/s3/2025-01/SMITH_OTHER_L.png">
    <img src="/images/styles/athlete_bio_full_body/s3/2025-01/VERA_MARLON_R.png">
    """
    url = _extract_full_body(html, "Marlon Vera")
    assert url == "https://www.ufc.com/images/styles/athlete_bio_full_body/s3/2025-01/VERA_MARLON_R.png"


# ------------------------------------------------------------------ bio fields


def test_leg_reach_inches_converted_to_cm():
    session = _FakeSession(ATHLETE_HTML)
    data = resolve_athlete(session, "Marlon Vera")
    assert data is not None
    assert data.leg_reach_cm == 105.4  # 41.50 in * 2.54


def test_trains_at_extracted():
    fields = _extract_bio_fields(ATHLETE_HTML)
    assert fields["trains at"] == "Millennia MMA, Rancho, CA"


def test_nested_age_field_does_not_pollute_bio_parsing():
    # 'Age' is nested inside div.field--name-age (no flat text); it must not
    # steal a neighbouring field's value.
    fields = _extract_bio_fields(ATHLETE_HTML)
    assert fields["leg reach"] == "41.50"
    assert fields.get("age", "") == ""


def test_resolve_athlete_fills_new_fields_and_keeps_headshot():
    session = _FakeSession(ATHLETE_HTML)
    data = resolve_athlete(session, "Marlon Vera")
    assert data is not None
    # New Phase-2 fields.
    assert data.full_body_url and "athlete_bio_full_body" in data.full_body_url
    assert data.leg_reach_cm == 105.4
    assert data.trains_at == "Millennia MMA, Rancho, CA"
    # Hero name captured (inner whitespace collapsed) for the anti-homonym guard.
    assert data.page_name == "Marlon Vera"
    # Existing pipeline untouched: headshot + record + measures still resolve.
    assert data.headshot_url == (
        "https://www.ufc.com/images/styles/event_results_athlete_headshot/s3/2025-04/"
        "VERA_MARLON_L_04-12_HS.png?itok=hs1"
    )
    assert (data.wins, data.losses, data.draws) == (23, 9, 1)
    assert data.reach_cm == 179.1  # 70.50 in
    assert data.nationality == "Ecuador"


def test_headshot_extraction_unchanged_by_new_parsing():
    url = _extract_headshot(ATHLETE_HTML, "Marlon Vera")
    assert url is not None
    assert "event_results_athlete_headshot" in url


# -------------------------------------------- extended bio (Fase 4 / BE5)


def test_birth_place_and_octagon_debut_extracted():
    session = _FakeSession(ATHLETE_HTML)
    data = resolve_athlete(session, "Marlon Vera")
    assert data is not None
    # Raw place kept whole; nationality still derives the country from it.
    assert data.birth_place == "Chone, Ecuador"
    assert data.nationality == "Ecuador"
    assert data.octagon_debut == date(2014, 11, 15)


def test_parse_ufc_date_tolerates_format_variants():
    # Real ufc.com format: abbreviated month with a trailing period.
    assert _parse_ufc_date("Apr. 25, 2015") == date(2015, 4, 25)
    # Tolerated drift: no period, full month name, stray whitespace.
    assert _parse_ufc_date("Apr 25, 2015") == date(2015, 4, 25)
    assert _parse_ufc_date("April 25, 2015") == date(2015, 4, 25)
    assert _parse_ufc_date("  Jun. 5,   2021 ") == date(2021, 6, 5)
    # Absent/garbage never raises, never yields a bogus date.
    assert _parse_ufc_date(None) is None
    assert _parse_ufc_date("") is None
    assert _parse_ufc_date("TBD") is None
    assert _parse_ufc_date("25/04/2015") is None


# ------------------------------------------- the page's own date is no debut
#
# 🪤 When a fighter has no debut, ufc.com paints the day the page was GENERATED
# in "Octagon Debut", and we stored it: 51 fighters ended up with one (45 with
# 2026-07-10, 6 loose ones). The two fixtures are real bio blocks served in the
# same minute on 20-oct-2026: Roque Conceição, no UFC bout, reading
# "Oct. 20, 2026"; Marlon Vera, debut at UFC 180 on 2014-11-15.

_FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_DEBUT_HTML = (
    _FIXTURES / "ufc_athlete_empty_debut_roque_conceicao.html"
).read_text(encoding="utf-8")
REAL_DEBUT_HTML = (
    _FIXTURES / "ufc_athlete_real_debut_marlon_vera.html"
).read_text(encoding="utf-8")
# The Date header of each one, as ufc.com sent it.
SERVED_ROQUE = {"Date": "Tue, 20 Oct 2026 22:39:12 GMT"}
SERVED_VERA = {"Date": "Tue, 20 Oct 2026 22:39:34 GMT"}


def _page_dated(painted: str) -> str:
    """The real Roque Conceição block, painting `painted` instead."""
    return EMPTY_DEBUT_HTML.replace("Oct. 20, 2026", painted)


def _debut(html: str, headers: dict[str, str] | None, name: str = "Roque Conceicao"):
    data = resolve_athlete(_FakeSession(html, headers=headers), name)
    assert data is not None
    return data.octagon_debut


def test_real_empty_debut_page_does_not_hand_its_own_date_over_as_a_debut():
    # The bio really reads the serving day: the bug is in the page, not in us.
    assert _extract_bio_fields(EMPTY_DEBUT_HTML)["octagon debut"] == "Oct. 20, 2026"
    assert _debut(EMPTY_DEBUT_HTML, SERVED_ROQUE) is None


def test_real_old_debut_served_the_same_minute_is_kept():
    assert _debut(REAL_DEBUT_HTML, SERVED_VERA, "Marlon Vera") == date(2014, 11, 15)


def test_page_date_out_of_a_week_old_cache_is_dropped_and_the_window_is_8_days():
    # How 6806 Roque Conceição got 2026-10-12: a page generated that day, still
    # in ufc.com's week-long cache when the 18-oct pass read it.
    served_18 = {"Date": "Sun, 18 Oct 2026 12:59:00 GMT"}
    assert _debut(_page_dated("Oct. 12, 2026"), served_18) is None
    # The edge, pinned: served on 20-oct, 8 days back (12-oct) is still inside
    # the window; 9 days back (11-oct) is outside and stays a debut.
    assert _debut(_page_dated("Oct. 12, 2026"), SERVED_ROQUE) is None
    assert _debut(_page_dated("Oct. 11, 2026"), SERVED_ROQUE) == date(2026, 10, 11)


def test_the_window_counts_from_the_date_header_not_from_the_day_of_the_run():
    # The 45 rows with 2026-07-10 were written on 11-jul-2026 (updated_at from
    # 14:39 GMT). Read today, that page date is months old and would pass for a
    # debut: only the Date the page was served with tells it apart.
    served_11_jul = {"Date": "Sat, 11 Jul 2026 14:39:47 GMT"}
    assert _debut(_page_dated("Jul. 10, 2026"), served_11_jul) is None


def test_debut_after_the_serving_day_is_dropped():
    # A booked debutant's page can show the bout he has not fought yet (on
    # 9-oct-2026 6782 Cody Chovancek read "Oct. 17, 2026"), and a booking can
    # move: 9108 Jessie Rosas still holds 2026-08-08, but debuted on 12-sep.
    served_9 = {"Date": "Fri, 09 Oct 2026 05:00:00 GMT"}
    assert _debut(_page_dated("Oct. 17, 2026"), served_9, "Cody Chovancek") is None


def test_without_a_readable_date_header_the_window_counts_from_today_utc():
    # No Date (or an unreadable one): the page is dated by today, UTC. It is
    # painted with today's date, as the bug does, so this holds on any day.
    today = datetime.now(timezone.utc).date()
    html = _page_dated(f"{today:%b}. {today.day}, {today.year}")
    for headers in (None, {}, {"Date": "not a date"}):
        assert _debut(html, headers) is None, headers


def _fake_network(monkeypatch, html: str, headers: dict[str, str]) -> None:
    session = _FakeSession(html, headers=headers)
    monkeypatch.setattr(enrich_fullbody.requests, "Session", lambda: session)


def test_backfill_never_writes_the_page_date_through_the_default_resolver(
    fakedb, monkeypatch
):
    # enrich_fullbody reuses resolve_athlete, so it is covered with no code of
    # its own. Run with the DEFAULT resolver, only the network faked.
    # 1) A page with a real photo AND the page date: the photo is written, the
    #    date never reaches the UPDATE.
    page = ATHLETE_HTML.replace("Nov. 15, 2014", "Oct. 20, 2026")
    _fake_network(monkeypatch, page, SERVED_ROQUE)
    rows = [(6414, "Marlon Vera", None, True)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    counts = enrich_fullbody.backfill(conn, dry_run=False, sleeper=lambda _s: None)
    assert counts["updated"] == 1
    executed = [step for cur in conn.cursors for step in cur.executed]
    params = [p for sql, p in executed if "UPDATE" in sql.upper()]
    assert len(params) == 1
    assert date(2026, 10, 20) not in params[0]
    # 2) The real Roque Conceição block, whose only "news" is the page date:
    #    nothing is left to write, so no UPDATE at all.
    _fake_network(monkeypatch, EMPTY_DEBUT_HTML, SERVED_ROQUE)
    rows = [(6806, "Roque Conceição", None, True)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    counts = enrich_fullbody.backfill(conn, dry_run=False, sleeper=lambda _s: None)
    assert counts["resolved"] == 1
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_update_fighter_enrichment_guards_bio_extras_with_coalesce(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fighter_enrichment(
        conn, 7, birth_place="Chone, Ecuador", octagon_debut=date(2014, 11, 15)
    )
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    # Same additive policy as every other bio column: fill only when empty.
    assert "birth_place = COALESCE(NULLIF(birth_place, ''), %s)" in sql
    assert "octagon_debut = COALESCE(octagon_debut, %s)" in sql
    assert "(NULLIF(birth_place, '') IS NULL AND %s IS NOT NULL)" in sql
    assert "(octagon_debut IS NULL AND %s IS NOT NULL)" in sql


def test_backfill_persists_birth_place_and_debut(fakedb):
    # A page with ONLY the two Fase-4 fields still counts as new data and is
    # written through the same COALESCE-guarded update.
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    bio_only = lambda _s, name: AthleteData(  # noqa: E731
        birth_place="Chone, Ecuador",
        octagon_debut=date(2014, 11, 15),
        page_name=name,
    )
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=bio_only, sleeper=lambda _s: None
    )
    assert counts["updated"] == 2
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 2
    assert all("birth_place" in sql and "octagon_debut" in sql for sql in updates)


# ------------------------------------------------- repository: never NULL over data


def test_update_fighter_enrichment_guards_new_columns_with_coalesce(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fighter_enrichment(
        conn, 7, full_body_url="https://www.ufc.com/x.png", leg_reach_cm=105.4, trains_at="Gym"
    )
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    # SET only fills empty columns: a NULL argument can never replace a value.
    assert "full_body_url = COALESCE(NULLIF(full_body_url, ''), %s)" in sql
    assert "leg_reach_cm = COALESCE(leg_reach_cm, %s)" in sql
    assert "trains_at = COALESCE(NULLIF(trains_at, ''), %s)" in sql
    # The row only matches when the column is empty AND the new value is NOT NULL.
    assert "(NULLIF(full_body_url, '') IS NULL AND %s IS NOT NULL)" in sql
    assert "(leg_reach_cm IS NULL AND %s IS NOT NULL)" in sql
    assert "(NULLIF(trains_at, '') IS NULL AND %s IS NOT NULL)" in sql


def test_update_with_all_null_new_values_updates_nothing(fakedb):
    # rowcount 0 simulates the WHERE guard matching no row (every "%s IS NOT
    # NULL" arm is false when all new values are NULL) -> nothing overwritten.
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert update_fighter_enrichment(conn, 7) is False
    _, params = conn.cursors[0].executed[0]
    # Everything except the fighter id is NULL -> the WHERE guard can't match.
    assert params.count(None) == len(params) - 1
    assert 7 in params


# ------------------------------------------------------------------- backfill


# Rows as the query returns them: (id, name, nickname, ufc_confirmed).
# ufc_confirmed=True: both already carried a ufc.com headshot (old behaviour).
_TARGET_ROWS = [(1, "Marlon Vera", None, True), (2, "Jon Jones", "Bones", True)]


def _responder(update_result, target_rows=_TARGET_ROWS):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT") and "FROM fighters" in flat:
            return target_rows
        if "UPDATE fighters" in flat:
            return update_result  # length simulates rowcount
        return []

    return responder


def _resolved(_session, name):
    return AthleteData(
        full_body_url="https://www.ufc.com/images/styles/athlete_bio_full_body/s3/x.png",
        leg_reach_cm=105.4,
        trains_at="Millennia MMA, Rancho, CA",
        page_name=name,  # page renders exactly the DB name -> guard passes
    )


def test_backfill_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=True, resolver=_resolved, sleeper=lambda _s: None
    )
    assert counts["targets"] == 2
    assert counts["resolved"] == 2
    assert counts["with_full_body"] == 2
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_backfill_writes_and_commits_per_updated_fighter(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=_resolved, sleeper=lambda _s: None
    )
    assert counts["updated"] == 2
    assert conn.commits == 2
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 2
    assert all("COALESCE" in sql for sql in updates)


def test_backfill_skips_update_when_page_has_no_new_data(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=lambda s, n: AthleteData(), sleeper=lambda _s: None
    )
    assert counts["resolved"] == 2
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_backfill_counts_unresolved_pages(fakedb):
    # resolve_athlete returns None on ufc.com 404s (historical fighters without
    # a page): counted as unresolved, never an error, never a write.
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=lambda s, n: None, sleeper=lambda _s: None
    )
    assert counts["unresolved"] == 2
    assert fakedb.mutating_statements(conn) == []


# ----------------------------------------------------------- target selection


def _selected_sql(conn) -> str:
    return " ".join(conn.cursors[0].executed[0][0].split())


def test_targets_default_scope_is_all_fighters_missing_any_field(fakedb):
    conn = fakedb.Connection(_responder(update_result=[]))
    targets = enrich_fullbody._get_target_fighters(conn)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # No ufc.com WHERE filter any more: every fighter missing one of the three
    # columns is a target; ufc.com only qualifies the confirmation flag.
    where = flat.split("WHERE", 1)[1]
    assert "ILIKE" not in where.split("ORDER BY")[0]
    assert "(f.full_body_url IS NULL OR f.leg_reach_cm IS NULL OR f.trains_at IS NULL)" in flat
    assert "(f.headshot_url ILIKE %s) AS ufc_confirmed" in flat
    assert params == ("%ufc.com%",)
    # Rows come back typed as (id, name, nickname, ufc_confirmed).
    assert targets == [(1, "Marlon Vera", None, True), (2, "Jon Jones", "Bones", True)]


def test_targets_ordered_ranked_then_upcoming_then_headshot_then_rest(fakedb):
    conn = fakedb.Connection(_responder(update_result=[]))
    enrich_fullbody._get_target_fighters(conn)
    flat = _selected_sql(conn)
    order_by = flat.split("ORDER BY", 1)[1]
    ranked = order_by.index("r.snapshot_date = (SELECT MAX(snapshot_date) FROM rankings)")
    upcoming = order_by.index("e.status = 'upcoming'")
    headshot = order_by.index("NULLIF(f.headshot_url, '') IS NOT NULL")
    name = order_by.rindex("f.name")
    assert ranked < upcoming < headshot < name
    # Boolean sort keys put TRUE first only with DESC.
    assert order_by.count("DESC") == 3
    # Ranked priority reads the LATEST snapshot, not any historical one.
    assert "MAX(snapshot_date)" in order_by


def test_targets_solo_ufc_restores_old_filter(fakedb):
    conn = fakedb.Connection(_responder(update_result=[]))
    targets = enrich_fullbody._get_target_fighters(conn, solo_ufc=True)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "WHERE headshot_url ILIKE %s" in flat
    assert "(full_body_url IS NULL OR leg_reach_cm IS NULL OR trains_at IS NULL)" in flat
    assert params == ("%ufc.com%",)
    assert targets == [(1, "Marlon Vera", None, True), (2, "Jon Jones", "Bones", True)]


def test_targets_select_exact_columns_in_both_scopes(fakedb):
    # The rows are unpacked by POSITION as (id, name, nickname, ufc_confirmed),
    # so the SELECT list is pinned exactly: a reordered column or a
    # 'NULL AS nickname' must fail here instead of silently in production.
    # An empty-string nickname comes back as None (no identity to match).
    nickname = enrich_fullbody._unique_nickname_sql
    expected = {
        False: f"SELECT f.id, f.name, {nickname('f.nickname')}, "
        "(f.headshot_url ILIKE %s) AS ufc_confirmed FROM fighters f WHERE ",
        True: f"SELECT id, name, {nickname('nickname')}, TRUE AS ufc_confirmed "
        "FROM fighters WHERE ",
    }
    rows = [(9132, "Valesca Machado", "Tina Black", False), (1, "Marlon Vera", "", True)]
    for solo_ufc in (False, True):
        conn = fakedb.Connection(_responder(update_result=[], target_rows=rows))
        targets = enrich_fullbody._get_target_fighters(conn, solo_ufc=solo_ufc)
        assert _selected_sql(conn).startswith(expected[solo_ufc])
        assert targets == [
            (9132, "Valesca Machado", "Tina Black", False),
            (1, "Marlon Vera", None, True),
        ]


def test_unique_nickname_sql_drops_a_nickname_other_fighters_share():
    # 'The Sniper' is the nickname of 5 fighters in the DB: it cannot tell whose
    # page this is, so the query hands the guard NULL instead of it. Same
    # criterion as the exact-duplicate-name exclusion (lower, trimmed); the
    # subquery is uncorrelated, so Postgres evaluates it once.
    assert enrich_fullbody._unique_nickname_sql("f.nickname") == (
        "CASE WHEN lower(btrim(f.nickname)) IN ("
        "SELECT lower(btrim(nickname)) FROM fighters"
        " GROUP BY 1 HAVING count(nickname) > 1"
        ") THEN NULL ELSE f.nickname END AS nickname"
    )


def test_targets_limit_appended_in_both_scopes(fakedb):
    for solo_ufc in (False, True):
        conn = fakedb.Connection(_responder(update_result=[]))
        enrich_fullbody._get_target_fighters(conn, limit=8, solo_ufc=solo_ufc)
        sql, params = conn.cursors[0].executed[0]
        assert sql.rstrip().endswith("LIMIT %s")
        assert params[-1] == 8


def test_backfill_solo_ufc_flag_reaches_the_query(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    enrich_fullbody.backfill(
        conn, dry_run=True, solo_ufc=True, resolver=_resolved, sleeper=lambda _s: None
    )
    assert "WHERE headshot_url ILIKE %s" in _selected_sql(conn)


# ---------------------------------------------------------- anti-homonym guard


def test_names_match_exact_ignoring_accents_case_and_spacing():
    assert enrich_fullbody._names_match("José  Aldo", "jose aldo")


def test_names_match_allows_extra_token_on_either_side():
    assert enrich_fullbody._names_match("Jose Aldo", "Jose Aldo Junior")
    assert enrich_fullbody._names_match("Jose Aldo Junior", "Jose Aldo")


def test_names_match_is_word_order_insensitive():
    assert enrich_fullbody._names_match("Weili Zhang", "Zhang Weili")


def test_names_match_rejects_homonym():
    assert not enrich_fullbody._names_match("Joe Smith", "John Smith")
    assert not enrich_fullbody._names_match("Conor McGregor", "")


def test_names_match_transliterates_stroke_letters():
    # NFKD no descompone la 'ł' polaca (ni ø/đ/ħ): sin transliteracion
    # 'Blachowicz' no casaba con 'Błachowicz' y el guard descartaba datos reales.
    assert enrich_fullbody._names_match("Jan Blachowicz", "Jan Błachowicz")
    assert enrich_fullbody._names_match("Jan Błachowicz", "Jan Blachowicz")


# 🪤 Caso real (fighters.id=9132): en la base es 'Valesca Machado' con apodo
# 'Tina Black', y ufc.com la publica en /athlete/valesca-machado con el h1
# 'Tina Black'. Por nombre no casa nunca, así que las tres guardas la rechazaban
# y se quedaba sin KO/SUB/1R, gimnasio, lugar de nacimiento ni facts.


def test_hero_name_matches_page_rendering_the_exact_nickname():
    assert enrich_fullbody._hero_name_matches("Valesca Machado", "Tina Black", "Tina Black")


def test_hero_name_matches_nickname_normalized_like_names():
    # Same normalization as the name: accents, case and spacing never matter.
    assert enrich_fullbody._hero_name_matches("Valesca Machado", "Tína  BLACK", "Tina Black")
    assert enrich_fullbody._hero_name_matches("Valesca Machado", "Tina Black", "TINA BLÁCK")


def test_hero_name_rejects_nickname_page_for_another_fighter_without_nickname():
    assert not enrich_fullbody._hero_name_matches("Joe Smith", None, "Tina Black")
    assert not enrich_fullbody._hero_name_matches("Joe Smith", "", "Tina Black")


def test_hero_name_rejects_single_token_nickname():
    # 'Black' is a subset of 'Tina Black' but the nickname path never uses
    # containment; and a one-word nickname is not an identity even when equal.
    assert not enrich_fullbody._hero_name_matches("Valesca Machado", "Black", "Tina Black")
    assert not enrich_fullbody._hero_name_matches("Jon Jones Jr", "Bones", "Bones")


def test_hero_name_rejects_one_word_nickname_even_when_it_splits_into_tokens():
    # Real DB nicknames: 'D-Rod' normalizes to ['d', 'rod'], but it is ONE word,
    # a mononym-shaped label like 'Bones'. Words are counted on the stored
    # nickname (split on whitespace); tokens only decide the equality.
    for name, nickname in (
        ("Daniel Rodriguez", "D-Rod"),
        ("Maria Oliveira", "Spider-Girl"),
        ("Gavin Tucker", "Guv'Nor"),
        ("Brian Ortega", "T-City"),
    ):
        assert not enrich_fullbody._hero_name_matches(name, nickname, nickname)
    # A punctuation-only chunk is not a word either: 'Tina .' is one word.
    assert not enrich_fullbody._hero_name_matches("Valesca Machado", "Tina .", "Tina")


def test_hero_name_nickname_requires_equality_not_containment():
    assert not enrich_fullbody._hero_name_matches("Valesca Machado", "Tina Black Jr", "Tina Black")
    assert not enrich_fullbody._hero_name_matches("Valesca Machado", "Tina Black", "Tina Black Jr")
    # Token ORDER counts too: the nickname is a phrase, not a set of words.
    assert not enrich_fullbody._hero_name_matches("Valesca Machado", "Black Tina", "Tina Black")


def test_hero_name_name_path_is_unchanged():
    # The nickname only ADDS a way in; the name rules stay exactly as they were.
    assert enrich_fullbody._hero_name_matches("Jose Aldo", None, "Jose Aldo Junior")
    assert enrich_fullbody._hero_name_matches("Weili Zhang", "Magnum", "Zhang Weili")
    assert not enrich_fullbody._hero_name_matches("Joe Smith", "Tina Black", "John Smith")


def test_page_identity_verified_accepts_exact_nickname():
    data = AthleteData(full_body_url="https://x.png", page_name="Tina Black")
    assert enrich_fullbody._page_identity_verified(
        "Valesca Machado", data, False, nickname="Tina Black"
    )
    assert not enrich_fullbody._page_identity_verified("Joe Smith", data, False, nickname=None)
    assert not enrich_fullbody._page_identity_verified(
        "Valesca Machado", data, False, nickname="Black"
    )


def test_page_identity_verified_without_page_name_still_depends_only_on_confirmation():
    anon = AthleteData(full_body_url="https://x.png", page_name=None)
    assert enrich_fullbody._page_identity_verified("Valesca Machado", anon, True, nickname="Tina Black")
    assert not enrich_fullbody._page_identity_verified(
        "Valesca Machado", anon, False, nickname="Tina Black"
    )


def test_backfill_accepts_page_rendering_the_fighters_nickname(fakedb):
    # Valesca Machado is published under her nickname; a fighter WITHOUT that
    # nickname landing on the same hero name is still a mismatch.
    rows = [(9132, "Valesca Machado", "Tina Black", False), (2, "Joe Smith", None, False)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    counts = enrich_fullbody.backfill(
        conn,
        dry_run=False,
        resolver=lambda _s, _n: AthleteData(full_body_url="https://x.png", page_name="Tina Black"),
        sleeper=lambda _s: None,
    )
    assert counts["resolved"] == 1
    assert counts["name_mismatch"] == 1
    assert counts["updated"] == 1
    updates = [
        params for cur in conn.cursors for sql, params in cur.executed if "UPDATE" in sql.upper()
    ]
    assert len(updates) == 1
    assert 9132 in updates[0]


def test_backfill_counts_and_logs_every_acceptance_by_nickname(fakedb, caplog):
    # The nickname path is looser than the old guard, so every page it lets in
    # leaves a trace: its own counter plus an INFO line with both names. A page
    # accepted by NAME never counts there.
    rows = [(9132, "Valesca Machado", "Tina Black", False), (1, "Marlon Vera", "Chito", True)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    hero = {"Valesca Machado": "Tina Black", "Marlon Vera": "Marlon Vera"}
    with caplog.at_level("INFO", logger="src.scrapers.enrich_fullbody"):
        counts = enrich_fullbody.backfill(
            conn,
            dry_run=True,
            resolver=lambda _s, name: AthleteData(full_body_url="https://x.png", page_name=hero[name]),
            sleeper=lambda _s: None,
        )
    assert counts["resolved"] == 2
    assert counts["nickname_match"] == 1
    traces = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Accepted by nickname")
    ]
    assert len(traces) == 1
    assert "id=9132" in traces[0]
    assert "'Tina Black'" in traces[0]


def test_backfill_skips_and_counts_name_mismatch_without_writing(fakedb):
    # Guessed slug (ufc_confirmed=False) landing on a namesake's page: the hero
    # name disagrees with the DB name -> skip, count, and never touch the DB.
    rows = [(1, "Joe Smith", None, False)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    homonym = lambda _s, _n: AthleteData(  # noqa: E731
        full_body_url="https://www.ufc.com/images/styles/athlete_bio_full_body/s3/SMITH_JOHN.png",
        page_name="John Smith",
    )
    counts = enrich_fullbody.backfill(
        conn, dry_run=False, resolver=homonym, sleeper=lambda _s: None
    )
    assert counts["name_mismatch"] == 1
    assert counts["resolved"] == 0
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_backfill_guard_applies_even_to_confirmed_fighters(fakedb):
    # The guard is free (the HTML is already in memory), so it also protects
    # confirmed fighters against slug redirects landing on someone else's page.
    rows = [(1, "Joe Smith", None, True)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    counts = enrich_fullbody.backfill(
        conn,
        dry_run=False,
        resolver=lambda _s, _n: AthleteData(full_body_url="https://x.png", page_name="John Smith"),
        sleeper=lambda _s: None,
    )
    assert counts["name_mismatch"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_backfill_accepts_guessed_slug_when_page_name_has_extra_token(fakedb):
    rows = [(1, "Jose Aldo", None, False)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    counts = enrich_fullbody.backfill(
        conn,
        dry_run=False,
        resolver=lambda _s, _n: AthleteData(full_body_url="https://x.png", page_name="Jose Aldo Junior"),
        sleeper=lambda _s: None,
    )
    assert counts["name_mismatch"] == 0
    assert counts["resolved"] == 1
    assert counts["updated"] == 1


def test_backfill_missing_page_name_trusts_only_confirmed_fighters(fakedb):
    # A page without hero-profile__name cannot be verified: proceed only for
    # fighters whose ufc.com headshot already proved the page is theirs.
    rows = [(1, "Marlon Vera", None, True), (2, "Conor McGregor", "The Notorious", False)]
    conn = fakedb.Connection(_responder(update_result=[(1,)], target_rows=rows))
    counts = enrich_fullbody.backfill(
        conn,
        dry_run=False,
        resolver=lambda _s, _n: AthleteData(full_body_url="https://x.png", page_name=None),
        sleeper=lambda _s: None,
    )
    assert counts["resolved"] == 1
    assert counts["name_mismatch"] == 1
    assert counts["updated"] == 1
    assert len(fakedb.mutating_statements(conn)) == 1
