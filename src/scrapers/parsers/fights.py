from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Settings
from ..models import FightStatsRecord
from ..utils import clean_text, parse_control_time_to_seconds, parse_int, parse_landed_attempted, source_id_from_url


BASE_URL = "http://ufcstats.com"


@dataclass(frozen=True)
class FightPageRecord:
    red_name: str
    blue_name: str
    red_source_id: str | None
    blue_source_id: str | None
    weight_class: str | None
    scheduled_rounds: int | None
    winner_corner: str | None
    method: str | None
    end_round: int | None
    end_time: str | None
    detail_url: str
    source_id: str


@dataclass(frozen=True)
class ParsedFightStats:
    fighter_source_id: str | None
    fighter_name: str
    stats: dict[str, int | None]


def parse_event_fights(soup: BeautifulSoup, settings: Settings) -> list[FightPageRecord]:
    fights: list[FightPageRecord] = []
    seen: set[str] = set()
    for row in soup.select("tr[data-link]"):
        detail_url = urljoin(BASE_URL, row.get("data-link"))
        if detail_url in seen:
            continue
        seen.add(detail_url)
        fighter_links = row.select("a[href*='/fighter-details/']")
        red_name = clean_text(fighter_links[0].get_text(" ", strip=True) if len(fighter_links) >= 1 else None) or ""
        blue_name = clean_text(fighter_links[1].get_text(" ", strip=True) if len(fighter_links) >= 2 else None) or ""
        red_source_id = source_id_from_url(fighter_links[0].get("href")) if len(fighter_links) >= 1 and fighter_links[0].get("href") else None
        blue_source_id = source_id_from_url(fighter_links[1].get("href")) if len(fighter_links) >= 2 and fighter_links[1].get("href") else None
        cells = row.select("td")
        winner_corner = _parse_winner_corner(row)
        method = _extract_dual_value(cells[7], 0) if len(cells) > 7 else None
        method_detail = _extract_dual_value(cells[7], 1) if len(cells) > 7 else None
        fights.append(
            FightPageRecord(
                red_name=red_name,
                blue_name=blue_name,
                red_source_id=red_source_id,
                blue_source_id=blue_source_id,
                weight_class=clean_text(cells[6].get_text(" ", strip=True) if len(cells) > 6 else None),
                scheduled_rounds=_infer_scheduled_rounds(
                    clean_text(cells[6].get_text(" ", strip=True) if len(cells) > 6 else None)
                ),
                winner_corner=winner_corner,
                method=_join_method(method, method_detail),
                end_round=parse_int(cells[8].get_text(" ", strip=True) if len(cells) > 8 else None),
                end_time=clean_text(cells[9].get_text(" ", strip=True) if len(cells) > 9 else None),
                detail_url=detail_url,
                source_id=source_id_from_url(detail_url),
            )
        )
    return fights


