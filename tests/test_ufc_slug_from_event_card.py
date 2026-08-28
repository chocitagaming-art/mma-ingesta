"""Stop GUESSING the ufc.com slug: read the one the event card publishes.

THE BUG THIS PINS DOWN, and it has two halves that hid each other.

1. THE GUESS IS OFTEN WRONG. `resolve_athlete` built the URL with
   `slugify(name_in_our_DB)`. Measured against the 29-ago-2026 card, 4 of the 5
   fighters with a photo problem had a slug the guess could never reach:

        DB name            guessed            real (published by ufc.com)
        Hector Santiago    hector-santiago    hector-de-sousa-santiago
        Cameron Nelson     cameron-nelson     cam-nelson
        Ce Liu             ce-liu             liu-ce
        Xiao Long          xiao-long          shiyao-ron

2. AND THE MISS WAS MUTE. An unknown slug on ufc.com does NOT answer 404: it
   answers **200** after redirecting to /search. So a wrong guess came back as a
   healthy page with every bio field empty, and the caller counted it under
   `resolved`, not `unresolved`. Nothing — no counter, no log line, no alarm —
   ever said that Hector Santiago had been unreachable since the day he was
   created. He went into the 29-ago card with no photo of his own for exactly
   this reason.

Half 2 is why half 1 could last: a wrong slug looked identical to a fighter
ufc.com simply has nothing on.

THE FIX, both halves. The event page already links every corner as
<a href="/athlete/<slug>">, so the slug is read instead of invented, keyed by
(data-fmid, corner) — the same join `backfill_standing_photos` uses, which
sidesteps name matching entirely: the page says "Liu Ce" and "Cam Nelson" where
we say "Ce Liu" and "Cameron Nelson". And a redirect to /search now returns
None, so what used to be a silent success is a visible `unresolved`.

Offline: real markup copied from the 29-ago-2026 card, and fake sessions.
"""

import pytest

from src.scrapers.enrich_photos_ufc import (
    GapFighter,
    _slug_for,
    athlete_slugs_from_event_html,
    resolve_athlete,
    slugify,
)

# Markup copied verbatim from https://www.ufc.com/event/ufc-fight-night-august-29-2026
# on 2026-08-28. The two corners differ on purpose and BOTH shapes are real: the
# red one wraps the name in given/family <span>s, the blue one is bare text.
EVENT_CARD_HTML = """
<div class="c-listing-fight" data-fmid="13083">
  <div class="c-listing-fight__corner-name c-listing-fight__corner-name--red">
    <a href="https://www.ufc.com/athlete/lawrence-lui">
      <span class="c-listing-fight__corner-given-name">Lawrence</span>
      <span class="c-listing-fight__corner-family-name">Lui</span>
    </a>
  </div>
  <div class="c-listing-fight__corner-name c-listing-fight__corner-name--blue">
    <a href="https://www.ufc.com/athlete/hector-de-sousa-santiago"> Hector Santiago </a>
  </div>
</div>
<div class="c-listing-fight" data-fmid="13084">
  <div class="c-listing-fight__corner-name c-listing-fight__corner-name--red">
    <a href="https://www.ufc.com/athlete/ding-meng">Ding Meng</a>
  </div>
  <div class="c-listing-fight__corner-name c-listing-fight__corner-name--blue">
    <a href="/athlete/cam-nelson">Cam Nelson</a>
  </div>
</div>
"""


class _FakeResponse:
    def __init__(self, text="", status_code=200, url="https://www.ufc.com/athlete/x"):
        self.text = text
        self.status_code = status_code
        self.ok = status_code < 400
        self.url = url


class _RecordingSession:
    """Records the URLs asked for, and can pretend to have been redirected."""

    def __init__(self, text="", final_url=None, status_code=200):
        self._text = text
        self._final_url = final_url
        self._status_code = status_code
        self.requested: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        return _FakeResponse(self._text, self._status_code, self._final_url or url)


# ------------------------------------------------- reading the slug off the card


def test_reads_the_real_slug_of_both_corners():
    slugs = athlete_slugs_from_event_html(EVENT_CARD_HTML)
    assert slugs[("13083", "red")] == "lawrence-lui"
    assert slugs[("13083", "blue")] == "hector-de-sousa-santiago"
    assert slugs[("13084", "red")] == "ding-meng"
    assert slugs[("13084", "blue")] == "cam-nelson"


def test_the_slugs_read_are_exactly_the_ones_the_guess_would_have_missed():
    """The whole point, stated as an assertion instead of a comment."""
    slugs = athlete_slugs_from_event_html(EVENT_CARD_HTML)
    assert slugs[("13083", "blue")] != slugify("Hector Santiago")
    assert slugs[("13084", "blue")] != slugify("Cameron Nelson")
    # And the ones the guess got right must still come out right.
    assert slugs[("13083", "red")] == slugify("Lawrence Lui")


