from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Settings
from ..models import EventRecord
from ..utils import clean_text, parse_date


BASE_URL = "http://ufcstats.com"


@dataclass(frozen=True)
class EventPageRecord:
    event: EventRecord
    detail_url: str


def parse_events_index(soup: BeautifulSoup, settings: Settings) -> list[EventPageRecord]:
    records: list[EventPageRecord] = []
    seen_urls: set[str] = set()
    for row in soup.select("tbody tr"):
        link = row.select_one("a[href*='/event-details/']")
        if not link:
            continue
        href = link.get("href")
        if not href:
            continue
        detail_url = urljoin(BASE_URL, href)
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        cells = row.select("td")
        name = clean_text(link.get_text(" ", strip=True))
        date_node = row.select_one("span.b-statistics__date")
        date_text = clean_text(date_node.get_text(" ", strip=True) if date_node else None)
        location_text = clean_text(cells[1].get_text(" ", strip=True) if len(cells) >= 2 else None)
        if not name:
            continue
        records.append(
            EventPageRecord(
                event=EventRecord(
                    name=name,
                    event_date=parse_date(date_text),
                    location=location_text,
                    promotion_id=settings.promotion_id_ufc,
                ),
                detail_url=detail_url,
            )
        )
    return records