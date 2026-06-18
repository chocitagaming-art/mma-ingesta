from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from html import unescape
from typing import Any
from urllib.parse import urlparse

from .config import get_settings
from .db import connect


SUSPICIOUS_KEYWORDS = (
    "descarga",
    "download",
    "free",
    "gratis",
    "gratuita",
    "mp3",
    "pdf",
    "torrent",
    "lyrics",
    "ringtone",
    "video",
    "youtube",
    "spotify",
    "deezer",
    "mediafire",
    "mega",
    "zippyshare",
)
SUSPICIOUS_KEYWORD_PATTERN = re.compile(
    r"\b(?:descarga|download|free|gratis|gratuita|mp3|pdf|torrent|lyrics|ringtone|video|youtube|spotify|deezer|mediafire|mega|zippyshare)\b",
    re.IGNORECASE,
)
NAME_STOPWORDS = {
    "de",
    "del",
    "da",
    "dos",
    "das",
    "di",
    "la",
    "le",
    "van",
    "von",
    "bin",
    "al",
    "el",
    "jr",
    "sr",
}
ESPN_HEADSHOT_HOST = "a.espncdn.com"
ESPN_HEADSHOT_PATH_FRAGMENT = "/i/headshots/mma/players/"


@dataclass(frozen=True)
class NameChange:
    fighter_id: int
    old_name: str
    new_name: str | None
    action: str
    reason: str


@dataclass(frozen=True)
class AuditSummary:
    dry_run: bool
    fighters_before: int
    fighters_after: int
    suspicious_name_candidates: int
    names_updated: int
    fighters_deleted: int
    headshots_before: int
    headshots_nullified: int
    headshots_after: int
    weird_name_count_before: int
    weird_name_count_after: int
    duplicate_name_groups_after: int
    duplicate_name_rows_after: int
    distinct_headshot_domains_before: dict[str, int]
    distinct_headshot_domains_after: dict[str, int]
    long_names_before: list[dict[str, Any]]
    suspicious_rows_before: list[dict[str, Any]]
    name_changes: list[dict[str, Any]]
    weird_names_before: list[dict[str, Any]]
    weird_names_after: list[dict[str, Any]]
    duplicate_names_after: list[dict[str, Any]]


def _normalize_spaces(value: str) -> str:
    return " ".join(value.split())


def _looks_suspicious(name: str) -> bool:
    return len(name) > 40 or bool(SUSPICIOUS_KEYWORD_PATTERN.search(name))


def _extract_real_name(name: str) -> str | None:
    cleaned = _normalize_spaces(unescape(name).replace("|", " ").replace("/", " "))
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"\[[^\]]*\]", " ", cleaned)
    cleaned = _normalize_spaces(cleaned)
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", cleaned)
    if not tokens:
        return None

    best_run: list[str] = []
    current_run: list[str] = []
    for token in tokens:
        lowered = token.casefold()
        if lowered in SUSPICIOUS_KEYWORDS:
            if len(current_run) > len(best_run):
                best_run = current_run[:]
            current_run = []
            continue
        if lowered in NAME_STOPWORDS or token[0].isupper():
            current_run.append(token)
            continue
        if len(current_run) > len(best_run):
            best_run = current_run[:]
        current_run = []

    if len(current_run) > len(best_run):
        best_run = current_run[:]

    if not best_run:
        return None

    candidate = _normalize_spaces(" ".join(best_run)).strip(" -_,;:")
    words = candidate.split()
    uppercase_words = [word for word in words if word and word[0].isupper()]
    if len(uppercase_words) < 2:
        return None
    if len(candidate) < 5 or len(candidate) > 60:
        return None
    return candidate


def _is_valid_espn_headshot(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.casefold() != ESPN_HEADSHOT_HOST:
        return False
    return parsed.path.casefold().startswith(ESPN_HEADSHOT_PATH_FRAGMENT)


def _fetch_count(cursor, query: str, params: tuple[Any, ...] = ()) -> int:
    cursor.execute(query, params)
    return int(cursor.fetchone()[0])


def _fetch_headshot_domains(cursor) -> dict[str, int]:
    cursor.execute(
        """
        SELECT
            COALESCE(NULLIF(split_part(regexp_replace(headshot_url, '^https?://', ''), '/', 1), ''), '(blank)') AS domain,
            COUNT(*)
        FROM fighters
        WHERE headshot_url IS NOT NULL
        GROUP BY 1
        ORDER BY COUNT(*) DESC, 1
        """
    )
    return {str(domain): int(count) for domain, count in cursor.fetchall()}


def _fetch_suspicious_rows(cursor) -> list[tuple[int, str]]:
    cursor.execute(
        r"""
        SELECT id, name
        FROM fighters
        WHERE char_length(name) > 40
           OR name ~* '\m(descarga|download|free|gratis|gratuita|mp3|pdf|torrent|lyrics|ringtone|video|youtube|spotify|deezer|mediafire|mega|zippyshare)\M'
        ORDER BY id
        """
    )
    return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def _fetch_long_names(cursor) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, name, length(name::text) AS name_length
        FROM fighters
        WHERE length(name::text) > 40
        ORDER BY name_length DESC, id
        """
    )
    return [
        {"id": int(row[0]), "name": str(row[1]), "name_length": int(row[2])}
        for row in cursor.fetchall()
    ]


def _fetch_weird_names(cursor) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, name
        FROM fighters
        WHERE name ~ '[<&;]'
           OR name LIKE '%�%'
           OR name LIKE '%Ã%'
           OR name LIKE '%Â%'
        ORDER BY id
        """
    )
    return [{"id": int(row[0]), "name": str(row[1])} for row in cursor.fetchall()]


