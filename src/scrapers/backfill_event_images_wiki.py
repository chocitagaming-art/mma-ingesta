"""Second-pass poster backfill: fill events.image_url from WIKIPEDIA for the past
events ufc.com cannot cover (old Fight Nights, "UFC on FOX/ESPN", etc.).

ufc.com drops the pages of old cards, and ESPN serves no event poster, so the
remaining gap is filled from the promotional poster in each event's English
Wikipedia infobox. The owner authorised using these posters even though Wikipedia
tags them non-free (fair use) — they are the same official UFC promo art we
already show from ufc.com.

RUN ORDER: always after `backfill_event_images --apply`. set_event_image is
first-writer-wins, so an official ufc.com poster (written first) always beats the
Wikipedia one; this pass only fills what is still empty.

MATCH GUARD (conservative, avoids mis-attributing a poster):
  - numbered event ("UFC 100")  -> the article's "UFC <N>" number must match exactly
  - otherwise                   -> >=2 shared headliner tokens (both surnames of "X vs Y")
The infobox lead <img> of a UFC event article is the poster; we de-thumb it to the
uploaded file, and reject SVG/flag/icon/non-wikimedia images.

Usage:
    python -m src.scrapers.backfill_event_images_wiki               # dry-run
    python -m src.scrapers.backfill_event_images_wiki --limit 20    # sample
    python -m src.scrapers.backfill_event_images_wiki --apply       # persist
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from typing import Callable

import requests
from bs4 import BeautifulSoup

from .backfill_event_images import TargetEvent, get_target_events, new_session
from .config import Settings, get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import strip_accents
from .repositories.events import set_event_image

LOGGER = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_ARTICLE = "https://en.wikipedia.org/wiki/{title}"
# Wikipedia asks for a descriptive User-Agent with contact info.
WIKI_HEADERS = {
    "User-Agent": "mma-ingesta/1.0 (poster backfill; contact gpicomanville@gmail.com)"
}

_UFC_NUM_RE = re.compile(r"\bufc\s*(\d{2,4})\b", re.IGNORECASE)
_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5}


def _norm(text: str | None) -> str:
    return strip_accents((text or "").lower())


def _sequel_ordinal(text: str | None) -> int | None:
    """Trailing rematch marker (2, 3, II, III...) of a card name, else None.
    'Condit vs Kampmann 2' -> 2 ; 'Belfort vs. Henderson III' -> 3 ; plain -> None.
    A trailing multi-digit like 'Fight Night 31' is NOT a sequel ordinal."""
    tokens = re.sub(r"[^a-z0-9 ]", " ", _norm(text)).split()
    if not tokens:
        return None
    last = tokens[-1]
    if last in _ROMAN:
        return _ROMAN[last]
    if last.isdigit() and len(last) == 1 and last != "1":
        return int(last)
    return None


def _headliner_surnames(event_name: str | None) -> list[str]:
    """Both sides' surnames from an 'A vs B' headliner (last word of each side).

    Robust to multi-word surnames ('Dos Santos' -> 'santos') and ignores the
    sequel marker. Returns [] when the name is not an 'X vs Y' card (e.g. 'Fight
    for the Troops'), which the caller treats as 'cannot confidently match'."""
    m = re.search(r"([a-z][a-z' .-]*?)\bvs\b[.\s]*([a-z][a-z' .-]*)", _norm(event_name))
    if not m:
        return []

    def last(side: str) -> str | None:
        words = [w for w in re.sub(r"[^a-z ]", " ", side).split() if len(w) > 1]
        # Drop a trailing roman rematch marker so 'Barao II' -> 'barao' (arabic
        # markers are already gone, stripped by the non-letter substitution above).
        while words and words[-1] in _ROMAN:
            words.pop()
        return words[-1] if words else None

    return [w for w in (last(m.group(1)), last(m.group(2))) if w]


def title_matches(event_name: str | None, article_title: str | None) -> bool:
    """True only if the Wikipedia article is confidently THIS UFC event.

    Numbered PPVs share the exact 'UFC <N>'; every other card must share BOTH
    headliner surnames AND the same sequel ordinal. Requiring BOTH surnames rejects
    a single fighter's article (e.g. 'Junior dos Santos' lacks the opponent's name)
    and stops 'Lewis vs Dos Santos' taking the 'Blaydes vs Dos Santos' poster; the
    ordinal check stops 'Kampmann 1' taking the 'Kampmann 2' poster. A non-'X vs Y'
    card (e.g. 'Fight for the Troops') has nothing to disambiguate on -> no match."""
    ev_num = _UFC_NUM_RE.search(event_name or "")
    if ev_num:
        art_num = _UFC_NUM_RE.search(article_title or "")
        return bool(art_num) and ev_num.group(1) == art_num.group(1)
    surnames = _headliner_surnames(event_name)
    # Need two DISTINCT surnames to disambiguate: 'Cowboy vs Cowboy' (both fighters
    # nicknamed Cowboy) would otherwise match any other 'Cowboy vs ...' card.
    if len(set(surnames)) < 2:
        return False
    title_tokens = set(re.sub(r"[^a-z0-9 ]", " ", _norm(article_title)).split())
    if not all(sn in title_tokens for sn in surnames):
        return False
    return _sequel_ordinal(event_name) == _sequel_ordinal(article_title)


def _dethumb(url: str) -> str:
    """Turn a Wikipedia thumbnail URL into the uploaded file URL.

    .../thumb/d/d7/Name.jpeg/250px-Name.jpeg -> .../d/d7/Name.jpeg
    A non-thumbnail URL is returned unchanged."""
    if "/thumb/" in url:
        head = url.rsplit("/", 1)[0]  # drop the '<size>px-Name' segment
        return head.replace("/thumb/", "/")
    return url


def poster_from_infobox(html: str) -> str | None:
    """Poster URL from an article's infobox lead image, or None.

    Rejects anything that isn't a real uploaded raster on upload.wikimedia.org
    (SVG flags/icons, tiny images, off-site images)."""
    soup = BeautifulSoup(html, "lxml")
    box = soup.select_one("table.infobox")
    if box is None:
        return None
    img = box.select_one("img")
    if img is None:
        return None
    src = img.get("src") or ""
    if src.startswith("//"):
        src = "https:" + src
    if "upload.wikimedia.org" not in src:
        return None
    low = src.lower().split("?")[0]
    if low.endswith(".svg") or ".svg/" in src.lower():
        return None
    width = img.get("width")
    if width:
        try:
            if int(width) < 80:  # flags/icons; posters render ~250px wide
                return None
        except (ValueError, TypeError):
            pass
    return _dethumb(src)


def search_titles(session: requests.Session, name: str, limit: int = 6) -> list[str]:
    try:
        resp = session.get(
            WIKI_API,
            params={
                "action": "query", "list": "search", "srsearch": name,
                "srlimit": limit, "format": "json",
            },
            headers=WIKI_HEADERS,
            timeout=25,
        )
        resp.raise_for_status()
        hits = resp.json().get("query", {}).get("search", [])
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("wiki search failed for %r: %s", name, exc)
        return []
    return [h["title"] for h in hits if h.get("title")]


def fetch_article_html(session: requests.Session, title: str) -> str | None:
    url = WIKI_ARTICLE.format(title=title.replace(" ", "_"))
    try:
        resp = session.get(url, headers=WIKI_HEADERS, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.info("wiki article fetch failed for %r: %s", title, exc)
        return None
    resp.encoding = "utf-8"
    return resp.text


def fetch_wiki_poster(
    session: requests.Session, settings: Settings, name: str
) -> str | None:
    """Search Wikipedia for the event and return the infobox poster, or None."""
    time.sleep(settings.request_delay_seconds)
    for title in search_titles(session, name):
        if not title_matches(name, title):
            continue
        time.sleep(settings.request_delay_seconds)
        html = fetch_article_html(session, title)
        if not html:
            continue
        poster = poster_from_infobox(html)
        if poster:
            LOGGER.info("event %r -> wiki %r -> %s", name, title, poster)
            return poster
    return None


def run(
    connection,
    *,
    apply: bool,
    resolve: Callable[[TargetEvent], str | None],
    limit: int | None = None,
    record_sink: list | None = None,
) -> Counter:
    """Resolve posters from Wikipedia for past events still missing one."""
    counts: Counter = Counter()
    events = get_target_events(connection)
    counts["targets"] = len(events)
    # End the read transaction before the slow HTTP loop (see backfill_event_images).
    connection.rollback()
    processed = 0
    for event in events:
        if limit is not None and processed >= limit:
            break
        processed += 1
        # One bad event (a transient Wikipedia error or a Neon reset on the write)
        # must not abort the whole gap-fill batch.
        try:
            poster = resolve(event)
            if record_sink is not None:
                record_sink.append({
                    "id": event.id, "name": event.name,
                    "event_date": str(event.event_date),
                    "resolved": bool(poster), "image_url": poster,
                })
            if not poster:
                counts["no_image"] += 1
                continue
            counts["resolved"] += 1
            if apply:
                wrote = set_event_image(connection, event.id, poster)
                # Always close the txn (even a 0-row UPDATE) to avoid leaving the
                # connection idle-in-transaction across the loop.
                connection.commit()
                if wrote:
                    counts["written"] += 1
        except Exception as exc:  # noqa: BLE001 - keep going past a single failure
            counts["errors"] += 1
            LOGGER.warning("event %d %r: skipped on error: %s", event.id, event.name, exc)
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Backfill event posters from Wikipedia for the ufc.com gap."
    )
    parser.add_argument("--apply", action="store_true", help="Persist posters (default: dry-run).")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many candidates.")
    parser.add_argument("--report", default=None, help="Write a per-event JSON report to this path.")
    args = parser.parse_args()

    settings = get_settings()
    session = new_session()

    def resolve(event: TargetEvent) -> str | None:
        return fetch_wiki_poster(session, settings, event.name)

    records: list | None = [] if args.report else None
    with connect(settings.database_url) as connection:
        counts = run(
            connection, apply=args.apply, resolve=resolve,
            limit=args.limit, record_sink=records,
        )

    if args.report and records is not None:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, ensure_ascii=False)
        print(f"Report written to {args.report} ({len(records)} events).")

    summary = {k: counts.get(k, 0) for k in ("targets", "resolved", "no_image", "errors", "written")}
    print(json.dumps(summary, indent=2))
    if not args.apply:
        print("Dry-run: nothing written. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
