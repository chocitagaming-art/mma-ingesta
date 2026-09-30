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
from datetime import date
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
    "token_subset_match",
    "given_name_diminutive_match",
    "split_generational_suffix",
    "birth_key",
    "history_identity_ok",
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


# Name particles and suffixes that carry no identity on their own: without
# this list "da Silva" or "dos Anjos" would count as two shared tokens and a
# bare compound surname could claim a fighter (fold() splits "." and "-", so
# initials like "T.J." degrade to single letters too — hence the len >= 2 cut).
_PARTICLE_TOKENS = frozenset({
    "al", "da", "das", "de", "del", "der", "di", "do", "dos", "du", "el",
    "ii", "iii", "jr", "la", "le", "los", "sr", "st", "van", "von",
})


def _significant_tokens(tokens: set[str]) -> set[str]:
    return {token for token in tokens if len(token) >= 2 and token not in _PARTICLE_TOKENS}


def token_subset_match(left: str, right: str) -> bool:
    """True when one folded name's tokens are contained in the other's.

    Covers the dropped-middle-name case ("Jose Delgado" vs "Jose Miguel
    Delgado") that the ratio tiers miss: on names this short ``difflib``
    scores it ~0.77, under every threshold above. Containment is exact per
    token (accent/case folded via :func:`fold`), so spelling variants remain
    a fuzzy-tier problem. The shared tokens must include at least two
    SIGNIFICANT ones (length >= 2, not a name particle like "da"/"dos"/"jr"),
    so a bare surname — simple ("Delgado"), compound ("da Silva", "dos
    Anjos"), or a pair of split initials ("T.J.") — never claims a fighter.
    Callers resolving against a POOL of candidates must ALSO keep their own
    uniqueness guard: two "Jose … Delgado"s both contain "Jose Delgado".
    """
    left_tokens = set(fold(left).split())
    right_tokens = set(fold(right).split())
    if len(_significant_tokens(left_tokens & right_tokens)) < 2:
        return False
    return left_tokens <= right_tokens or right_tokens <= left_tokens


# Suelo de longitud del diminutivo. Con 4 caracteres "zach"/"zachary" y
# "alex"/"alexander" pasan, y "jon"/"jonathan" o "ali"/"alistair" no. Es
# deliberado: por debajo de 4 hay nombres completos y cortos que son prefijo de
# otros distintos, y en este matcher un falso positivo suelda las stats del
# rival en la ficha de otro. Perder un match solo cuesta que la pelea espere.
_MIN_DIMINUTIVE_LEN = 4


def given_name_diminutive_match(left: str, right: str) -> bool:
    """True cuando dos nombres solo se diferencian en el DIMINUTIVO del pila.

    Tercer nivel, después de la igualdad exacta y de
    :func:`token_subset_match`. Existe por un caso real: la pelea 12850 del
    UFC 329 pasó **catorce días** sin árbitro ni stats porque nuestra ficha
    dice "Zachary Reese" y ufcstats la lista como "Zach Reese". Ni la clave
    exacta ni el subconjunto por tokens lo cazan, porque "zach" y "zachary"
    son tokens DISTINTOS, y difflib se queda en 0.83, por debajo del umbral.

    LA RESTRICCIÓN IMPORTANTE: solo el PRIMER token admite el prefijo; el
    resto deben coincidir exactamente, y el número de tokens debe ser el mismo.
    Un prefijo libre sobre cualquier token casaría "Marcos Silva" con "Marcos
    Silveira", que son personas distintas — los diminutivos ocurren en el
    nombre de pila, no en el apellido. Con esa restricción, más el suelo de
    :data:`_MIN_DIMINUTIVE_LEN`, el nivel es estrecho a propósito.

    Como los demás niveles, NO garantiza unicidad: quien resuelva contra un
    grupo de candidatos mantiene su propia guarda (``corner_for`` rechaza un
    nombre que reclame las dos esquinas, y ``_match_fight`` exige candidato
    único).
    """
    left_tokens = fold(left).split()
    right_tokens = fold(right).split()
    if len(left_tokens) != len(right_tokens) or len(left_tokens) < 2:
        return False
    # Apellidos (y cualquier token intermedio) exactos, sin excepción.
    if left_tokens[1:] != right_tokens[1:]:
        return False
    corto, largo = sorted((left_tokens[0], right_tokens[0]), key=len)
    if corto == largo:
        return False  # Idénticos: eso ya lo resuelve el nivel exacto.
    return len(corto) >= _MIN_DIMINUTIVE_LEN and largo.startswith(corto)


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


