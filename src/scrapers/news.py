from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import get_close_matches
from email.utils import parsedate_to_datetime
from html import unescape

import feedparser
import requests
from anthropic import Anthropic

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, alnum_name as _normalize_name, ratio
from .repositories.fighters import FighterMatchRecord, get_all_fighters
from .repositories.news import NewsArticleRecord, get_existing_news_urls, upsert_news_article


LOGGER = logging.getLogger(__name__)
# Identity matching: tagging an article with the wrong fighter_id, so keep the
# strict cutoff. See src/scrapers/matching.py for the threshold policy.
FUZZY_MATCH_THRESHOLD = IDENTITY_THRESHOLD
MAX_SUMMARY_LENGTH = 4000
CATEGORIES = {
    "fight_announcement",
    "fight_result",
    "injury",
    "ranking_change",
    "interview",
    "transfer",
    "other",
}
# Spanish-language source (matches the Spanish UI). ESPN Deportes ships a clean
# RSS with images; ufc.com/rss/news is English and image-less, so it's not used
# for news — ufc.com is used elsewhere only for athlete photos.
RSS_FEEDS = (
    ("ESPN Deportes", "https://espndeportes.espn.com/espn/rss/mma/news"),
)


@dataclass(frozen=True)
class FeedArticle:
    source: str
    title: str
    summary: str | None
    url: str
    published_at: datetime | None
    image_url: str | None = None


@dataclass(frozen=True)
class ClassificationResult:
    fighters: list[str]
    category: str


def scrape_news(max_articles: int = 100) -> Counter:
    settings = get_settings()
    counts: Counter = Counter()
    classifier = NewsClassifier(settings.anthropic_api_key)
    with connect(settings.database_url) as connection:
        fighters = get_all_fighters(connection)
        exact_name_index = _build_exact_name_index(fighters)
        normalized_name_index = _build_normalized_name_index(fighters)
        existing_urls = get_existing_news_urls(connection)
        articles = fetch_feed_articles(max_articles=max_articles)
        counts["fetched"] = len(articles)
        for article in articles:
            if article.url in existing_urls:
                counts["skipped_existing"] += 1
                continue
            classification = classifier.classify(article, fighters)
            fighter_id = _match_first_fighter_id(classification.fighters, exact_name_index, normalized_name_index)
            if fighter_id is not None:
                counts["linked"] += 1
            record = NewsArticleRecord(
                headline=article.title,
                summary=article.summary,
                source=article.source,
                url=article.url,
                published_at=article.published_at,
                fighter_id=fighter_id,
                category=classification.category,
                relevance=_calculate_relevance(classification.category, fighter_id),
                image_url=article.image_url or fetch_og_image(article.url),
            )
            upsert_news_article(connection, record)
            connection.commit()
            existing_urls.add(article.url)
            counts["stored"] += 1
            counts[f"category_{classification.category}"] += 1
            if classifier.used_claude:
                counts["classified_claude"] += 1
            else:
                counts["classified_fallback"] += 1
    return counts


class NewsClassifier:
    def __init__(self, anthropic_api_key: str | None) -> None:
        self.client = Anthropic(api_key=anthropic_api_key) if anthropic_api_key else None
        self.used_claude = False

    def classify(self, article: FeedArticle, fighters: list[FighterMatchRecord]) -> ClassificationResult:
        self.used_claude = False
        if self.client is not None:
            try:
                result = self._classify_with_claude(article)
                self.used_claude = True
                return result
            except Exception as exc:
                LOGGER.warning("Claude classification failed for %s: %s", article.url, exc)
        return _classify_with_fallback(article, fighters)

    def _classify_with_claude(self, article: FeedArticle) -> ClassificationResult:
        prompt = (
            "You are classifying MMA news. Return JSON only with keys "
            "\"fighters\" and \"category\". Category must be one of "
            "[fight_announcement, fight_result, injury, ranking_change, interview, transfer, other]. "
            f"Title: {article.title}\n"
            f"Summary: {article.summary or ''}"
        )
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()
        payload = json.loads(text)
        fighters = [str(name).strip() for name in payload.get("fighters", []) if str(name).strip()]
        category = str(payload.get("category", "other")).strip()
        if category not in CATEGORIES:
            category = "other"
        return ClassificationResult(fighters=fighters, category=category)


