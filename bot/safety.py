"""
Search query blocklist (query safety).

Purpose: queries that match the blocklist are never executed (not a single
database round-trip); the user gets a neutral reply. The check sits at the
SINGLE entry point shared by every search path (``_build_response`` in
handlers/search.py: new query, pagination, filter/sort change, content-filter
toggle) plus an early guard in the text handler (before the "Searching..."
message).

How it works (defence against simple evasion, NOT a cryptographic boundary):
  1. Normalisation: lower-case → fold "yo" (U+0451) to "ye" (U+0435) → digit/symbol
     substitutions (0→o, 1→i, 3→e, 4→a, 5→s, 7→t, @→a, $→s) → tokenise on
     non-letters (dots/dashes/spaces disappear) → merge runs of single-letter
     tokens ("z q x" → "zqx").
  2. Second projection: Cyrillic homoglyphs → Latin (U+0430→a, U+0441→c, ...)
     — catches mixed keyboard layouts.
  3. Collapse repeated characters inside a token ("zzqqx" → "zqx") as an extra
     form of each token; BOTH forms are checked.
  4. Matching: short exact terms match a whole token; longer terms match a
     token prefix (catches inflections and glued words); combo phrases block
     only when a marker from group A AND a marker from group B occur in the
     same query; a numeric pattern is matched by regex on the form that still
     has digits.

Term lists: the lists in this file are a NEUTRAL DEMO SET that only exercises
the mechanism. In production the real term lists are kept in a private source
and are not part of this repository. Extending a list means adding a line to
the matching tuple below — the logic does not change.

Logging: the text of a blocked query is NEVER written anywhere (neither to
search_log nor to loggers). Only the fact is recorded — the
``BLOCKED_LOG_MARKER`` value in search_log (timestamp + user_id).
has_results=True for the marker is intentional: False would make it show up
in the panel's "content gaps" report, and such queries are noise, not demand.
"""

import re
import unicodedata

# Marker stored in search_log instead of the blocked query text.
BLOCKED_LOG_MARKER = "[blocked]"


class BlockedQueryError(Exception):
    """The query is blocked by the blocklist — the search is not executed.

    The query text is deliberately NOT stored in the exception (so it never
    leaks into tracebacks or logs)."""


# ─────────────────────────────────────────────────────────────────────────────
# Terms — NEUTRAL DEMO SET (production lists live in a private source).
# ─────────────────────────────────────────────────────────────────────────────

# Exact short terms — must equal a WHOLE token (after normalisation and after
# collapsing repeats). Short terms must not be prefix/substring-matched — they
# would hit ordinary words.
_EXACT_TOKENS: frozenset[str] = frozenset({
    "zqx", "qzx",
})

# Prefix terms — the token STARTS WITH the term (catches inflections, plurals
# and glued forms such as "<term>videos"). Only long/unambiguous terms belong here.
_PREFIX_TOKENS: tuple[str, ...] = (
    "blockedterm",
    "forbiddenterm",
    "bannedterm",
    # Arabic-script demo term ("forbidden") — exercises the Arabic normaliser below.
    "محظور",
)

# Combos: the query is blocked only if it contains a marker from group A AND a
# marker from group B (substrings of the glued normalised string — catches both
# the spaced "alphaword betaword" and the glued "alphawordbetaword"). Each
# marker on its own is an ordinary word and must not block anything.
_COMBO_A_MARKERS: tuple[str, ...] = (
    "alphaword", "gammaword",
)
_COMBO_B_MARKERS: tuple[str, ...] = (
    "betaword", "deltaword",
)

# Numeric pattern: a number + a unit word, matched on the normalised string WITH
# digits (in the rest of the matching digits have already been substituted or
# dropped). Demo unit: "dmu" / "demo unit(s)".
_NUMERIC_PATTERN_RE = re.compile(
    r"(?<!\d)(?:[1-9]\d?)\s*[-–]?\s*"
    r"(?:dmu\b|demo\s*units?\b)"
)

# ─────────────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────────────

# Digit/symbol substitutions ("bl0ck3dterm", "$..."). Applied before tokenisation.
_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s", "!": "i",
})

# Cyrillic homoglyphs → Latin (second projection, for mixed layouts).
# Written as escapes: a, c, e, o, p, x, y, k in Cyrillic.
_HOMOGLYPH_MAP = str.maketrans({
    "\u0430": "a", "\u0441": "c", "\u0435": "e", "\u043e": "o",
    "\u0440": "p", "\u0445": "x", "\u0443": "y", "\u043a": "k",
})

# ─── Arabic script ───────────────────────────────────────────────────────────
# Why Arabic needs its own step: the letter class used for tokenisation used to
# be Latin + Cyrillic only, so an Arabic string tokenised into NOTHING and no
# check could fire at all.
#
# Evasion tricks specific to Arabic script and how they are closed:
#   1. DIACRITICS / vowel marks (U+064B-U+0652, U+0670): invisible when read,
#      but they change the bytes — easy to "mask" a word. → removed.
#   2. TATWEEL (U+0640) — decorative stretching: looks like the same word,
#      different bytes. → removed.
#   3. LETTER VARIANTS: alef with hamza/madda (أ إ آ ٱ) ↔ bare ا; alef maksura
#      ى ↔ ي; ta marbuta ة ↔ ه; hamza carriers ؤ/ئ ↔ و/ي. The writer may pick any
#      form — for matching it is the same word. → folded to the base letter.
#   4. ARABIC-INDIC DIGITS (٠-٩, Persian ۰-۹) — otherwise the numeric pattern
#      would miss them. → converted to ASCII BEFORE the numeric check.
#   5. Unicode PRESENTATION FORMS (U+FB50-FDFF, U+FE70-FEFF) — the same letters
#      at "typographic" code points. → removed by NFKC normalisation.
#   6. The DEFINITE ARTICLE ال is glued to the front of a word (which is also
#      why prefix search cannot strip it). → each token is additionally checked
#      WITHOUT the article.
#
# Why NFKC is enough as a first step: it maps compatibility code points to
# canonical ones (presentation forms, ligatures such as ﻻ, full-width
# characters); what remains is stripping diacritics and unifying letter
# variants — which NFKC is not required to do, because Unicode treats those as
# different letters, not as different spellings.
_ARABIC_DIACRITICS_RE = re.compile(r"[ً-ْٓ-ٰٕـ]")
_ARABIC_LETTER_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ٲ": "ا", "ٳ": "ا",
    "ى": "ي", "ئ": "ي", "ی": "ي",          # alef maksura and Persian yeh
    "ة": "ه",                                # ta marbuta
    "ؤ": "و",
    "ك": "ك", "ک": "ك",                      # Persian kaf → Arabic kaf
    "ء": "",                                 # a standalone hamza carries no difference for matching
})
# Arabic-Indic and Persian digits → ASCII (before the numeric check).
_ARABIC_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ARABIC_ARTICLE = "ال"


