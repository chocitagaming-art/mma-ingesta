"""ufc.com renders each weight division's view-grouping TWICE (desktop + mobile
blocks with identical content). _parse_rankings must collapse those duplicates to one
division per slug, otherwise it emits duplicate (division, rank_position) rows — the
root cause of the doubled 2026-06-24 snapshot that violated rankings_slot_key.
"""

from collections import Counter

from src.scrapers.rankings import _parse_rankings


def _division_html(header: str, champion: str | None, fighters: list[str]) -> str:
    rows = "".join(
        f'<tr><td class="views-field-title">{n}</td>'
        f'<td class="views-field-weight-class-rank">{i + 1}</td></tr>'
        for i, n in enumerate(fighters)
    )
    champ_block = (
        '<div class="rankings--athlete--champion"><div class="info">'
        f'<h5><a>{champion}</a></h5></div></div>'
        if champion is not None
        else ""
    )
    return (
        '<div class="view-grouping">'
        f'<div class="view-grouping-header">{header}</div>'
        f'{champ_block}'
        f'<table><tbody>{rows}</tbody></table>'
        '</div>'
    )


def test_parse_rankings_collapses_duplicate_groupings():
    block = _division_html("Heavyweight", "Tom Aspinall", ["Fighter A", "Fighter B"])
    html = f"<html><body>{block}{block}</body></html>"  # division rendered twice
    counts: Counter = Counter()

    divisions = _parse_rankings(html, counts)

    assert len(divisions) == 1, "duplicate grouping must collapse to a single division"
    assert divisions[0].slug == "heavyweight"
    assert divisions[0].champion_name == "Tom Aspinall"
    assert [e.rank_position for e in divisions[0].entries] == [1, 2]
    assert counts["duplicate_groupings"] == 1


def test_parse_rankings_preserves_champion_across_asymmetric_renders():
    """If duplicate renders are asymmetric — the richer block (more entries) lacks the
    champion while the other has it — the collapse must keep BOTH the richer entries
    and the champion, never dropping rank 0."""
    richer_no_champ = _division_html("Heavyweight", None, ["A", "B", "C"])
    poorer_with_champ = _division_html("Heavyweight", "Tom Aspinall", ["A", "B"])
    html = f"<html><body>{richer_no_champ}{poorer_with_champ}</body></html>"
    counts: Counter = Counter()

    divisions = _parse_rankings(html, counts)

    assert len(divisions) == 1
    assert divisions[0].champion_name == "Tom Aspinall"
    assert [e.rank_position for e in divisions[0].entries] == [1, 2, 3]  # richer block kept
    assert counts["duplicate_groupings"] == 1


def test_parse_rankings_keeps_distinct_divisions():
    html = (
        "<html><body>"
        + _division_html("Heavyweight", "Tom Aspinall", ["A", "B"])
        + _division_html("Lightweight", "Islam Makhachev", ["C", "D", "E"])
        + "</body></html>"
    )
    counts: Counter = Counter()

    divisions = _parse_rankings(html, counts)

    assert {d.slug for d in divisions} == {"heavyweight", "lightweight"}
    assert counts["duplicate_groupings"] == 0
