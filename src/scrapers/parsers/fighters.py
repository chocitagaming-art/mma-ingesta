from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Settings
from ..models import FighterRecord
from ..utils import (
    clean_text,
    parse_date,
    parse_height_to_cm,
    parse_reach_to_cm,
    parse_record,
    parse_weight_to_grams,
    source_id_from_url,
)


BASE_URL = "http://ufcstats.com"


def parse_fighter_index(soup: BeautifulSoup) -> list[str]:
    links: list[str] = []
    for anchor in soup.select("a[href*='/fighter-details/']"):
        href = anchor.get("href")
        if href:
            absolute = urljoin(BASE_URL, href)
            if absolute not in links:
                links.append(absolute)
    return links


def parse_fighter_detail(soup: BeautifulSoup, url: str, settings: Settings) -> FighterRecord:
    name = _extract_name(soup)
    nickname = _extract_value_by_label(soup, {"Nickname:", "Nickname"})
    nationality = _extract_value_by_label(soup, {"Nationality:", "Nationality"})
    birth_date = parse_date(_extract_value_by_label(soup, {"DOB:", "DOB", "Date of Birth:"}))
    height_cm = parse_height_to_cm(_extract_value_by_label(soup, {"Height:", "Height"}))
    reach_cm = parse_reach_to_cm(_extract_value_by_label(soup, {"Reach:", "Reach"}))
    stance = _extract_value_by_label(soup, {"STANCE:", "Stance:", "Stance"})
    weight_grams = parse_weight_to_grams(_extract_value_by_label(soup, {"Weight:", "Weight"}))
    wins, losses, draws = parse_record(_extract_record_text(soup))
    return FighterRecord(
        name=name,
        nickname=nickname,
        nationality=nationality,
        birth_date=birth_date,
        height_cm=height_cm,
        reach_cm=reach_cm,
        stance=stance,
        weight_grams=weight_grams,
        wins=wins,
        losses=losses,
        draws=draws,
        source=settings.source_name,
        source_id=source_id_from_url(url),
    )


def _extract_name(soup: BeautifulSoup) -> str:
    for selector in ("span.b-content__title-highlight", "h2.b-content__title", "h1"):
        node = soup.select_one(selector)
        text = clean_text(node.get_text(" ", strip=True) if node else None)
        if text:
            return text
    raise ValueError("Unable to parse fighter name.")


def _extract_record_text(soup: BeautifulSoup) -> str | None:
    for selector in ("span.b-content__title-record", ".b-content__title-record", "li.b-list__box-list-item"):
        for node in soup.select(selector):
            text = clean_text(node.get_text(" ", strip=True))
            if text and "Record:" in text:
                return text.split("Record:", 1)[-1].strip()
    return None


def _extract_value_by_label(soup: BeautifulSoup, labels: set[str]) -> str | None:
    for item in soup.select("li, p, span"):
        text = clean_text(item.get_text(" ", strip=True))
        if not text:
            continue
        for label in labels:
            if text.startswith(label):
                return clean_text(text[len(label):])
    return None