def test_a_relative_href_resolves_to_the_same_slug_as_an_absolute_one():
    """ufc.com serves both forms; the slug is what matters, not the host."""
    slugs = athlete_slugs_from_event_html(EVENT_CARD_HTML)
    assert slugs[("13084", "blue")] == "cam-nelson"  # came from /athlete/cam-nelson


def test_a_bout_without_data_fmid_is_skipped_not_mismapped():
    """No fight id means no safe join, and a wrong join is worse than no photo."""
    html = EVENT_CARD_HTML.replace('data-fmid="13083"', "")
    slugs = athlete_slugs_from_event_html(html)
    assert ("13083", "red") not in slugs
    assert slugs[("13084", "red")] == "ding-meng"  # the rest of the card survives


def test_empty_or_broken_html_returns_an_empty_map_not_an_error():
    assert athlete_slugs_from_event_html("") == {}
    assert athlete_slugs_from_event_html("<div class='c-listing-fight'></div>") == {}


# ----------------------------------------------------- using it in resolve_athlete


def test_an_explicit_slug_is_used_instead_of_the_guessed_one():
    session = _RecordingSession(text="<html></html>")
    resolve_athlete(session, "Hector Santiago", slug="hector-de-sousa-santiago")
    assert session.requested == ["https://www.ufc.com/athlete/hector-de-sousa-santiago"]


def test_without_a_slug_it_still_guesses_from_the_name():
    """The fallback must survive: `--all` has no card to read a slug from."""
    session = _RecordingSession(text="<html></html>")
    resolve_athlete(session, "Hector Santiago")
    assert session.requested == ["https://www.ufc.com/athlete/hector-santiago"]


def test_a_redirect_to_search_is_a_miss_even_though_it_answers_200():
    """🪤 The mute failure. 200 + /search is 'no such athlete', not a page."""
    session = _RecordingSession(
        text="<html>search results</html>",
        final_url="https://www.ufc.com/search?query=athlete+hector+santiago",
    )
    assert resolve_athlete(session, "Hector Santiago") is None


def test_a_normal_page_is_not_mistaken_for_a_search_redirect():
    session = _RecordingSession(
        text="<html></html>", final_url="https://www.ufc.com/athlete/lawrence-lui"
    )
    assert resolve_athlete(session, "Lawrence Lui") is not None


def test_a_response_without_url_does_not_blow_up():
    """Older test doubles have no `url`; that must not take down a whole pass."""

    class _NoUrlSession:
        def get(self, url, headers=None, timeout=None):
            response = _FakeResponse("<html></html>")
            del response.url
            return response

    assert resolve_athlete(_NoUrlSession(), "Lawrence Lui") is not None


# ------------------------------------------------------------- picking the slug


def test_slug_for_uses_the_card_and_caches_the_page_for_the_whole_event():
    session = _RecordingSession(text=EVENT_CARD_HTML)
    cache: dict = {}
    santiago = GapFighter(9123, "Hector Santiago", "ufc-fight-night-august-29-2026", "13083", "blue")
    nelson = GapFighter(9125, "Cameron Nelson", "ufc-fight-night-august-29-2026", "13084", "blue")

    assert _slug_for(session, cache, santiago) == "hector-de-sousa-santiago"
    assert _slug_for(session, cache, nelson) == "cam-nelson"
    # One card, one fetch: 26 fighters must not mean 26 downloads of the same page.
    assert len(session.requested) == 1


@pytest.mark.parametrize(
    "gap",
    [
        pytest.param(GapFighter(1, "Nadie"), id="sin-contexto-de-evento"),
        pytest.param(GapFighter(1, "Nadie", "un-evento", None, "red"), id="sin-fmid"),
        pytest.param(GapFighter(1, "Nadie", "un-evento", "13083", None), id="sin-esquina"),
    ],
)
def test_without_full_context_it_falls_back_to_the_guess(gap):
    """`--all` scope and any half-filled row: None means 'guess from the name'."""
    session = _RecordingSession(text=EVENT_CARD_HTML)
    assert _slug_for(session, {}, gap) is None
    assert session.requested == []  # and it does not waste a request finding out


def test_a_fighter_not_on_the_card_gets_no_slug_rather_than_a_wrong_one():
    session = _RecordingSession(text=EVENT_CARD_HTML)
    unknown = GapFighter(1, "Nadie", "ufc-fight-night-august-29-2026", "99999", "red")
    assert _slug_for(session, {}, unknown) is None


def test_an_unreachable_event_page_degrades_to_the_guess_instead_of_failing():
    """ufc.com 403s the GitHub runners (measured 28-ago). That must not abort."""
    session = _RecordingSession(text="", status_code=403)
    gap = GapFighter(9123, "Hector Santiago", "ufc-fight-night-august-29-2026", "13083", "blue")
    assert _slug_for(session, {}, gap) is None
