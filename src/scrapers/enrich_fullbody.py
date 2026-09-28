"""Backfill full-body photo, leg reach and gym from ufc.com athlete pages.

Phase 2 (MMA STATUS redesign), expanded: the first pass only walked fighters
whose ``headshot_url`` already pointed at ufc.com (~1,468 confirmed pages), but
plenty of fighters with a ufc.com page carry an ESPN headshot in our DB
(e.g. Conor McGregor, Max Holloway) and were left out. This pass now targets
EVERY fighter still missing ``full_body_url`` / ``leg_reach_cm`` / ``trains_at``
(migration 008) and guesses the athlete page by name slug
(reusing enrich_photos_ufc.resolve_athlete: browser UA + 0.4s delay).

Targets are ordered by product priority so partial passes (--limit) cover the
most visible fighters first:
  1. fighters in the LATEST rankings snapshot,
  2. fighters booked on an upcoming event,
  3. fighters that already have a headshot (any source),
  4. everyone else (mostly historical fighters; many 404 on ufc.com — those are
     counted as ``unresolved``, never treated as an error).
``--solo-ufc`` restores the old ufc.com-headshot-only selection.

ANTI-HOMONYM GUARD — now that slugs are guessed for fighters never confirmed on
ufc.com, slugify(name) can land on a namesake's page. Before persisting
anything, the name the page renders (hero-profile__name) must match the DB
fighter's name after normalization (NFKD accents stripped, casefold, whitespace
collapsed): exact equality or every token of the shorter name contained in the
longer one ('Jose Aldo' vs 'Jose Aldo Junior' passes) — or the hero name must
be EXACTLY the fighter's stored nickname (same normalization, no containment,
at least two words, and a nickname no other fighter shares): ufc.com publishes
some fighters under their nickname (fighters.id=9132 'Valesca Machado' renders
as 'Tina Black'). Every acceptance by nickname is counted (``nickname_match``)
and logged at INFO. The guard runs for ALL
resolved pages — it is a regex over HTML already in memory, so it is free, and
it also protects previously-confirmed fighters against slug redirects — but a
page WITHOUT a hero name is only trusted for fighters whose ufc.com headshot
already proved the page is theirs. Mismatches are skipped and counted as
``name_mismatch`` (warning logged with both names).

WRITE POLICY — writes by default, like the other enrich_* passes
(enrich_photos_ufc, enrich_ranked): every UPDATE goes through
update_fighter_enrichment, which COALESCEs, so it only fills NULL/empty columns
and can never overwrite existing data with NULL or anything else. The
dry-run-by-default pattern (backfill_fight_videos, cleanup_*) is reserved for
passes that write judgement calls or delete rows; this one only copies facts
from a name-verified athlete page. --dry-run is still available for preview.

The selection is idempotent/resumable: only fighters still missing at least one
of the three columns are targeted, so partial passes with --limit resume the gap.

Usage:
    python -m src.scrapers.enrich_fullbody --dry-run --limit 5   # preview, no writes
    python -m src.scrapers.enrich_fullbody --limit 200           # partial pass
    python -m src.scrapers.enrich_fullbody --solo-ufc            # old scope: ufc.com headshots only
    python -m src.scrapers.enrich_fullbody                       # full pass (~2.5k pages, ~25 min)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable

import requests

from .config import get_settings
from .db import connect
from .enrich_photos_ufc import REQUEST_DELAY_SECONDS, AthleteData, resolve_athlete
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_enrichment

LOGGER = logging.getLogger(__name__)

PROGRESS_EVERY = 25

# resolver(session, name) -> AthleteData | None. Injected in tests.
Resolver = Callable[[requests.Session, str], AthleteData | None]

# Target row: (fighter_id, name, nickname, ufc_confirmed) — nickname (None when
# empty or shared with another fighter) is the guard's second way to recognize
# the hero name; ufc_confirmed is True when the stored headshot already comes
# from ufc.com (page identity proven by the previous pass), which relaxes the
# guard only when the page has no hero name.
Target = tuple[int, str, str | None, bool]


def _unique_nickname_sql(column: str) -> str:
    """SELECT-list expression for the guard's nickname: ``column`` itself, or
    NULL when another fighter carries the same nickname (lower-cased and
    trimmed: the same criterion as the exact-duplicate-name exclusion).

    A shared nickname cannot tell whose page this is ('The Sniper' belongs to 5
    fighters in the DB, 'The Great' to 4), so it never reaches the guard. Shared
    by the three ufc.com enrichers. The subquery is uncorrelated, so Postgres
    evaluates it once (hashed SubPlan); ``count(nickname)`` leaves the NULL
    group out of the list.
    """
    return (
        f"CASE WHEN lower(btrim({column})) IN ("
        "SELECT lower(btrim(nickname)) FROM fighters"
        " GROUP BY 1 HAVING count(nickname) > 1"
        f") THEN NULL ELSE {column} END AS nickname"
    )


def _get_target_fighters(
    connection, limit: int | None = None, solo_ufc: bool = False
) -> list[Target]:
    """Fighters still missing at least one of the three Phase-2 columns.

    Default scope: ALL fighters, ordered by priority — latest-rankings-snapshot
    members, then upcoming-event fighters, then fighters with any headshot,
    then the rest (each tier alphabetical). ``solo_ufc`` restores the original
    conservative filter: only fighters whose headshot is already from ufc.com.
    """
    if solo_ufc:
        nickname = _unique_nickname_sql("nickname")
        sql = f"""
            SELECT id, name, {nickname}, TRUE AS ufc_confirmed
            FROM fighters
            WHERE headshot_url ILIKE %s
              AND (full_body_url IS NULL OR leg_reach_cm IS NULL OR trains_at IS NULL)
            ORDER BY name
        """
        params: list = ["%ufc.com%"]
    else:
        nickname = _unique_nickname_sql("f.nickname")
        sql = f"""
            SELECT f.id, f.name, {nickname}, (f.headshot_url ILIKE %s) AS ufc_confirmed
            FROM fighters f
            WHERE f.name IS NOT NULL AND f.name <> ''
              AND (f.full_body_url IS NULL OR f.leg_reach_cm IS NULL OR f.trains_at IS NULL)
            ORDER BY
                EXISTS (
                    SELECT 1 FROM rankings r
                    WHERE r.fighter_id = f.id
                      AND r.snapshot_date = (SELECT MAX(snapshot_date) FROM rankings)
                ) DESC,
                EXISTS (
                    SELECT 1 FROM fights fi
                    JOIN events e ON e.id = fi.event_id
                    WHERE e.status = 'upcoming'
                      AND (fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id)
                ) DESC,
                (NULLIF(f.headshot_url, '') IS NOT NULL) DESC,
                f.name
        """
        params = ["%ufc.com%"]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [
            (int(row[0]), str(row[1]), row[2] or None, bool(row[3]))
            for row in cursor.fetchall()
        ]


# Letras con trazo/barra que NFKD NO descompone: sin transliterar, 'Blachowicz'
# no casaba con 'Błachowicz' y el guard descartaba facts/fotos reales. Cubre
# ł/ø/đ/ħ (y mayúsculas); las demás con diacrítico ya las pliega NFKD.
_STROKE_TRANSLIT = str.maketrans(
    {"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ħ": "h", "Ħ": "H"}
)


def _normalized_name_tokens(name: str) -> list[str]:
    """Lowercase ASCII word tokens: transliterate stroke letters (ł/ø/đ/ħ) that
    NFKD does not decompose, then NFKD with accents stripped, casefolded,
    whitespace/punctuation collapsed ('José  Aldo Jr.' -> ['jose','aldo','jr'])."""
    decomposed = unicodedata.normalize("NFKD", name.translate(_STROKE_TRANSLIT))
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", ascii_name.casefold())


def _names_match(db_name: str, page_name: str) -> bool:
    """True when both names are the same person for guard purposes: identical
    after normalization, or every token of the shorter name appears in the
    longer one ('Jose Aldo' vs 'Jose Aldo Junior', 'Weili Zhang' vs
    'Zhang Weili'). Token containment is set-based, so word order never fails
    a legitimate match."""
    ours = _normalized_name_tokens(db_name)
    theirs = _normalized_name_tokens(page_name)
    if not ours or not theirs:
        return False
    if ours == theirs:
        return True
    shorter, longer = (ours, theirs) if len(ours) <= len(theirs) else (theirs, ours)
    return set(shorter) <= set(longer)


# Minimum words for a nickname to count as a page identity (see below).
_MIN_NICKNAME_WORDS = 2


def _nickname_word_count(nickname: str) -> int:
    """Whitespace-separated words that carry at least one name token. Counted on
    the stored nickname, NOT on the normalized tokens: 'D-Rod' normalizes to
    ['d', 'rod'] but is one word, and a punctuation-only chunk is no word."""
    return sum(1 for word in nickname.split() if _normalized_name_tokens(word))


def _hero_name_matches(db_name: str, nickname: str | None, page_name: str) -> bool:
    """True when the hero name a ufc.com page renders identifies this fighter.

    Shared by the three ufc.com enrichers (fullbody, athlete_stats, facts) so
    their guards cannot drift apart. Two ways in:

    1. By name: ``_names_match`` (exact or subset containment), unchanged.
    2. By nickname: ufc.com publishes some fighters under their nickname —
       fighters.id=9132 is 'Valesca Machado' in our DB, and her canonical page
       /athlete/valesca-machado renders 'Tina Black' in hero-profile__name.
       The nickname path is deliberately STRICTER than the name path:
         - equality of the normalized token lists (same
           ``_normalized_name_tokens``, same order), never containment. With
           containment a generic nickname would open the door to other people:
           'Black' would accept any '<X> Black' page and 'Tina Black Jr' a
           'Tina Black' one;
         - at least two words (``_nickname_word_count``). A one-word nickname
           ('Bones', 'D-Rod') has the same shape as a mononym hero name, so a
           single matching word is too weak to prove whose page this is;
         - the nickname must be ours alone. Two words are no uniqueness proof
           ('The Sniper' is carried by 5 fighters), so the target queries hand
           over NULL for a nickname another fighter shares
           (``_unique_nickname_sql``) and this path never sees it.
    """
    if _names_match(db_name, page_name):
        return True
    if not nickname:
        return False
    if _nickname_word_count(nickname) < _MIN_NICKNAME_WORDS:
        return False
    return _normalized_name_tokens(nickname) == _normalized_name_tokens(page_name)


def _accepted_by_nickname(db_name: str, page_name: str | None) -> bool:
    """For a page the guard ALREADY accepted: True when only the nickname path
    let it in (it renders a hero name that does not match the DB name). The
    backfills count and log these, since that path is looser than the name one."""
    return page_name is not None and not _names_match(db_name, page_name)


def _page_identity_verified(
    db_name: str, data: AthleteData, ufc_confirmed: bool, *, nickname: str | None
) -> bool:
    """Anti-homonym guard: the resolved page must belong to this DB fighter.

    When the page renders a hero name, it must match the DB name or be exactly
    the fighter's nickname (``_hero_name_matches``; all fighters, confirmed or
    not — the check is free and catches slug redirects). When the page has no
    hero name, only fighters already confirmed on ufc.com (their stored headshot
    came from there) are trusted; the nickname plays no part there.
    """
    if data.page_name is None:
        return ufc_confirmed
    return _hero_name_matches(db_name, nickname, data.page_name)


def backfill(
    connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    solo_ufc: bool = False,
    resolver: Resolver = resolve_athlete,
    sleeper: Callable[[float], None] = time.sleep,
) -> Counter:
    session = requests.Session()
    counts: Counter = Counter()
    targets = _get_target_fighters(connection, limit=limit, solo_ufc=solo_ufc)
    total = len(targets)
    counts["targets"] = total
    LOGGER.info(
        "Fighters missing full-body/leg-reach/gym (scope=%s): %d",
        "solo-ufc" if solo_ufc else "all", total,
    )

    for idx, (fighter_id, name, nickname, ufc_confirmed) in enumerate(targets, 1):
        data = resolver(session, name)
        sleeper(REQUEST_DELAY_SECONDS)
        if data is None:
            # Includes ufc.com 404s (historical fighters without a page):
            # expected, counted, never an error.
            counts["unresolved"] += 1
        elif not _page_identity_verified(name, data, ufc_confirmed, nickname=nickname):
            counts["name_mismatch"] += 1
            LOGGER.warning(
                "Name mismatch for fighter id=%d %r: page renders %r — skipping",
                fighter_id, name, data.page_name,
            )
        else:
            counts["resolved"] += 1
            if _accepted_by_nickname(name, data.page_name):
                counts["nickname_match"] += 1
                LOGGER.info(
                    "Accepted by nickname: fighter id=%d %r (nickname %r) — page renders %r",
                    fighter_id, name, nickname, data.page_name,
                )
            if data.full_body_url:
                counts["with_full_body"] += 1
            if data.leg_reach_cm:
                counts["with_leg_reach"] += 1
            if data.trains_at:
                counts["with_trains_at"] += 1
            has_new_data = bool(
                data.full_body_url
                or data.leg_reach_cm
                or data.trains_at
                or data.birth_place
                or data.octagon_debut
            )
            if not dry_run and has_new_data:
                # Additive-only write: COALESCE fills NULL/empty columns and a
                # NULL argument never overwrites an existing value. birth_place
                # and octagon_debut (Fase 4 / BE5) ride along: the page is
                # already fetched and name-verified, so they cost nothing here.
                updated = update_fighter_enrichment(
                    connection,
                    fighter_id,
                    full_body_url=data.full_body_url,
                    leg_reach_cm=data.leg_reach_cm,
                    trains_at=data.trains_at,
                    birth_place=data.birth_place,
                    octagon_debut=data.octagon_debut,
                )
                if updated:
                    connection.commit()
                    counts["updated"] += 1

        if idx % PROGRESS_EVERY == 0:
            LOGGER.info(
                "Progress %d/%d — resolved=%d full_body=%d leg_reach=%d trains_at=%d "
                "updated=%d name_mismatch=%d",
                idx, total, counts["resolved"], counts["with_full_body"],
                counts["with_leg_reach"], counts["with_trains_at"],
                counts["updated"], counts["name_mismatch"],
            )
    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill fighters.full_body_url + leg_reach_cm + trains_at from ufc.com athlete pages."
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters (re-runnable; resumes the gap).")
    parser.add_argument(
        "--solo-ufc",
        action="store_true",
        dest="solo_ufc",
        help="Old conservative scope: only fighters whose headshot already comes from ufc.com.",
    )
    args = parser.parse_args()
    configure_logging()

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = backfill(connection, dry_run=args.dry_run, limit=args.limit, solo_ufc=args.solo_ufc)

    keys = [
        "targets", "resolved", "with_full_body", "with_leg_reach", "with_trains_at",
        "updated", "unresolved", "name_mismatch", "nickname_match",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
