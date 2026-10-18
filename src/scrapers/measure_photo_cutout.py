"""Measure whether each Hall of Fame photo is a cutout (transparent background).

The Salón paints a card two ways: "en relieve" when the photo is a cutout (the
fighter pops out of the frame) or "enmarcada" (gold frame) when it has a
background. It cannot be guessed from the domain — measured 18-oct-2026, the
"ufc.com = cutout" rule got Dan Severn (gallery photo WITH background) and Forrest
Griffin (local file that IS a cutout) wrong — so we measure the alpha channel and
store the verdict per URL in ``photo_cutout`` (migration 031). The web LEFT JOINs
that table; a URL with no row is painted framed, which is the safe default.

Which URLs: exactly the ones the Salón shows from the DB —
COALESCE(hall_of_fame.photo_url, fighters.headshot_url) for each person and the
headshot_url of both corners of each Fight Wing bout. Local paths ('/fighters/…')
are fetched from https://mmastatus.app + path. Only the URLs with no row, a row
older than 30 days or a failed last fetch are measured; then upsert.

A failed fetch never throws away a good verdict: a timeout, a 5xx, a 429/403 or
a 200 that is not an image keeps the previous is_cutout/width/height/checked_at
(only http_status records the failure, so the next pass retries it). Only a
404/410 — the photo is gone — resets the row to framed. And the exit code is 1
when any URL failed for a reason other than 404/410, so the workflow goes red
instead of hiding that every download was blocked.

🪤 The key is the EXACT string stored in the DB: 643 headshot_url carry a literal
'&amp;' and the browser requests that very string. Never normalise it.

    python -m src.scrapers.measure_photo_cutout            # measure and upsert
    python -m src.scrapers.measure_photo_cutout --dry-run  # measure, print, write nothing
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import requests
from PIL import Image

from .config import get_settings
from .db import connect
from .logging_config import configure_logging

LOGGER = logging.getLogger(__name__)

SITE_ORIGIN = "https://mmastatus.app"
RECHECK_AFTER_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_DELAY_SECONDS = 0.5
# Cutout rule (validated on the 55 Salón photos, 18-oct-2026: the 51 cutouts
# score 0.958-1.0 on the ring, Dan Severn 0.269, photos without alpha 0).
RING_TRANSPARENT_MIN = 0.90

# Browser-like UA, as the ufc.com enrichers use. 🪤 But NOT a browser's Accept:
# ufc.com negotiates the format, and with 'image/avif' in Accept it re-encodes
# the PNG as a lossy AVIF whose alpha is noisy at the edges. Measured 18-oct-2026
# on Ronda Rousey's headshot: PNG ring 1.0 (cutout), AVIF ring 0.896 (framed by
# mistake). '*/*' gets the original file, which is what the alpha rule is for.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "*/*",
}

# Every photo URL the Salón paints from the DB: the person (own curated photo
# first, else the linked fighter's headshot) and both Fight Wing corners.
SALON_URLS_SQL = """
    SELECT DISTINCT url FROM (
        SELECT COALESCE(h.photo_url, f.headshot_url) AS url
        FROM hall_of_fame h
        LEFT JOIN fighters f ON f.id = h.fighter_id
        UNION ALL
        SELECT fa.headshot_url FROM hall_of_fame h JOIN fighters fa ON fa.id = h.fighter_a_id
        UNION ALL
        SELECT fb.headshot_url FROM hall_of_fame h JOIN fighters fb ON fb.id = h.fighter_b_id
    ) AS salon
    WHERE url IS NOT NULL AND url <> ''
    ORDER BY url
"""

# HTTP codes that mean "this photo no longer exists": the only failures allowed
# to overwrite a previous verdict, and the only ones that do not turn the run red.
GONE_STATUSES = (404, 410)

# The ones that need (re)measuring: never measured, measured more than
# RECHECK_AFTER_DAYS ago, last fetch not a 200 (a transient error should not
# leave a photo framed for a month), or no dimensions (a 200 that was not a
# readable image — an HTML error page, an empty or truncated body — is a failed
# measurement too).
STALE_SQL = """
    SELECT u.url
    FROM unnest(%s::text[]) AS u(url)
    LEFT JOIN photo_cutout pc ON pc.url = u.url
    WHERE pc.url IS NULL
       OR pc.checked_at < NOW() - make_interval(days => %s)
       OR pc.http_status IS DISTINCT FROM 200
       OR pc.width IS NULL
    ORDER BY u.url
