"""The corner must never encode who won. One rule, checked at the two places it can leak.

THE BUG THIS PINS DOWN. ufcstats publishes no corner column: its event table lists the
WINNER first, and so does its fight-details page. Two code paths turned that ordering
into a stored corner:

  1. `parse_event_fights` took red = first fighter link.
  2. `repair_fight_winners` swapped the stored corners to match the detail page.

Result: the 232 bouts of 2026 imported that way came out 232/232 red-wins, against
44.7-52.3% in every other year. `reach_cm_diff` and friends are computed red-minus-blue,
so the target was leaking straight into the features. Repaired by hand on 2026-08-22
(103 rows + 147 judge scorecards).

THE RULE NOW: the real corner comes from ufc.com, the only source that publishes it.
Where there is no such source (historical ufcstats backfill), corners are assigned by
ascending fighter source_id -- arbitrary, stable, and blind to the result. And a stored
corner is never overwritten by a ufcstats pass, so the two rules cannot fight.

Offline: pure parsing plus a fake connection.
"""

from types import SimpleNamespace

from bs4 import BeautifulSoup

from src.scrapers.parsers.fights import parse_event_fights

SETTINGS = SimpleNamespace(source_name="ufcstats")


def _event_row(first_id: str, second_id: str, first_result: str, second_result: str) -> str:
    """One ufcstats event-table row. ufcstats always renders the WINNER first."""
    return f"""
    <tr data-link="http://ufcstats.com/fight-details/bout1">
      <td><p class="b-fight-details__table-text">{first_result}</p>
          <p class="b-fight-details__table-text">{second_result}</p></td>
      <td><a href="http://ufcstats.com/fighter-details/{first_id}">First Fighter</a>
          <a href="http://ufcstats.com/fighter-details/{second_id}">Second Fighter</a></td>
      <td></td><td></td><td></td><td></td>
      <td><p>Lightweight</p></td>
      <td><p>KO/TKO</p><p>Punches</p></td>
      <td><p>1</p></td><td><p>3:21</p></td>
    </tr>
    """


def _parse_one(html: str):
    return parse_event_fights(BeautifulSoup(f"<table>{html}</table>", "html.parser"), SETTINGS)[0]


def test_the_winner_does_not_decide_the_corner():
    """Same two fighters, same order on the page, opposite results.

    ufcstats renders the winner first either way, so if the corner followed the page
    order the red corner would change with the result. It must not.
    """
    aaa_wins = _parse_one(_event_row("aaa", "zzz", "W", "L"))
    zzz_wins = _parse_one(_event_row("zzz", "aaa", "W", "L"))

    assert aaa_wins.red_source_id == zzz_wins.red_source_id == "/fighter-details/aaa"
    assert aaa_wins.blue_source_id == zzz_wins.blue_source_id == "/fighter-details/zzz"
    # ...and the winner is still reported correctly in both.
    assert aaa_wins.winner_corner == "red"
    assert zzz_wins.winner_corner == "blue"


def test_corner_follows_ascending_source_id():
    """The fallback rule: red = lower source_id. 99.7% of the stored history follows it."""
    already_ordered = _parse_one(_event_row("aaa", "zzz", "W", "L"))
    needs_flip = _parse_one(_event_row("zzz", "aaa", "L", "W"))

    for fight in (already_ordered, needs_flip):
        assert fight.red_source_id == "/fighter-details/aaa"
        assert fight.blue_source_id == "/fighter-details/zzz"


def test_names_travel_with_the_corner_they_belong_to():
    """A flip that moved the ids but not the names would mislabel every fighter."""
    fight = _parse_one(_event_row("zzz", "aaa", "W", "L"))
    assert fight.red_source_id == "/fighter-details/aaa" and fight.red_name == "Second Fighter"
    assert fight.blue_source_id == "/fighter-details/zzz" and fight.blue_name == "First Fighter"
    # "First Fighter" won (listed first by ufcstats) and now sits in blue.
    assert fight.winner_corner == "blue"


def test_a_draw_keeps_no_winner_through_the_flip():
    fight = _parse_one(_event_row("zzz", "aaa", "D", "D"))
    assert fight.red_source_id == "/fighter-details/aaa"
    assert fight.winner_corner is None


def test_a_missing_source_id_is_left_alone():
    """No id to sort by: keep the page order rather than invent one."""
    row = """
    <tr data-link="http://ufcstats.com/fight-details/bout9">
      <td><p class="b-fight-details__table-text">W</p>
          <p class="b-fight-details__table-text">L</p></td>
      <td><a href="http://ufcstats.com/fighter-details/zzz">Only Linked</a>
          <span>Unlinked Opponent</span></td>
      <td></td><td></td><td></td><td></td>
      <td><p>Lightweight</p></td><td><p>U-DEC</p><p></p></td>
      <td><p>3</p></td><td><p>5:00</p></td>
    </tr>
    """
    fight = _parse_one(row)
    assert fight.red_source_id == "/fighter-details/zzz"
    assert fight.blue_source_id is None
    assert fight.winner_corner == "red"
