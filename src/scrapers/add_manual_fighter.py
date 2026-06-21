"""Add a manual (Tapology-sourced) fighter and wire them into the site.

Some regional debutants on upcoming cards have no profile on ESPN or UFC (our two
automated sources), so they import as a name-only bout slot: initials, 0-0-0, no
photo. Tapology has them, but it sits behind a Cloudflare Turnstile challenge that
automation cannot pass (confirmed: even a human clicking the checkbox inside an
automated browser loops forever). So this data is entered by hand, read from
Tapology in a normal browser. This streamlines the two recurring chores:

  1. DB row + bout link  - upsert a source='manual' fighter with the W-L-D and
     measures you read off Tapology, and link the matching name-only bout slot(s)
     in upcoming events (so the card shows the record + flag instead of 0-0-0).
  2. Photo               - Tapology blocks hotlinking too, so you download the
     headshot in your browser; pass --photo <file> and this copies it into the
     frontend's public/fighters/ and maps it in local-headshots.ts (the fallback
     used when the DB has no headshot_url).

Modes:
  default        upsert the manual fighter (+ --link-upcoming to link bouts), and
                 install --photo if given.
  --photo-only   skip the DB entirely; just install the photo + mapping. Use when
                 the fighter already has a row (ESPN/UFC) and only the photo is
                 missing (the Hasanov / Cepo case).

Idempotent: the fighter is keyed by (source='manual', source_id=<name slug>), so
re-running updates in place instead of duplicating; the photo mapping is skipped
if already present. Unlike the scrapers (which only fill empty fields), the record
and measures you pass here are authoritative manual entries and are written as-is.

Usage (you run this; the default mode writes to the DB):
    # preview everything, no writes:
    python -m src.scrapers.add_manual_fighter --name "Gable Steveson" --record 3-0-0 \
        --nationality "United States" --link-upcoming --dry-run

    # create the row + link the bout:
    python -m src.scrapers.add_manual_fighter --name "Gable Steveson" --record 3-0-0 \
        --nationality "United States" --link-upcoming

    # same, and also install a downloaded photo:
    python -m src.scrapers.add_manual_fighter --name "Gable Steveson" --record 3-0-0 \
        --link-upcoming --photo "C:/Users/gpico/Downloads/steveson.webp"

    # just a photo for a fighter that already has a row:
    python -m src.scrapers.add_manual_fighter --name "Farman Hasanov" --photo-only \
        --photo "C:/Users/gpico/Downloads/hasanov.jpg"
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from pathlib import Path

from .config import get_settings
from .db import connect
from .enrich_photos_ufc import slugify
from .enrich_ranked import _fold
from .link_upcoming_fighters import _get_unlinked_slots
from .logging_config import configure_logging
from .models import FighterRecord
from .repositories.fighters import upsert_fighter

LOGGER = logging.getLogger(__name__)

MANUAL_SOURCE = "manual"
LB_TO_G = 453.59237
_RECORD_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s*$")
# Frontend layout (mma-app is a sibling repo of mma-ingesta).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]          # .../mma-ingesta
_DEFAULT_FRONTEND_DIR = _PROJECT_ROOT.parent / "mma-app"


def _parse_record(text: str) -> tuple[int, int, int]:
    match = _RECORD_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"--record must look like W-L-D (e.g. 3-0-0), got {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


# --------------------------------------------------------------------------- DB


def _link_slots(connection, fighter_id: int, name: str, dry_run: bool) -> list[int]:
    """Link unlinked upcoming bout slots whose corner name matches `name` (folded)."""
    target = _fold(name)
    matched = [(bout_id, corner) for bout_id, corner, slot_name in _get_unlinked_slots(connection)
               if _fold(slot_name) == target]
    if dry_run:
        return [bout_id for bout_id, _ in matched]
    for bout_id, corner in matched:
        column = "fighter_red_id" if corner == "red" else "fighter_blue_id"
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE fights SET {column} = %s WHERE id = %s", (fighter_id, bout_id))
    return [bout_id for bout_id, _ in matched]


def upsert_manual_fighter(
    *,
    name: str,
    record: tuple[int, int, int],
    nickname: str | None = None,
    nationality: str | None = None,
    height_cm: float | None = None,
    reach_cm: float | None = None,
    weight_lb: float | None = None,
    stance: str | None = None,
    link_upcoming: bool = False,
    dry_run: bool = False,
) -> dict:
    settings = get_settings()
    weight_grams = int(round(weight_lb * LB_TO_G)) if weight_lb else None
    fighter = FighterRecord(
        name=name,
        nickname=nickname,
        headshot_url=None,  # manual fighters render via the local-headshots fallback
        nationality=nationality,
        birth_date=None,
        height_cm=height_cm,
        reach_cm=reach_cm,
        stance=stance,
        weight_grams=weight_grams,
        wins=record[0],
        losses=record[1],
        draws=record[2],
        source=MANUAL_SOURCE,
        source_id=slugify(name),
    )
    with connect(settings.database_url) as connection:
        if dry_run:
            linked = _link_slots(connection, fighter_id=-1, name=name, dry_run=True) if link_upcoming else []
            connection.rollback()
            return {"fighter_id": None, "source_id": fighter.source_id, "linked_bouts": linked, "dry_run": True}
        fighter_id = upsert_fighter(connection, fighter)
        linked = _link_slots(connection, fighter_id, name, dry_run=False) if link_upcoming else []
        connection.commit()
        return {"fighter_id": fighter_id, "source_id": fighter.source_id, "linked_bouts": linked, "dry_run": False}


# ----------------------------------------------------------------------- photo


def install_photo(name: str, photo: str, frontend_dir: Path, dry_run: bool) -> dict:
    """Copy a downloaded headshot into the frontend and map it in local-headshots.ts."""
    src = Path(photo)
    if not src.is_file():
        raise FileNotFoundError(f"--photo not found: {src}")
    slug = slugify(name)
    ext = src.suffix.lower() or ".jpg"
    rel = f"/fighters/{slug}{ext}"
    key = name.strip().lower()  # mirrors the frontend's name.trim().toLowerCase()
    dest = frontend_dir / "public" / "fighters" / f"{slug}{ext}"
    ts_path = frontend_dir / "src" / "lib" / "local-headshots.ts"

    if not ts_path.is_file():
        raise FileNotFoundError(f"local-headshots.ts not found at {ts_path} (check --frontend-dir)")
    ts_text = ts_path.read_text(encoding="utf-8")
    already_mapped = f'"{key}"' in ts_text

    result = {"dest": str(dest), "mapping": f'"{key}": "{rel}"', "already_mapped": already_mapped, "dry_run": dry_run}
    if dry_run:
        return result

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)

    if not already_mapped:
        new_text, count = _insert_mapping(ts_text, key, rel)
        if count != 1:
            raise RuntimeError(
                "Could not locate the LOCAL_HEADSHOTS object to edit; add this line by hand:\n"
                f'  "{key}": "{rel}",'
            )
        ts_path.write_text(new_text, encoding="utf-8")
    return result


def _insert_mapping(ts_text: str, key: str, rel: str) -> tuple[str, int]:
    """Insert a `"key": "rel",` line just before the LOCAL_HEADSHOTS object's closing brace."""
    pattern = re.compile(r"(const LOCAL_HEADSHOTS\b[^{]*\{.*?)(\n\};)", re.DOTALL)
    line = f'\n  "{key}": "{rel}",'
    new_text, count = pattern.subn(lambda m: m.group(1) + line + m.group(2), ts_text, count=1)
    return new_text, count