"""

# A measurement is VALID when it produced dimensions (HTTP 200 + readable image).
# On conflict: a valid one, or a 404/410 (photo gone), replaces the verdict; any
# other failure keeps the previous is_cutout/width/height/checked_at and only
# records its http_status. Keeping checked_at is what makes the row stale again
# on the next pass even when the failure was a 200 with a non-image body.
UPSERT_SQL = """
    INSERT INTO photo_cutout (url, is_cutout, width, height, http_status, checked_at)
    VALUES (%s, %s, %s, %s, %s, NOW())
    ON CONFLICT (url) DO UPDATE SET
        is_cutout   = CASE WHEN EXCLUDED.width IS NOT NULL OR EXCLUDED.http_status IN (404, 410)
                           THEN EXCLUDED.is_cutout ELSE photo_cutout.is_cutout END,
        width       = CASE WHEN EXCLUDED.width IS NOT NULL OR EXCLUDED.http_status IN (404, 410)
                           THEN EXCLUDED.width ELSE photo_cutout.width END,
        height      = CASE WHEN EXCLUDED.width IS NOT NULL OR EXCLUDED.http_status IN (404, 410)
                           THEN EXCLUDED.height ELSE photo_cutout.height END,
        http_status = EXCLUDED.http_status,
        checked_at  = CASE WHEN EXCLUDED.width IS NOT NULL OR EXCLUDED.http_status IN (404, 410)
                           THEN EXCLUDED.checked_at ELSE photo_cutout.checked_at END