def parse_fight_stats(soup: BeautifulSoup) -> list[ParsedFightStats]:
    """Parse per-fighter fight totals from a ufcstats fight-details page.

    ufcstats serves the stats tables broken down PER ROUND (one ``<tbody>`` row
    per round, no all-rounds totals row). The fix for the historical undercount
    (#44) is to SUM every per-round row instead of reading only round 1.

    Two tables are used, identified by their header (robust to column drift):
      - "overall" (header contains 'KD'): KD, Sig. str., Total str., Td,
        Sub. att, Ctrl.
      - "by target/position" (header contains 'Head'): Head, Body, Leg,
        Distance, Clinch, Ground. Absent on very old fights -> NULL columns.
    """
    tables = soup.select("table.b-fight-details__table")
    if not tables:
        return []
    overall = _find_stats_table(tables, "KD") or tables[0]
    targets = _find_stats_table(tables, "Head")
    overall_rows = _data_rows(overall)
    if not overall_rows:
        return []
    target_rows = _data_rows(targets) if targets is not None else []

    first_cells = overall_rows[0].find_all("td", recursive=False)
    fighter_links = first_cells[0].select("a[href*='/fighter-details/']") if first_cells else []
    fighter_names = [clean_text(link.get_text(" ", strip=True)) or "" for link in fighter_links[:2]]
    fighter_source_ids = [
        source_id_from_url(link.get("href")) if link.get("href") else None
        for link in fighter_links[:2]
    ]

    parsed: list[ParsedFightStats] = []
    for index in range(min(2, len(fighter_names))):
        sig_landed, sig_attempted = _sum_landed_attempted(overall_rows, 2, index)
        td_landed, td_attempted = _sum_landed_attempted(overall_rows, 5, index)
        head_landed, head_attempted = _sum_landed_attempted(target_rows, 3, index)
        body_landed, body_attempted = _sum_landed_attempted(target_rows, 4, index)
        leg_landed, leg_attempted = _sum_landed_attempted(target_rows, 5, index)
        distance_landed, distance_attempted = _sum_landed_attempted(target_rows, 6, index)
        clinch_landed, clinch_attempted = _sum_landed_attempted(target_rows, 7, index)
        ground_landed, ground_attempted = _sum_landed_attempted(target_rows, 8, index)
        parsed.append(
            ParsedFightStats(
                fighter_source_id=fighter_source_ids[index] if index < len(fighter_source_ids) else None,
                fighter_name=fighter_names[index],
                stats={
                    "sig_strikes_landed": sig_landed,
                    "sig_strikes_attempted": sig_attempted,
                    "takedowns_landed": td_landed,
                    "takedowns_attempted": td_attempted,
                    "submission_attempts": _sum_int(overall_rows, 7, index),
                    "control_time_seconds": _sum_seconds(overall_rows, 9, index),
                    "knockdowns": _sum_int(overall_rows, 1, index),
                    "sig_str_head_landed": head_landed,
                    "sig_str_head_attempted": head_attempted,
                    "sig_str_body_landed": body_landed,
                    "sig_str_body_attempted": body_attempted,
                    "sig_str_leg_landed": leg_landed,
                    "sig_str_leg_attempted": leg_attempted,
                    "sig_str_distance_landed": distance_landed,
                    "sig_str_distance_attempted": distance_attempted,
                    "sig_str_clinch_landed": clinch_landed,
                    "sig_str_clinch_attempted": clinch_attempted,
                    "sig_str_ground_landed": ground_landed,
                    "sig_str_ground_attempted": ground_attempted,
                },
            )
        )
    return parsed


def build_fight_stats_record(fight_id: int, fighter_id: int, parsed: ParsedFightStats) -> FightStatsRecord:
    stats = parsed.stats
    return FightStatsRecord(
        fight_id=fight_id,
        fighter_id=fighter_id,
        sig_strikes_landed=stats["sig_strikes_landed"],
        sig_strikes_attempted=stats["sig_strikes_attempted"],
        takedowns_landed=stats["takedowns_landed"],
        takedowns_attempted=stats["takedowns_attempted"],
        submission_attempts=stats["submission_attempts"],
        control_time_seconds=stats["control_time_seconds"],
        knockdowns=stats["knockdowns"],
        sig_str_head_landed=stats["sig_str_head_landed"],
        sig_str_head_attempted=stats["sig_str_head_attempted"],
        sig_str_body_landed=stats["sig_str_body_landed"],
        sig_str_body_attempted=stats["sig_str_body_attempted"],
        sig_str_leg_landed=stats["sig_str_leg_landed"],
        sig_str_leg_attempted=stats["sig_str_leg_attempted"],
        sig_str_distance_landed=stats["sig_str_distance_landed"],
        sig_str_distance_attempted=stats["sig_str_distance_attempted"],
        sig_str_clinch_landed=stats["sig_str_clinch_landed"],
        sig_str_clinch_attempted=stats["sig_str_clinch_attempted"],
        sig_str_ground_landed=stats["sig_str_ground_landed"],
        sig_str_ground_attempted=stats["sig_str_ground_attempted"],
    )