# ------------------------------------------------------------------------ main


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Add a manual (Tapology-sourced) fighter and/or photo.")
    parser.add_argument("--name", required=True, help="Full fighter name as it appears on the card.")
    parser.add_argument("--record", type=_parse_record, help="Pro record W-L-D, e.g. 3-0-0.")
    parser.add_argument("--nickname", default=None)
    parser.add_argument("--nationality", default=None, help="Country name, e.g. 'United States'.")
    parser.add_argument("--height-cm", type=float, default=None, dest="height_cm")
    parser.add_argument("--reach-cm", type=float, default=None, dest="reach_cm")
    parser.add_argument("--weight-lb", type=float, default=None, dest="weight_lb")
    parser.add_argument("--stance", default=None, help="Orthodox / Southpaw / Switch.")
    parser.add_argument("--link-upcoming", action="store_true", help="Link matching name-only upcoming bout slots.")
    parser.add_argument("--photo", default=None, help="Path to a headshot you downloaded from Tapology.")
    parser.add_argument("--photo-only", action="store_true", help="Skip the DB; only install the photo + mapping.")
    parser.add_argument("--frontend-dir", default=str(_DEFAULT_FRONTEND_DIR),
                        help="Path to the mma-app repo (default: sibling of mma-ingesta).")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen; no writes.")
    args = parser.parse_args()

    if args.photo_only and not args.photo:
        parser.error("--photo-only requires --photo.")
    if not args.photo_only and args.record is None:
        parser.error("--record is required (or use --photo-only to just install a photo).")

    out: dict = {"name": args.name}
    if not args.photo_only:
        out["fighter"] = upsert_manual_fighter(
            name=args.name,
            record=args.record,
            nickname=args.nickname,
            nationality=args.nationality,
            height_cm=args.height_cm,
            reach_cm=args.reach_cm,
            weight_lb=args.weight_lb,
            stance=args.stance,
            link_upcoming=args.link_upcoming,
            dry_run=args.dry_run,
        )
    if args.photo:
        try:
            out["photo"] = install_photo(args.name, args.photo, Path(args.frontend_dir), args.dry_run)
        except (FileNotFoundError, RuntimeError) as exc:
            out["photo_error"] = str(exc)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    if out.get("photo_error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
