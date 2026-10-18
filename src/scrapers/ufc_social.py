"""A fighter's Instagram and X profiles, read from their ufc.com athlete page.

WHERE THEY LIVE. ufc.com does not render social icons in the athlete page body.
It publishes them in the structured-data block only:

    <script type="application/ld+json"> -> @graph -> mainEntity (@type Person)
        -> sameAs: ["https://twitter.com/...", "https://instagram.com/...", ...]

Measured on 20 pages (18-oct-2026): all 20 carry that block; 18 list Instagram,
16 list X (always as twitter.com), 7 Facebook (not stored: owner's decision).

🪤 THE TRAP. Every ufc.com page footer links the UFC's OWN accounts
(instagram.com/ufc/, twitter.com/ufc). Scanning the page's <a> tags would give
@ufc to every fighter. This module therefore NEVER reads anchors: only the
``sameAs`` of a Person node whose ``url`` is an ufc.com /athlete/ page.

THE TWO ANSWERS (they drive the write rule in enrich_athlete_stats):
  - ``None``: no Person block on the page -> we do not know -> touch nothing.
  - ``SocialLinks(...)``: the block is there -> ufc.com is authoritative, and a
    network it no longer lists comes back as None, which the writer stores as
    NULL (owner's decision, 18-oct: a dropped account is deleted).
    A Person WITHOUT a ``sameAs`` key counts as "lists nothing": measured on
    18-oct, ufc.com omits the key for fighters with no accounts (Fred Ettish,
    Zane Frazier, Kevin Rosier, Jason DeLucia), it never sends an empty list.
    The guard against a template change that drops the key for EVERYONE is the
    run-level brake in enrich_athlete_stats, not this parser.

UNPARSED != ABSENT. A sameAs entry ON instagram.com / twitter.com / x.com that
does not normalize to a profile (a post, an unknown URL shape ufc.com might
start using) is not proof the account is gone: ``instagram_unparsed`` /
``x_unparsed`` tell the writer to KEEP what is stored. Only an explicit filler
("Unavailable") or the UFC's own account on that host means "no account".

NORMALIZATION. ufc.com serves dirty values: "twitter.com/@navajostirling",
"facebook.com/Unavailable  ", "?ref=br_rs", mixed case. Output is exactly the
shape migration 032's CHECK constraints accept:
  - https://www.instagram.com/<handle lower-cased>/
  - https://x.com/<handle>
Anything that is not clearly a profile (a post, an intent link, a placeholder,
the UFC's own account, another domain) normalizes to None.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

# Handle grammar. Instagram: letters, digits, dot, underscore, max 30 (stored
# lower-case: Instagram handles are case-insensitive). X: letters, digits,
# underscore, max 15 (case kept as published).
# Always used with fullmatch: Python's ``$`` also matches before a trailing
# newline, Postgres's does not.
_IG_HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}")
_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")

# Last line of defence: the EXACT patterns of migration 032's CHECKs
# (tests/test_ufc_social.py reads them from the .sql and compares). A value the
# CHECK would reject aborts the whole sweep, so it is never returned.
DB_CHECK_INSTAGRAM = re.compile(r"^https://www\.instagram\.com/[a-z0-9._]{1,30}/$")
DB_CHECK_X = re.compile(r"^https://x\.com/[A-Za-z0-9_]{1,15}$")

# First path segments that are site sections, not profiles.
_IG_RESERVED = frozenset({
    "p", "reel", "reels", "tv", "explore", "stories", "accounts", "direct",
    "about", "developer", "legal", "web", "share", "_u",
})
_X_RESERVED = frozenset({
    "intent", "share", "home", "i", "search", "hashtag", "explore", "settings",
    "messages", "notifications", "login", "signup", "tos", "privacy",
})

# Fillers seen (or plausible) in ufc.com's data, plus the UFC's own accounts:
# a fighter is never @ufc. Compared lower-cased, against the WHOLE handle only
# ("michelpereiraufc" is a real fighter account and passes).
_PLACEHOLDERS = frozenset({
    "unavailable", "none", "null", "na", "n_a", "tbd", "tba", "undefined",
    "ufc", "ufcespanol", "ufc_espanol", "ufcbrasil", "ufceurope", "ufc_europe",
})

_IG_HOSTS = frozenset({"instagram.com", "instagr.am"})
_X_HOSTS = frozenset({"x.com", "twitter.com"})


@dataclass(frozen=True)
class SocialLinks:
    """Normalized profile URLs; None = ufc.com does not list that network.

    ``*_unparsed``: None, but because ufc.com lists that network in a shape we
    cannot read (not because it dropped it): the writer keeps the stored link.
    """

    instagram_url: str | None
    x_url: str | None
    instagram_unparsed: bool = False
    x_unparsed: bool = False

    @property
    def any(self) -> bool:
        return self.instagram_url is not None or self.x_url is not None


def _host_and_segments(raw: str | None) -> tuple[str | None, list[str]]:
    """Lower-cased bare host (www./m./mobile. stripped) and non-empty path
    segments. Only http(s) URLs or scheme-less host/path strings qualify."""
    value = (raw or "").strip()
    if not value:
        return None, []
    if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.IGNORECASE):
        if not re.match(r"^https?://", value, re.IGNORECASE):
            return None, []  # javascript:, mailto:, ...
    else:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None, []
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    path = parts.path
    # Old Twitter hash-bang links: twitter.com/#!/JonnyBones.
    if path.strip("/") == "" and parts.fragment.startswith("!/"):
        path = parts.fragment[1:]
    segments = [segment for segment in path.split("/") if segment]
    return host or None, segments


def _profile_handle(raw: str | None, hosts: frozenset[str], reserved: frozenset[str]) -> str | None:
    """The handle of a PROFILE URL on one of ``hosts``, else None. Exactly one
    path segment: /<handle>/status/123 (a post) or /p/ABC is not a profile."""
    host, segments = _host_and_segments(raw)
    # instagram.com/_u/<handle>: the app deep-link form of a profile.
    if host in _IG_HOSTS and len(segments) == 2 and segments[0] == "_u":
        segments = segments[1:]
    if host not in hosts or len(segments) != 1:
        return None
    handle = segments[0].strip().lstrip("@")
    lowered = handle.lower()
    if not handle or lowered in reserved or lowered in _PLACEHOLDERS:
        return None
    return handle


def normalize_instagram(raw: str | None) -> str | None:
    """'https://instagram.com/TheNotoriousMMA?hl=es' -> 'https://www.instagram.com/thenotoriousmma/'."""
    handle = _profile_handle(raw, _IG_HOSTS, _IG_RESERVED)
    if handle is None or not _IG_HANDLE.fullmatch(handle):
        return None
    # Instagram never starts/ends a handle with a dot nor allows "..".
    if handle.startswith(".") or handle.endswith(".") or ".." in handle:
        return None
    url = f"https://www.instagram.com/{handle.lower()}/"
    return url if DB_CHECK_INSTAGRAM.fullmatch(url) else None


def normalize_x(raw: str | None) -> str | None:
    """'https://twitter.com/@navajostirling' -> 'https://x.com/navajostirling'."""
    handle = _profile_handle(raw, _X_HOSTS, _X_RESERVED)
    if handle is None or not _X_HANDLE.fullmatch(handle):
        return None
    url = f"https://x.com/{handle}"
    return url if DB_CHECK_X.fullmatch(url) else None


def _says_no_account(segments: list[str]) -> bool:
    """A sameAs entry on the network's host that explicitly means "none":
    a filler ("Unavailable") or the UFC's own account as the only segment."""
    return len(segments) == 1 and segments[0].strip().lstrip("@").lower() in _PLACEHOLDERS