def _fetch_duplicate_names(cursor) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT lower(trim(name)) AS normalized_name, COUNT(*) AS fighter_count, array_agg(id ORDER BY id) AS fighter_ids
        FROM fighters
        GROUP BY lower(trim(name))
        HAVING COUNT(*) > 1
        ORDER BY fighter_count DESC, normalized_name
        """
    )
    return [
        {
            "normalized_name": str(row[0]),
            "fighter_count": int(row[1]),
            "fighter_ids": [int(value) for value in row[2]],
        }
        for row in cursor.fetchall()
    ]


def cleanup_data_quality(dry_run: bool = False) -> AuditSummary:
    settings = get_settings()

    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            fighters_before = _fetch_count(cursor, "SELECT COUNT(*) FROM fighters")
            headshots_before = _fetch_count(cursor, "SELECT COUNT(*) FROM fighters WHERE headshot_url IS NOT NULL")
            distinct_headshot_domains_before = _fetch_headshot_domains(cursor)
            long_names_before = _fetch_long_names(cursor)
            weird_names_before = _fetch_weird_names(cursor)
            suspicious_rows = _fetch_suspicious_rows(cursor)
            suspicious_rows_before = [{"id": fighter_id, "name": name} for fighter_id, name in suspicious_rows]

            name_changes: list[NameChange] = []
            deleted_ids: list[int] = []

            for fighter_id, old_name in suspicious_rows:
                extracted_name = _extract_real_name(old_name)
                if extracted_name and extracted_name != old_name:
                    cursor.execute(
                        """
                        UPDATE fighters
                        SET name = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (extracted_name, fighter_id),
                    )
                    name_changes.append(
                        NameChange(
                            fighter_id=fighter_id,
                            old_name=old_name,
                            new_name=extracted_name,
                            action="updated",
                            reason="extracted probable real fighter name from suspicious text",
                        )
                    )
                elif extracted_name == old_name:
                    continue
                else:
                    cursor.execute("DELETE FROM fighters WHERE id = %s", (fighter_id,))
                    deleted_ids.append(fighter_id)
                    name_changes.append(
                        NameChange(
                            fighter_id=fighter_id,
                            old_name=old_name,
                            new_name=None,
                            action="deleted",
                            reason="could not determine a reliable fighter name",
                        )
                    )

            cursor.execute("SELECT id, headshot_url FROM fighters WHERE headshot_url IS NOT NULL")
            invalid_headshot_ids: list[int] = []
            invalid_patterns: Counter[str] = Counter()
            for fighter_id, headshot_url in cursor.fetchall():
                url = str(headshot_url)
                if _is_valid_espn_headshot(url):
                    continue
                invalid_headshot_ids.append(int(fighter_id))
                invalid_patterns[urlparse(url).netloc.casefold() or "(blank)"] += 1

            if invalid_headshot_ids:
                cursor.execute(
                    """
                    UPDATE fighters
                    SET headshot_url = NULL, updated_at = NOW()
                    WHERE id = ANY(%s)
                    """,
                    (invalid_headshot_ids,),
                )

            fighters_after = _fetch_count(cursor, "SELECT COUNT(*) FROM fighters")
            headshots_after = _fetch_count(cursor, "SELECT COUNT(*) FROM fighters WHERE headshot_url IS NOT NULL")
            weird_names_after = _fetch_weird_names(cursor)
            duplicate_names_after = _fetch_duplicate_names(cursor)
            distinct_headshot_domains_after = _fetch_headshot_domains(cursor)

        if dry_run:
            connection.rollback()
        else:
            connection.commit()

    return AuditSummary(
        dry_run=dry_run,
        fighters_before=fighters_before,
        fighters_after=fighters_after,
        suspicious_name_candidates=len(suspicious_rows),
        names_updated=sum(1 for change in name_changes if change.action == "updated"),
        fighters_deleted=len(deleted_ids),
        headshots_before=headshots_before,
        headshots_nullified=len(invalid_headshot_ids),
        headshots_after=headshots_after,
        weird_name_count_before=len(weird_names_before),
        weird_name_count_after=len(weird_names_after),
        duplicate_name_groups_after=len(duplicate_names_after),
        duplicate_name_rows_after=sum(item["fighter_count"] for item in duplicate_names_after),
        distinct_headshot_domains_before=distinct_headshot_domains_before,
        distinct_headshot_domains_after=distinct_headshot_domains_after,
        long_names_before=long_names_before,
        suspicious_rows_before=suspicious_rows_before,
        name_changes=[asdict(change) for change in name_changes],
        weird_names_before=weird_names_before,
        weird_names_after=weird_names_after,
        duplicate_names_after=duplicate_names_after,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean suspicious fighter names and invalid headshot URLs.")
    parser.add_argument("--dry-run", action="store_true", help="Preview cleanup without committing.")
    args = parser.parse_args()
    print(json.dumps(asdict(cleanup_data_quality(dry_run=args.dry_run)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()