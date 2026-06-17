from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse


INCH_TO_CM = 2.54
POUND_TO_GRAMS = 453.59237


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.replace("\xa0", " ").split()).strip()
    return normalized or None


def parse_int(value: str | None) -> int | None:
    text = clean_text(value)
    if not text or text in {"--", "---", "N/A"}:
        return None
    digits = re.sub(r"[^\d-]", "", text)
    if not digits:
        return None
    return int(digits)


def parse_height_to_cm(value: str | None) -> float | None:
    text = clean_text(value)
    if not text or text in {"--", "---"}:
        return None
    match = re.match(r"(?P<feet>\d+)'\\s*(?P<inches>\d+)\"", text)
    if not match:
        return None
    feet = int(match.group("feet"))
    inches = int(match.group("inches"))
    return round(((feet * 12) + inches) * INCH_TO_CM, 2)


def parse_reach_to_cm(value: str | None) -> float | None:
    text = clean_text(value)
    if not text or text in {"--", "---"}:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    inches = int(match.group(1))
    return round(inches * INCH_TO_CM, 2)


def parse_weight_to_grams(value: str | None) -> int | None:
    text = clean_text(value)
    if not text or text in {"--", "---"}:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    pounds = int(match.group(1))
    return int(round(pounds * POUND_TO_GRAMS))


def parse_record(value: str | None) -> tuple[int, int, int]:
    text = clean_text(value) or ""
    match = re.search(r"(\d+)\s*-\s*(\d+)\s*-\s*(\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    match = re.search(r"(\d+)\s*-\s*(\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2)), 0
    return 0, 0, 0


def parse_date(value: str | None) -> datetime.date | None:
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_landed_attempted(value: str | None) -> tuple[int | None, int | None]:
    text = clean_text(value)
    if not text or text in {"--", "---"}:
        return None, None
    match = re.search(r"(\d+)\s+of\s+(\d+)", text, re.IGNORECASE)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def parse_control_time_to_seconds(value: str | None) -> int | None:
    text = clean_text(value)
    if not text or text in {"--", "---"}:
        return None
    match = re.match(r"(?:(\d+):)?(\d+):(\d+)$", text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        return hours * 3600 + minutes * 60 + seconds
    match = re.match(r"(\d+):(\d+)$", text)
    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        return minutes * 60 + seconds
    return None


def source_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path