def _unreadable_on(raw: str, hosts: frozenset[str]) -> bool:
    """True when ``raw`` is ON one of ``hosts`` but is neither a readable
    profile nor an explicit "no account" (callers check the first part)."""
    host, segments = _host_and_segments(raw)
    return host in hosts and not _says_no_account(segments)


def _is_person(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "Person" in kinds


def _is_athlete_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (host == "ufc.com" or host.endswith(".ufc.com")) and parts.path.startswith("/athlete/")


def _candidate_nodes(data: object) -> list[dict]:
    """Top-level nodes of a JSON-LD document plus each node's mainEntity."""
    if isinstance(data, list):
        roots = data
    elif isinstance(data, dict):
        graph = data.get("@graph")
        roots = graph if isinstance(graph, list) else [data]
    else:
        return []
    nodes: list[dict] = []
    for root in roots:
        if not isinstance(root, dict):
            continue
        nodes.append(root)
        main = root.get("mainEntity")
        if isinstance(main, dict):
            nodes.append(main)
    return nodes


def _athlete_person(soup: BeautifulSoup) -> dict | None:
    """The Person node of the athlete this page is about, or None."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in _candidate_nodes(data):
            if _is_person(node) and _is_athlete_url(node.get("url")):
                return node
    return None


def extract_social_links(soup: BeautifulSoup) -> SocialLinks | None:
    """Instagram / X from the athlete Person's ``sameAs`` ONLY.

    None when the page has no athlete Person block (layout drift, a search
    page, a non-athlete page): the caller must then leave stored values alone.
    With the block present, a network it does not list (or lists in a shape
    that is not a profile) is None, i.e. "delete it". The first valid URL per
    network wins; the page's anchors (footer @ufc) are never read.
    """
    person = _athlete_person(soup)
    if person is None:
        return None
    same_as = person.get("sameAs")
    if isinstance(same_as, str):
        same_as = [same_as]
    if not isinstance(same_as, list):
        same_as = []
    instagram = x = None
    instagram_unparsed = x_unparsed = False
    for raw in same_as:
        if not isinstance(raw, str):
            continue
        if instagram is None:
            instagram = normalize_instagram(raw)
            if instagram is None and _unreadable_on(raw, _IG_HOSTS):
                instagram_unparsed = True
        if x is None:
            x = normalize_x(raw)
            if x is None and _unreadable_on(raw, _X_HOSTS):
                x_unparsed = True
    return SocialLinks(
        instagram_url=instagram,
        x_url=x,
        instagram_unparsed=instagram is None and instagram_unparsed,
        x_unparsed=x is None and x_unparsed,
    )