"""


@dataclass(frozen=True)
class Measurement:
    url: str
    is_cutout: bool
    width: int | None
    height: int | None
    http_status: int | None

    @property
    def valid(self) -> bool:
        """HTTP 200 and a readable image: the only result that is a real verdict."""
        return self.width is not None

    @property
    def gone(self) -> bool:
        return self.http_status in GONE_STATUSES


# --------------------------------------------------------------- pure helpers


def is_cutout(image: Image.Image) -> bool:
    """True when the image has a transparent background around the subject.

    Rule: RGBA, alpha 0 in BOTH top corners and >= 90 % of fully transparent
    pixels on the top row plus the left and right columns. The bottom row does
    NOT count: shoulders touch it in every headshot cutout (Bisping, Liddell
    measured with alpha 255 along the bottom)."""
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode in ("P", "L", "RGB") and "transparency" in image.info
    )
    if not has_alpha:
        return False
    alpha = image.convert("RGBA").getchannel("A")
    width, height = alpha.size
    if width < 2 or height < 2:
        return False
    if alpha.getpixel((0, 0)) != 0 or alpha.getpixel((width - 1, 0)) != 0:
        return False
    top = alpha.crop((0, 0, width, 1)).tobytes()
    left = alpha.crop((0, 0, 1, height)).tobytes()
    right = alpha.crop((width - 1, 0, width, height)).tobytes()
    ring = top + left + right
    transparent = ring.count(0)
    return transparent / len(ring) >= RING_TRANSPARENT_MIN


def fetch_url_for(url: str) -> str:
    """The URL to request for a stored photo URL: local paths go to the site.

    The stored string is otherwise requested VERBATIM ('&amp;' included), which is
    what the browser does with it."""
    if url.startswith("/"):
        return SITE_ORIGIN + url
    return url


def measure_bytes(url: str, status: int | None, content: bytes | None) -> Measurement:
    """Verdict for one downloaded image. Anything that is not a readable image
    (HTTP error, HTML error page, truncated file) comes back as not-a-cutout with
    NO dimensions: a failed measurement (``valid`` False), which the upsert does
    not let overwrite a previous good verdict unless it is a 404/410."""
    if status != 200 or not content:
        return Measurement(url, False, None, None, status)
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            width, height = image.size
            return Measurement(url, is_cutout(image), width, height, status)
    except Exception as exc:  # noqa: BLE001 - an unreadable image is just "framed"
        LOGGER.warning("Unreadable image %s: %s", url, exc)
        return Measurement(url, False, None, None, status)


# ------------------------------------------------------------------ IO layers


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_HEADERS)
    return session


def download(session: requests.Session, url: str) -> tuple[int | None, bytes | None]:
    try:
        response = session.get(fetch_url_for(url), timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        LOGGER.warning("Fetch failed for %s: %s", url, exc)
        return None, None
    return response.status_code, (response.content if response.ok else None)


def salon_urls(connection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(SALON_URLS_SQL)
        return [str(row[0]) for row in cursor.fetchall()]


def stale_urls(connection, urls: list[str], recheck_after_days: int = RECHECK_AFTER_DAYS) -> list[str]:
    if not urls:
        return []
    with connection.cursor() as cursor:
        cursor.execute(STALE_SQL, (urls, recheck_after_days))
        return [str(row[0]) for row in cursor.fetchall()]


def measure_all(
    urls: Iterable[str],
    fetch: Callable[[str], tuple[int | None, bytes | None]],
    sleep: Callable[[float], None] = time.sleep,
) -> list[Measurement]:
    measurements: list[Measurement] = []
    for position, url in enumerate(urls):
        if position:
            sleep(REQUEST_DELAY_SECONDS)
        status, content = fetch(url)
        measurement = measure_bytes(url, status, content)
        LOGGER.info(
            "%s %s %sx%s HTTP %s",
            "FALLO   " if not measurement.valid else "RECORTE " if measurement.is_cutout else "ENMARCAR",
            url, measurement.width, measurement.height, measurement.http_status,
        )
        measurements.append(measurement)
    return measurements


def upsert(connection, measurements: list[Measurement]) -> int:
    with connection.cursor() as cursor:
        for m in measurements:
            cursor.execute(UPSERT_SQL, (m.url, m.is_cutout, m.width, m.height, m.http_status))
    connection.commit()
    return len(measurements)


def run(connection, fetch, *, dry_run: bool = False, sleep=time.sleep) -> dict[str, object]:
    urls = salon_urls(connection)
    todo = stale_urls(connection, urls)
    # Close the read transaction before the slow downloads: Neon kills a session
    # left idle INSIDE a transaction for more than 5 minutes.
    connection.rollback()
    measurements = measure_all(todo, fetch, sleep=sleep)
    written = 0 if dry_run else upsert(connection, measurements)
    return {
        "salon_urls": len(urls),
        "measured": len(measurements),
        "cutouts": sum(1 for m in measurements if m.is_cutout),
        "framed": sum(1 for m in measurements if m.valid and not m.is_cutout),
        # Every URL without a real verdict (non-200 OR a 200 that was no image)...
        "fetch_errors": [m.url for m in measurements if not m.valid],
        # ...of which these are photos that no longer exist (404/410).
        "gone": [m.url for m in measurements if m.gone],
        "written": written,
        "dry_run": dry_run,
    }


def exit_code(summary: dict[str, object]) -> int:
    """1 when a URL failed for a reason other than 404/410 (timeout, 5xx, 429,
    403, a non-image body): the job must go red so someone re-runs it — the
    workflow is manual, nothing retries on its own. A 404/410 is a dead link in
    the data, not a failed run, so it is reported but does not fail."""
    gone = set(summary["gone"])  # type: ignore[arg-type]
    retryable = [url for url in summary["fetch_errors"] if url not in gone]  # type: ignore[union-attr]
    if retryable:
        LOGGER.error("%d photo(s) could not be measured; re-run to retry: %s", len(retryable), retryable)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="measure and print, write nothing")
    args = parser.parse_args(argv)

    configure_logging()
    settings = get_settings()
    session = build_session()
    with connect(settings.database_url) as connection:
        summary = run(connection, lambda url: download(session, url), dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