# Minimum length of a shared word for plausibly_same_person. With 4,
# "Osmanli" and "Silva" count; particles ("da", "de", "jr") never do.
_MIN_SHARED_TOKEN_LEN = 4


def _surname_tokens(name: str) -> set[str]:
    """Folded words of a name except the first (the given name), each of at
    least _MIN_SHARED_TOKEN_LEN letters. A one-word name keeps its word."""
    words = fold(name).split()
    if len(words) > 1:
        words = words[1:]
    return {word for word in words if len(word) >= _MIN_SHARED_TOKEN_LEN}


def plausibly_same_person(name: str, candidates, nicknames=()) -> bool:
    """Whether ``name`` can be the same fighter as any of ``candidates``.

    A COARSE filter on purpose, never an identification: it only has to tell
    apart two spellings of one person from two people. Accepted:

    * the same words in any order ("Liu Ce" / "Ce Liu", ring name == nickname);
    * a close whole-name spelling (fuzzy_match at DEFAULT_THRESHOLD: a letter
      fold() cannot strip, like the Polish l in "Blachowicz");
    * a shared word of 4+ letters in SURNAME position, i.e. not the first word
      of either name ("Mahammadali/Mehemmedeli Osmanli", "Jose Montanha da
      Silva" / "Henrique da Silva Lopes", "Ilimbek Akylbek (Uulu)").

    A shared first name alone ("Michael Johnson" / "Michael Chiesa") is NOT
    enough: that is the substitute-vs-withdrawn case this filter exists for.
    ``nicknames`` only count as a WHOLE name (same words or a close
    spelling): a nickname word is not a surname ("The King" must not make
    "Sean King" look like Erik Silva). A shared common surname (two different
    Silvas) still passes; callers must not treat a True as proof of identity.
    """
    name = name or ""
    words = sorted(fold(name).split())
    surname = _surname_tokens(name)
    for candidate in candidates:
        if not candidate:
            continue
        if sorted(fold(candidate).split()) == words:
            return True
        if fuzzy_match(name, candidate, DEFAULT_THRESHOLD):
            return True
        if surname & _surname_tokens(candidate):
            return True
    for nickname in nicknames:
        if not nickname:
            continue
        if sorted(fold(nickname).split()) == words:
            return True
        if fuzzy_match(name, nickname, DEFAULT_THRESHOLD):
            return True
    return False


# --- Identity guard of the ESPN history import (t4-9-2) ---------------------

# Generational suffixes, folded (fold() already turns "Jr." into "jr"). The
# long forms map to the short one so "Junior" and "Jr." are the same suffix.
# "Neto" and "Filho" are deliberately NOT here: in Brazil they are surnames.
_GENERATIONAL_SUFFIXES = {
    "jr": "jr", "junior": "jr",
    "sr": "sr", "senior": "sr",
    "ii": "ii", "iii": "iii", "iv": "iv",
}

# Minimum length of the shared word in the same-birth-date rule: "Spohn" and
# "Jose" count, "Al" or "da" never do.
_MIN_BIRTH_SHARED_TOKEN_LEN = 3

# How far apart two known birth years may be before the names are taken to be
# two people (father and son). One year of slack absorbs time zones and typos.
_MAX_BIRTH_YEAR_GAP = 1

_NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")

BirthKey = tuple[int, tuple[int, int]]


