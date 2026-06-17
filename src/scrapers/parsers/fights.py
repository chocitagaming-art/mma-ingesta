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
    stats_table = soup.select_one("table.b-fight-details__table")
    if not stats_table:
        return []
    first_data_row = next(
        (
            row
            for row in stats_table.select("tbody tr")
            if row.select("td") and row.select_one("a[href*='/fighter-details/']")
        ),
        None,
    )
    if first_data_row is None:
        return []
    cells = first_data_row.select("td")
    if len(cells) < 10:
        return []
    fighter_links = first_data_row.select("a[href*='/fighter-details/']")
    fighter_names = [clean_text(link.get_text(" ", strip=True)) or "" for link in fighter_links[:2]]
    fighter_source_ids = [
        source_id_from_url(link.get("href")) if link.get("href") else None
        for link in fighter_links[:2]
    ]
    kd_values = _extract_column_values(cells[1])
    sig_values = _extract_column_values(cells[2])
    td_values = _extract_column_values(cells[5])
    sub_values = _extract_column_values(cells[7])
    ctrl_values = _extract_column_values(cells[9])
    parsed: list[ParsedFightStats] = []
    for index in range(min(2, len(fighter_names))):
        sig_landed, sig_attempted = parse_landed_attempted(sig_values[index] if index < len(sig_values) else None)
        td_landed, td_attempted = parse_landed_attempted(td_values[index] if index < len(td_values) else None)
        parsed.append(
            ParsedFightStats(
                fighter_source_id=fighter_source_ids[index] if index < len(fighter_source_ids) else None,
                fighter_name=fighter_names[index],
                stats={
                    "sig_strikes_landed": sig_landed,
                    "sig_strikes_attempted": sig_attempted,
                    "takedowns_landed": td_landed,
                    "takedowns_attempted": td_attempted,
                    "submission_attempts": parse_int(sub_values[index] if index < len(sub_values) else None),
                    "control_time_seconds": parse_control_time_to_seconds(ctrl_values[index] if index < len(ctrl_values) else None),
                    "knockdowns": parse_int(kd_values[index] if index < len(kd_values) else None),
                },
            )
        )
    return parsed


def build_fight_stats_record(fight_id: int, fighter_id: int, parsed: ParsedFightStats) -> FightStatsRecord:
    return FightStatsRecord(
        fight_id=fight_id,
        fighter_id=fighter_id,
        sig_strikes_landed=parsed.stats["sig_strikes_landed"],
        sig_strikes_attempted=parsed.stats["sig_strikes_attempted"],
        takedowns_landed=parsed.stats["takedowns_landed"],
        takedowns_attempted=parsed.stats["takedowns_attempted"],
        submission_attempts=parsed.stats["submission_attempts"],
        control_time_seconds=parsed.stats["control_time_seconds"],
        knockdowns=parsed.stats["knockdowns"],
    )


def _parse_winner_corner(row: BeautifulSoup) -> str | None:
    first_cell = row.select_one("td")
    text = clean_text(first_cell.get_text(" ", strip=True) if first_cell else None)
    if not text:
        return None
    values = [value.lower() for value in text.split() if value]
    if "win" in values:
        return "red"
    if "loss" in values:
        return "blue"
    return None


def _extract_column_values(cell: BeautifulSoup) -> list[str]:
    values = [clean_text(node.get_text(" ", strip=True)) for node in cell.select("p")]
    return [value for value in values if value]


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