# Fast path: does the string contain any Arabic script at all? The vast
# majority of queries are Latin/Cyrillic, and running the Arabic transforms on
# them is wasted work (without this check the pre-check got measurably slower
# on a Latin-only benchmark).
_HAS_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def _normalize_arabic(s: str) -> str:
    """Strip diacritics/tatweel and fold letter variants to base letters."""
    if not _HAS_ARABIC_RE.search(s):
        return s
    s = _ARABIC_DIACRITICS_RE.sub("", s)
    return s.translate(_ARABIC_LETTER_MAP)


# ─── Extending to new scripts ────────────────────────────────────────────────
# Adding a script = add its normaliser and letter range here, plus terms in the
# shared tuples above. The LOGIC (_tokenize/_match_*/is_blocked_query) stays the same.
_SCRIPT_NORMALIZERS: tuple = (_normalize_arabic,)
# Letter ranges for tokenisation: Latin, Cyrillic (U+0430-U+044F) and Arabic
# (after normalisation only code points U+0621-U+064A remain).
_LETTER_RANGES = "a-z\u0430-\u044fء-ي"

_NON_LETTER_RE = re.compile(f"[^{_LETTER_RANGES}]+")
_REPEAT_RE = re.compile(r"(.)\1+")


def _tokenize(s: str) -> list[str]:
    """Tokens of the normalised string, merging runs of single-letter tokens
    ("z q x" / "z.q.x" → "zqx") — closes the character-splitting evasion."""
    raw = [tok for tok in _NON_LETTER_RE.split(s) if tok]
    merged: list[str] = []
    buf = ""
    for tok in raw:
        if len(tok) == 1:
            buf += tok
            continue
        if buf:
            merged.append(buf)
            buf = ""
        merged.append(tok)
    if buf:
        merged.append(buf)
    return merged


def _token_forms(tok: str) -> tuple[str, ...]:
    """Forms of a token to check: as is, with collapsed repeats, and without the
    Arabic article ال (the article is glued to the FRONT, so prefix matching
    alone does not remove it). The article is only dropped if a meaningful
    remainder is left — otherwise "الله" would become a meaningless stub."""
    collapsed = _REPEAT_RE.sub(r"\1", tok)
    forms = [tok, collapsed]
    for base in (tok, collapsed):
        if base.startswith(_ARABIC_ARTICLE) and len(base) > len(_ARABIC_ARTICLE) + 1:
            forms.append(base[len(_ARABIC_ARTICLE):])
    return tuple(dict.fromkeys(forms))


def _match_tokens(tokens: list[str]) -> bool:
    """Check tokens (in all their forms) against the exact and prefix term lists."""
    for tok in tokens:
        for form in _token_forms(tok):
            if form in _EXACT_TOKENS:
                return True
            if any(form.startswith(p) for p in _PREFIX_TOKENS):
                return True
    return False


def _match_combo(joined: str) -> bool:
    """A group-A marker together with a group-B marker in one string."""
    if any(m in joined for m in _COMBO_A_MARKERS):
        return any(s in joined for s in _COMBO_B_MARKERS)
    return False


def is_blocked_query(query: str) -> bool:
    """True → the query is blocked: do NOT run the search, do NOT log the text.

    Cheap (pure Python, no DB, no regexes over large strings): it runs on every
    search entry and does not change the latency of the healthy path."""
    # NFKC removes presentation forms/ligatures/full-width characters, then the
    # per-script normalisers run (currently Arabic). `is_normalized` is a cheap
    # check: ordinary Latin/Cyrillic strings are already normalised, so the full
    # NFKC pass is skipped.
    raw = query or ""
    if not unicodedata.is_normalized("NFKC", raw):
        raw = unicodedata.normalize("NFKC", raw)
    lowered = raw.lower().replace("\u0451", "\u0435").translate(_ARABIC_DIGITS_MAP)
    for normalize in _SCRIPT_NORMALIZERS:
        lowered = normalize(lowered)

    # Numeric pattern — before leet substitutions (digits are still in place,
    # Arabic ones already converted to ASCII).
    if _NUMERIC_PATTERN_RE.search(lowered):
        return True

    leeted = lowered.translate(_LEET_MAP)
    folded = leeted.translate(_HOMOGLYPH_MAP)

    for projection in (leeted, folded):
        tokens = _tokenize(projection)
        if _match_tokens(tokens):
            return True
        joined = "".join(tokens)
        collapsed_joined = _REPEAT_RE.sub(r"\1", joined)
        if _match_combo(joined) or _match_combo(collapsed_joined):
            return True
    return False
