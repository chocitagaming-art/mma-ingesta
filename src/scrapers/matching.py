"""Single source of truth for fighter / event *name matching*.

Historically the normalize -> fold -> fuzzy-compare pipeline was copy-pasted into
~8 scraper modules, each drifting its own way (three different normalizers and two
different fuzzy thresholds: 0.92 vs 0.85 vs 0.80). That made every tweak a
multi-file hunt and let the modules disagree about whether two names are "the
same". This module centralizes the primitives so every caller shares one
implementation and one documented threshold policy.

Threshold policy
----------------
``DEFAULT_THRESHOLD = 0.87`` is the single canonical cutoff for general-purpose
name comparison. It is a deliberate compromise:

* MMA names are short. On a ~12-character normalized name a single-character
  difference already scores ~0.92 with ``difflib``, so 0.87 still demands a very
  close match and rejects unrelated names.
* Yet it is loose enough to absorb the spelling / transliteration / punctuation
  drift that is endemic to fight data ("Jiri" vs "Jiří", "St-Pierre" vs
  "St Pierre", a dropped middle name), which the old 0.92 cutoff sometimes
  rejected.

``IDENTITY_THRESHOLD = 0.92`` is the one documented exception. It is used only by
the ingestion path that *links a name to a DB fighter_id* (the ESPN matchers,
news tagging, rankings). There a false positive is not a cosmetic glitch: it
welds the wrong fighter's stats/photo/record onto a record. Those call sites
keep the stricter 0.92 they already shipped with, so this refactor does not loosen
identity matching. (rankings.py's module docstring already advertises
"exact -> normalized -> fuzzy @ 0.92" as a contract.)

Normalizers
-----------
Three normalizers exist because the codebase genuinely needs three keys; each is
documented below. ``normalize_name`` is the default and the base for ``fold``.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

__all__ = [
    "DEFAULT_THRESHOLD",
    "IDENTITY_THRESHOLD",
    "strip_accents",
    "normalize_name",
    "casefold_name",
    "alnum_name",
    "fold",
    "ratio",
    "fold_ratio",
    "fuzzy_match",
]

# Canonical compromise cutoff for general-purpose name comparison.
DEFAULT_THRESHOLD = 0.87
# Stricter cutoff reserved for matches that attach a DB fighter_id, where a false
# positive corrupts data. See module docstring.
IDENTITY_THRESHOLD = 0.92

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def strip_accents(text: str) -> str:
    """Remove diacritics via Unicode NFKD decomposition.

    Decomposes each character into its base + combining marks and drops the marks,
    so "Jiří" -> "Jiri" and "Procházka" -> "Prochazka". Case and spacing are left
    untouched; this is a low-level primitive that the normalizers build on.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Default name key: lowercase, trim, collapse whitespace, split on ``.``/``-``.

    Casefolds, treats ``.`` and ``-`` as separators (so "St-Pierre" and
    "St Pierre" produce the same key, and "T.J." becomes "t j"), then collapses
    runs of whitespace. This is the key used by the ESPN fighter matchers and the
    base for :func:`fold`.
    """
    return " ".join(name.casefold().replace(".", " ").replace("-", " ").split())


def casefold_name(name: str) -> str:
    """Lightest name key: lowercase, trim, collapse whitespace only.

    Unlike :func:`normalize_name` it does NOT split on ``.``/``-``, so hyphenated
    names stay distinct. Used where that distinction must be preserved (e.g. the
    duplicate-fighter grouping key).
    """
    return " ".join(name.casefold().split())


def alnum_name(name: str) -> str:
    """Aggressive name key: lowercase then drop every non ``[a-z0-9]`` character.

    Strips all punctuation, apostrophes and stray symbols (collapsing them to a
    single space). Best when the input carries noise that should not affect
    matching -- HTML entities, parentheses, quotes -- e.g. free-text news bodies
    or scraped headshot captions.
    """
    return " ".join(_NON_ALNUM_RE.sub(" ", name.casefold()).split())


def fold(name: str) -> str:
    """Diacritic-insensitive form of :func:`normalize_name`.

    Strips accents first (NFKD) then applies :func:`normalize_name`, so
    "Jiří Procházka" folds to "jiri prochazka" and matches a DB row stored as the
    plain ASCII "Jiri Prochazka".
    """
    return normalize_name(strip_accents(name))


def ratio(left: str, right: str) -> float:
    """Raw ``difflib`` similarity ratio (0.0-1.0) of two already-prepared strings.

    Callers are expected to have normalized/folded both sides beforehand; this is
    the thin wrapper over ``SequenceMatcher`` that every module used to inline.
    """
    return SequenceMatcher(None, left, right).ratio()


def fold_ratio(left: str, right: str) -> float:
    """Diacritic-insensitive similarity: :func:`ratio` over :func:`fold` of both."""
    return ratio(fold(left), fold(right))


def fuzzy_match(left: str, right: str, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """True when two names are similar enough under diacritic-insensitive folding.

    Compares ``fold(left)`` and ``fold(right)`` and tests against ``threshold``
    (the canonical :data:`DEFAULT_THRESHOLD` by default; pass
    :data:`IDENTITY_THRESHOLD` for fighter_id-linking call sites).
    """
    return fold_ratio(left, right) >= threshold
