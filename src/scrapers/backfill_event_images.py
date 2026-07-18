"""One-off: backfill events.image_url (poster) for PAST events from ufc.com.

The upcoming-events flow scrapes a poster while a card is upcoming, but the
~777 historical events imported from ufcstats have none (only 3 carry a poster),
so the "Pasados" cards fall back to a placeholder icon. ufc.com still serves the
poster of old cards (verified back to UFC 200), so we re-derive each event's
ufc.com slug and re-parse the poster with the same helper the live scraper uses
(`_parse_detail_image`).

SLUG DERIVATION (per event):
  - source='ufc.com' + source_id      -> that stored slug            (strategy 'slug')
  - "UFC <N>" in name                 -> "ufc-<N>"                    (strategy 'ppv')
  - "Fight Night" in name + a date    -> "ufc-fight-night-<mon>-<dd>-<yyyy>" ('fn')
  - anything else (TUF finales, old "UFC on FOX/ESPN" city slugs)     -> NOT derivable
Old Fight Nights used a city/broadcaster slug that is not derivable from the date;
those simply stay without a poster (reported, never guessed).

SOFT-404 GUARD: a bad slug returns HTTP 200 but REDIRECTS to /search (verified),
so we reject any response whose final URL left /event/. Combined with requiring an
actual poster on the page, a wrong/generic image can never be written.

Writes are FIRST-WRITER-WINS (`set_event_image`): a stored poster is never
overwritten and re-runs are idempotent. Rate-limited via settings.request_delay.

Usage:
    python -m src.scrapers.backfill_event_images                 # dry-run, all strategies
    python -m src.scrapers.backfill_event_images --strategy slug ppv   # subset
    python -m src.scrapers.backfill_event_images --limit 10      # first 10 candidates
    python -m src.scrapers.backfill_event_images --apply         # persist posters
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Callable

import requests
from bs4 import BeautifulSoup

from .config import Settings, get_settings
from .db import connect
from .logging_config import configure_logging
from .rankings import BROWSER_HEADERS
from .repositories.events import set_event_image
from .upcoming_events import _parse_detail_image

LOGGER = logging.getLogger(__name__)

EVENT_URL = "https://www.ufc.com/event/{slug}"
STRATEGIES = ("slug", "ppv", "fn")

# Locale-independent month names for the date-based Fight Night slug.
MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

_PPV_RE = re.compile(r"\bUFC\s+(\d+)\b", re.IGNORECASE)
_FN_RE = re.compile(r"fight\s*night", re.IGNORECASE)


@dataclass(frozen=True)
class TargetEvent:
    id: int
    name: str
    source: str | None
    source_id: str | None
    event_date: date | None


def derive_slug(
    name: str | None,
    source: str | None,
    source_id: str | None,
    event_date: date | None,
) -> tuple[str | None, str]:
    """Return (ufc.com slug, strategy) for an event, or (None, 'undeivable').

    Order matters: a stored ufc.com slug is authoritative; then numbered PPVs
    ('UFC 325' -> 'ufc-325'); then date-derived Fight Nights. Numbered events are
    checked before Fight Night so 'UFC 325' never falls through to the FN branch.
    """
    if source == "ufc.com" and source_id:
        return source_id, "slug"
    ppv = _PPV_RE.search(name or "")
    if ppv:
        return f"ufc-{ppv.group(1)}", "ppv"
    if event_date is not None and _FN_RE.search(name or ""):
        slug = (
            f"ufc-fight-night-{MONTHS[event_date.month - 1]}"
            f"-{event_date.day:02d}-{event_date.year}"
        )
        return slug, "fn"
    return None, "undeivable"


def get_target_events(connection) -> list[TargetEvent]:
    """PAST events (date already passed) that still have no poster, recent first."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, source, source_id, event_date
            FROM events
            WHERE event_date < CURRENT_DATE
              AND (image_url IS NULL OR image_url = '')
            ORDER BY event_date DESC
            """
        )
        return [
            TargetEvent(int(r[0]), str(r[1]), r[2], r[3], r[4])
            for r in cursor.fetchall()
        ]


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session


def fetch_poster(
    session: requests.Session, settings: Settings, slug: str
) -> str | None:
    """Poster URL for a ufc.com event slug, or None if the event page is missing.

    Rate-limited (settings.request_delay_seconds). A bad slug redirects to
    /search on ufc.com (HTTP 200), so any response that left /event/ is treated as
    "no such event" — this is what prevents a generic/search image being stored.
    """
    time.sleep(settings.request_delay_seconds)
    url = EVENT_URL.format(slug=slug)
    try:
        resp = session.get(url, timeout=settings.request_timeout_seconds)
    except requests.RequestException as exc:
        LOGGER.warning("slug %s -> request error: %s", slug, exc)
        return None
    if resp.status_code == 404 or not resp.ok:
        LOGGER.info("slug %s -> HTTP %s", slug, resp.status_code)
        return None
    if "/event/" not in resp.url:
        LOGGER.info("slug %s -> redirected to %s (no such event)", slug, resp.url)
        return None
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")
    return _parse_detail_image(soup)


def run(
    connection,
    *,
    apply: bool,
    fetch_image: Callable[[str], str | None],
    strategies: tuple[str, ...] = STRATEGIES,
    limit: int | None = None,
    record_sink: list | None = None,
) -> Counter:
    """Resolve and (optionally) store posters for past events missing one.

    When ``record_sink`` is given, every attempted/undeivable event is appended
    as a dict (id, name, strategy, slug, resolved, image_url) so a caller can see
    the exact gap left for a secondary source (ESPN/Wikipedia)."""
    counts: Counter = Counter()
    events = get_target_events(connection)
    counts["targets"] = len(events)
    # The worklist is fully materialized (fetchall) above; end the read
    # transaction NOW so the connection is not left "idle in transaction" holding
    # a snapshot / locks during the slow (~15-20 min) HTTP loop below. Otherwise
    # it would block autovacuum on `events`, which the live crons keep writing to.
    # Mirrors backfill_news_images' "close the read txn before the network loop".
    connection.rollback()
    processed = 0
    for event in events:
        slug, strategy = derive_slug(
            event.name, event.source, event.source_id, event.event_date
        )
        if strategy == "undeivable":
            counts["skip_undeivable"] += 1
            if record_sink is not None:
                record_sink.append(_record(event, "undeivable", None, None))
            continue
        if strategy not in strategies:
            counts["skip_filtered"] += 1
            continue
        if limit is not None and processed >= limit:
            break
        processed += 1
        counts[f"try_{strategy}"] += 1
        # One bad event (an unexpected parse error, or a Neon connection reset on
        # the write after minutes idle) must not abort the ~700-event batch.
        try:
            image_url = fetch_image(slug)  # type: ignore[arg-type]
            if record_sink is not None:
                record_sink.append(_record(event, strategy, slug, image_url))
            if not image_url:
                counts["no_image"] += 1
                continue
            counts["resolved"] += 1
            counts[f"resolved_{strategy}"] += 1
            LOGGER.info("event %d %r -> %s", event.id, event.name, image_url)
            if apply:
                wrote = set_event_image(connection, event.id, image_url)
                # Always close the txn, even when the UPDATE matched 0 rows, so the
                # connection is never left idle-in-transaction across the loop.
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


def _record(event: TargetEvent, strategy: str, slug: str | None, image_url: str | None) -> dict:
    return {
        "id": event.id,
        "name": event.name,
        "event_date": str(event.event_date),
        "strategy": strategy,
        "slug": slug,
        "resolved": bool(image_url),
        "image_url": image_url,
    }


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Backfill posters (events.image_url) for past events from ufc.com."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Persist posters (default: dry-run)."
    )
    parser.add_argument(
        "--strategy",
        nargs="+",
        choices=STRATEGIES,
        default=list(STRATEGIES),
        help="Which slug-derivation strategies to run (default: all).",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Process at most this many candidates."
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Write a per-event JSON report (id/strategy/slug/resolved) to this path.",
    )
    args = parser.parse_args()

    settings = get_settings()
    session = new_session()

    def fetch_image(slug: str) -> str | None:
        return fetch_poster(session, settings, slug)

    records: list | None = [] if args.report else None
    with connect(settings.database_url) as connection:
        counts = run(
            connection,
            apply=args.apply,
            fetch_image=fetch_image,
            strategies=tuple(args.strategy),
            limit=args.limit,
            record_sink=records,
        )

    if args.report and records is not None:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, ensure_ascii=False)
        print(f"Report written to {args.report} ({len(records)} events).")

    summary = {
        "targets": counts.get("targets", 0),
        "skip_undeivable": counts.get("skip_undeivable", 0),
        "skip_filtered": counts.get("skip_filtered", 0),
        "try_slug": counts.get("try_slug", 0),
        "try_ppv": counts.get("try_ppv", 0),
        "try_fn": counts.get("try_fn", 0),
        "resolved": counts.get("resolved", 0),
        "resolved_slug": counts.get("resolved_slug", 0),
        "resolved_ppv": counts.get("resolved_ppv", 0),
        "resolved_fn": counts.get("resolved_fn", 0),
        "no_image": counts.get("no_image", 0),
        "errors": counts.get("errors", 0),
        "written": counts.get("written", 0),
    }
    print(json.dumps(summary, indent=2))
    if not args.apply:
        print("Dry-run: nothing written. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