def split_generational_suffix(name: str) -> tuple[str, str | None]:
    """Split a trailing generational suffix off a folded name.

    Returns ``(base, suffix)`` where ``base`` is the folded name without the
    suffix and ``suffix`` is the canonical form ("jr", "sr", "ii", "iii", "iv")
    or None. Only the LAST word counts, and only when at least two words remain
    ("Aswell Jr." keeps its "jr": a bare surname is not a name). "Neto" and
    "Filho" are surnames, never suffixes.
    """
    tokens = fold(name).split()
    if len(tokens) >= 3 and tokens[-1] in _GENERATIONAL_SUFFIXES:
        return " ".join(tokens[:-1]), _GENERATIONAL_SUFFIXES[tokens[-1]]
    return " ".join(tokens), None


def birth_key(value: date | str | None) -> BirthKey | None:
    """``(year, (low, high))`` of a birth date, with day and month UNORDERED.

    ESPN's ``displayDOB`` puts day and month in an order that depends on the
    request's lang/region ("12/10/1984", "23/5/2002"), so the only safe
    comparison is the year plus the unordered pair {day, month}. Accepts a
    ``date``, that ``d/m/yyyy``-or-``m/d/yyyy`` text, or an ISO date/datetime.
    Anything else, or an impossible date, is None.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value.year, tuple(sorted((value.month, value.day)))
    text = str(value).strip()
    match = _NUMERIC_DATE_RE.match(text)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        match = _ISO_DATE_RE.match(text)
        if not match:
            return None
        year, first, second = int(match.group(1)), int(match.group(2)), int(match.group(3))
    low, high = sorted((first, second))
    # At least one of the two must be a month, and neither can pass 31.
    if low < 1 or low > 12 or high > 31:
        return None
    return year, (low, high)


def history_identity_ok(
    our_name: str,
    espn_name: str,
    *,
    our_birth: date | str | None = None,
    espn_birth: date | str | None = None,
) -> bool:
    """Whether the ESPN athlete page ``espn_name`` is our fighter ``our_name``.

    The guard of the ESPN history import (espn_fight_history.backfill): a
    wrong yes injects a stranger's career into a fighter card, a wrong no only
    leaves a card without its regional history. In order:

    * both names carry a generational suffix and they differ (Jr. vs Sr.,
      II vs III) -> NO;
    * both birth years are known and more than one year apart (father and
      son with the same name) -> NO;
    * the whole names reach IDENTITY_THRESHOLD -> yes (the old guard);
    * the names without the suffix reach it -> yes ("Michael Aswell Jr." /
      "Michael Aswell");
    * the same words in another order -> yes ("Xiong Jingnan" /
      "Jingnan Xiong");
    * exactly the same birth date (year and the unordered {day, month}) AND
      a shared word of 3+ letters that is not a particle -> yes ("Daniel
      Spohn" / "Dan Spohn", "Jose Souza" / "Jose Henrique").

    A date missing on either side simply disables the rules that use it.
    """
    our_base, our_suffix = split_generational_suffix(our_name)
    espn_base, espn_suffix = split_generational_suffix(espn_name)
    if our_suffix and espn_suffix and our_suffix != espn_suffix:
        return False

    our_key = birth_key(our_birth)
    espn_key = birth_key(espn_birth)
    if our_key and espn_key and abs(our_key[0] - espn_key[0]) > _MAX_BIRTH_YEAR_GAP:
        return False

    if fold_ratio(our_name, espn_name) >= IDENTITY_THRESHOLD:
        return True
    if ratio(our_base, espn_base) >= IDENTITY_THRESHOLD:
        return True
    our_tokens = our_base.split()
    espn_tokens = espn_base.split()
    if our_tokens and sorted(our_tokens) == sorted(espn_tokens):
        return True
    if our_key and our_key == espn_key:
        shared = {
            token for token in set(our_tokens) & set(espn_tokens)
            if len(token) >= _MIN_BIRTH_SHARED_TOKEN_LEN and token not in _PARTICLE_TOKENS
        }
        if shared:
            return True
    return False