def _parse_winner_corner(row: BeautifulSoup) -> str | None:
    first_cell = row.select_one("td")
    if not first_cell:
        return None
    values = [value.lower() for value in _extract_column_values(first_cell)]
    if len(values) >= 2:
        red_result = values[0]
        blue_result = values[1]
        if red_result == "w" and blue_result == "l":
            return "red"
        if red_result == "l" and blue_result == "w":
            return "blue"
        if {red_result, blue_result} & {"d", "draw", "nc"}:
            return None
    text = clean_text(first_cell.get_text(" ", strip=True) if first_cell else None)
    if not text:
        return None
    normalized = [value.lower() for value in text.split() if value]
    if normalized[:2] == ["w", "l"]:
        return "red"
    if normalized[:2] == ["l", "w"]:
        return "blue"
    if any(value in {"d", "draw", "nc"} for value in normalized):
        return None
    return None


def _extract_column_values(cell: BeautifulSoup) -> list[str]:
    values = [clean_text(node.get_text(" ", strip=True)) for node in cell.select("p")]
    return [value for value in values if value]


def _find_stats_table(tables, header_keyword: str):
    """Return the first stats table whose header contains ``header_keyword``.

    Robust to ufcstats serving either per-round-only tables (current) or a
    separate all-rounds totals table first (older markup): the first match wins,
    and summing 1 totals row or N round rows both yield the fight total.
    """
    keyword = header_keyword.lower()
    for table in tables:
        head = table.find("thead")
        if not head:
            continue
        header_text = " ".join(th.get_text(" ", strip=True) for th in head.find_all("th")).lower()
        if keyword in header_text:
            return table
    return None


def _data_rows(table) -> list:
    """Per-round data rows of a stats table (rows that carry fighter links)."""
    if table is None:
        return []
    rows = []
    for row in table.select("tbody tr"):
        if row.find_all("td", recursive=False) and row.select_one("a[href*='/fighter-details/']"):
            rows.append(row)
    return rows


def _cell_value(row, col_index: int, fighter_index: int) -> str | None:
    cells = row.find_all("td", recursive=False)
    if col_index >= len(cells):
        return None
    paragraphs = cells[col_index].select("p")
    if fighter_index >= len(paragraphs):
        return None
    return paragraphs[fighter_index].get_text(" ", strip=True)


def _sum_landed_attempted(rows, col_index: int, fighter_index: int) -> tuple[int | None, int | None]:
    landed_total = 0
    attempted_total = 0
    seen = False
    for row in rows:
        landed, attempted = parse_landed_attempted(_cell_value(row, col_index, fighter_index))
        if landed is not None and attempted is not None:
            landed_total += landed
            attempted_total += attempted
            seen = True
    return (landed_total, attempted_total) if seen else (None, None)


def _sum_int(rows, col_index: int, fighter_index: int) -> int | None:
    total = 0
    seen = False
    for row in rows:
        value = parse_int(_cell_value(row, col_index, fighter_index))
        if value is not None:
            total += value
            seen = True
    return total if seen else None


def _sum_seconds(rows, col_index: int, fighter_index: int) -> int | None:
    total = 0
    seen = False
    for row in rows:
        value = parse_control_time_to_seconds(_cell_value(row, col_index, fighter_index))
        if value is not None:
            total += value
            seen = True
    return total if seen else None


def _extract_dual_value(cell: BeautifulSoup, index: int) -> str | None:
    values = _extract_column_values(cell)
    if index >= len(values):
        return None
    return values[index]


def _join_method(method: str | None, detail: str | None) -> str | None:
    if method and detail:
        return f"{method} - {detail}"
    return method or detail


def _infer_scheduled_rounds(weight_class: str | None) -> int | None:
    if not weight_class:
        return None
    lowered = weight_class.lower()
    if "title" in lowered or "main event" in lowered:
        return 5
    return 3