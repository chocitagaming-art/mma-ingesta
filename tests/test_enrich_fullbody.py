"""Phase 2 (full-body/leg-reach/gym) parsing + backfill tests.

The HTML fixtures mirror a real ufc.com/athlete page: the full-body photo lives
in div.hero-profile__image-wrap > img.hero-profile__image[src] (URL NOT
constructible a priori) and the bio block exposes 'Leg reach' / 'Trains at' in
div.c-bio__field ('Age' comes nested in div.field--name-age and is ignored).
No network, no DB: resolve_athlete gets a fake session and writes go through the
shared fakedb recorder.
"""

from datetime import date

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
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.ok = status_code < 400


class _FakeSession:
    def __init__(self, html: str):
        self._html = html
        self.requested: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        return _FakeResponse(self._html)


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


# ufc_confirmed=True: both already carried a ufc.com headshot (old behaviour).
_TARGET_ROWS = [(1, "Marlon Vera", True), (2, "Jon Jones", True)]


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
    # Rows come back typed as (id, name, ufc_confirmed).
    assert targets == [(1, "Marlon Vera", True), (2, "Jon Jones", True)]


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
    assert targets == [(1, "Marlon Vera", True), (2, "Jon Jones", True)]


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


def test_backfill_skips_and_counts_name_mismatch_without_writing(fakedb):
    # Guessed slug (ufc_confirmed=False) landing on a namesake's page: the hero
    # name disagrees with the DB name -> skip, count, and never touch the DB.
    rows = [(1, "Joe Smith", False)]
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
    rows = [(1, "Joe Smith", True)]
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
    rows = [(1, "Jose Aldo", False)]
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
    rows = [(1, "Marlon Vera", True), (2, "Conor McGregor", False)]
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