def fetch_feed_articles(max_articles: int) -> list[FeedArticle]:
    articles: list[FeedArticle] = []
    per_feed_limit = max(25, max_articles // len(RSS_FEEDS) + 5)
    for source, url in RSS_FEEDS:
        parsed = feedparser.parse(url)
        if getattr(parsed, "bozo", False):
            LOGGER.warning("Feed parse issue for %s: %s", source, getattr(parsed, "bozo_exception", "unknown"))
        for entry in parsed.entries[:per_feed_limit]:
            article = _entry_to_article(source, entry)
            if article is not None:
                articles.append(article)
    articles.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    deduped: list[FeedArticle] = []
    seen_urls: set[str] = set()
    for article in articles:
        if article.url in seen_urls:
            continue
        seen_urls.add(article.url)
        deduped.append(article)
        if len(deduped) >= max_articles:
            break
    return deduped


def _entry_to_article(source: str, entry) -> FeedArticle | None:
    title = _clean_text(getattr(entry, "title", None))
    url = _clean_text(getattr(entry, "link", None))
    if not title or not url:
        return None
    summary = _clean_text(getattr(entry, "summary", None) or getattr(entry, "description", None))
    if summary:
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = " ".join(summary.split())[:MAX_SUMMARY_LENGTH]
    published_at = _parse_published_at(
        getattr(entry, "published", None)
        or getattr(entry, "updated", None)
        or getattr(entry, "pubDate", None)
    )
    return FeedArticle(
        source=source,
        title=title,
        summary=summary,
        url=url,
        published_at=published_at,
        image_url=_extract_image_url(entry),
    )


IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def _is_http_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _looks_like_image(url: object) -> bool:
    return isinstance(url, str) and IMAGE_EXT_RE.search(url) is not None


def _clean_image_url(raw: object) -> str | None:
    """Decode HTML entities (e.g. ``&amp;`` in og:image query params) and keep
    only absolute http(s) URLs. Browsers don't decode entities inside an <img>
    src that arrives as an attribute string, so an un-decoded URL would 404."""
    if not isinstance(raw, str):
        return None
    cleaned = unescape(raw).strip()
    return cleaned if _is_http_url(cleaned) else None


def _extract_image_url(entry) -> str | None:
    """Best-effort image extraction from an RSS entry across the common feed shapes."""
    candidates: list[tuple[object, object]] = []
    for media in getattr(entry, "media_content", None) or []:
        if isinstance(media, dict):
            candidates.append((media.get("url"), media.get("type")))
    for thumb in getattr(entry, "media_thumbnail", None) or []:
        if isinstance(thumb, dict):
            candidates.append((thumb.get("url"), "image/"))
    for link in getattr(entry, "links", None) or []:
        if isinstance(link, dict):
            candidates.append((link.get("href"), link.get("type")))
    for enclosure in getattr(entry, "enclosures", None) or []:
        if isinstance(enclosure, dict):
            candidates.append((enclosure.get("href") or enclosure.get("url"), enclosure.get("type")))
    for url, mime in candidates:
        if not _is_http_url(url):
            continue
        if (isinstance(mime, str) and mime.startswith("image/")) or _looks_like_image(url):
            return _clean_image_url(url)
    # Fallback: first <img> in the content/summary HTML.
    html = ""
    content = getattr(entry, "content", None)
    if isinstance(content, (list, tuple)) and content and isinstance(content[0], dict):
        html = content[0].get("value", "") or ""
    if not html:
        html = getattr(entry, "summary", None) or getattr(entry, "description", None) or ""
    match = IMG_TAG_RE.search(html)
    if match:
        return _clean_image_url(match.group(1))
    return None


OG_IMAGE_RES = (
    re.compile(
        r"""<meta[^>]+(?:property|name)=["'](?:og:image(?::url)?|twitter:image(?::src)?)["'][^>]*\scontent=["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""<meta[^>]+content=["']([^"']+)["'][^>]*(?:property|name)=["'](?:og:image(?::url)?|twitter:image(?::src)?)["']""",
        re.IGNORECASE,
    ),
)
_OG_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mma-ingesta/1.0; +https://ufcstats.com)"}


def fetch_og_image(url: str, timeout: int = 10) -> str | None:
    """Fetch an article page and return its og:image / twitter:image, if any."""
    if not _is_http_url(url):
        return None
    try:
        response = requests.get(url, headers=_OG_HEADERS, timeout=timeout)
        response.raise_for_status()
        html = response.text[:300_000]
    except Exception as exc:  # noqa: BLE001 - network/parse issues are non-fatal
        LOGGER.debug("og:image fetch failed for %s: %s", url, exc)
        return None
    for pattern in OG_IMAGE_RES:
        match = pattern.search(html)
        if match:
            cleaned = _clean_image_url(match.group(1))
            if cleaned:
                return cleaned
    return None


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _classify_with_fallback(article: FeedArticle, fighters: list[FighterMatchRecord]) -> ClassificationResult:
    text = f"{article.title} {article.summary or ''}"
    lowered = text.casefold()
    fighters_found = _extract_fighter_names_from_text(text, fighters)
    if any(token in lowered for token in (" vs ", "versus", "bout", "booked", "set to face", "fight announced")):
        category = "fight_announcement"
    elif any(token in lowered for token in ("defeats", "defeat", "stops", "submits", "knocks out", "wins", "result")):
        category = "fight_result"
    elif any(token in lowered for token in ("injury", "injured", "out of", "withdraws", "withdrawn")):
        category = "injury"
    elif any(token in lowered for token in ("rankings", "ranking", "moves up", "drops to", "pound-for-pound")):
        category = "ranking_change"
    elif any(token in lowered for token in ("signs", "signed", "joins", "leaves", "released", "free agent")):
        category = "transfer"
    elif any(token in lowered for token in ("says", "interview", "talks", "speaks", "reacts", "media")):
        category = "interview"
    else:
        category = "other"
    return ClassificationResult(fighters=fighters_found, category=category)


def _extract_fighter_names_from_text(text: str, fighters: list[FighterMatchRecord]) -> list[str]:
    normalized_text = _normalize_name(text)
    matches: list[str] = []
    for fighter in fighters:
        normalized_name = _normalize_name(fighter.name)
        if normalized_name and normalized_name in normalized_text:
            matches.append(fighter.name)
            if len(matches) >= 4:
                break
    return matches


def _build_name_index(
    fighters: list[FighterMatchRecord],
    key_func: Callable[[str], str],
) -> dict[str, FighterMatchRecord | None]:
    """Index fighters by ``key_func(name)``, tombstoning homonym collisions.

    A plain dict keyed by name silently lets one of two distinct fighters who
    share a name overwrite the other (issue #6), so an article gets tagged with
    the wrong fighter_id. Ambiguous keys are stored as ``None`` and fall through
    to "no match". Mirrors the policy in src/scrapers/espn.py.
    """
    index: dict[str, FighterMatchRecord | None] = {}
    for fighter in fighters:
        if not fighter.name:
            continue
        key = key_func(fighter.name)
        if not key:
            continue
        if key in index:
            existing = index[key]
            if existing is not None and existing.id != fighter.id:
                index[key] = None  # burn the homonym key
            continue
        index[key] = fighter
    return index


def _build_exact_name_index(fighters: list[FighterMatchRecord]) -> dict[str, FighterMatchRecord | None]:
    return _build_name_index(fighters, str.casefold)


def _build_normalized_name_index(fighters: list[FighterMatchRecord]) -> dict[str, FighterMatchRecord | None]:
    return _build_name_index(fighters, _normalize_name)


def _match_first_fighter_id(
    fighter_names: list[str],
    exact_name_index: dict[str, FighterMatchRecord | None],
    normalized_name_index: dict[str, FighterMatchRecord | None],
) -> int | None:
    for fighter_name in fighter_names:
        matched = _match_fighter(fighter_name, exact_name_index, normalized_name_index)
        if matched is not None:
            return matched.id
    return None


def _match_fighter(
    full_name: str,
    exact_name_index: dict[str, FighterMatchRecord | None],
    normalized_name_index: dict[str, FighterMatchRecord | None],
) -> FighterMatchRecord | None:
    exact_match = exact_name_index.get(full_name.casefold())
    if exact_match is not None:
        return exact_match
    normalized_name = _normalize_name(full_name)
    normalized_match = normalized_name_index.get(normalized_name)
    if normalized_match is not None:
        return normalized_match
    live_keys = [key for key, value in normalized_name_index.items() if value is not None]
    candidates = get_close_matches(normalized_name, live_keys, n=1, cutoff=FUZZY_MATCH_THRESHOLD)
    if not candidates:
        return None
    candidate_name = candidates[0]
    similarity = ratio(normalized_name, candidate_name)
    if similarity < FUZZY_MATCH_THRESHOLD:
        return None
    return normalized_name_index[candidate_name]


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _calculate_relevance(category: str, fighter_id: int | None) -> int:
    base = 80 if fighter_id is not None else 50
    if category in {"fight_announcement", "fight_result", "injury"}:
        return min(100, base + 15)
    if category in {"ranking_change", "transfer", "interview"}:
        return min(100, base + 5)
    return base


def _build_summary(counts: Counter) -> str:
    return json.dumps(
        {
            "fetched": counts["fetched"],
            "stored": counts["stored"],
            "skipped_existing": counts["skipped_existing"],
            "classified_claude": counts["classified_claude"],
            "classified_fallback": counts["classified_fallback"],
            "linked": counts["linked"],
        },
        indent=2,
        sort_keys=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape MMA news from RSS feeds.")
    parser.add_argument("--max-articles", type=int, default=100, help="Maximum number of articles to ingest.")
    args = parser.parse_args()
    configure_logging()
    counts = scrape_news(max_articles=args.max_articles)
    print(_build_summary(counts))


if __name__ == "__main__":
    main()