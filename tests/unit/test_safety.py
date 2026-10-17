"""
Unit tests for the query blocklist MECHANISM (bot/safety.py).

The term lists in bot/safety.py are a neutral demo set, so these tests check
the mechanism, not specific words: normalisation, case, separators, leetspeak,
Cyrillic homoglyphs, repeated characters, Unicode tricks, Arabic-script
normalisation, combo markers and the numeric pattern — plus the absence of
false positives on ordinary queries.

Pure Python: imports only bot.safety (no config, no database).
Run: python -m pytest tests/unit
"""

import pytest

from bot.safety import is_blocked_query

# Must be BLOCKED, grouped by the evasion class being exercised.
BLOCK = {
    "exact token": ["zqx", "qzx", "find zqx channels"],
    "prefix term and inflections": ["blockedterm", "blockedterms", "forbiddentermvideos", "bannedterm links"],
    "case": ["ZQX", "BlockedTerm", "FORBIDDENTERM"],
    "per-character separators": ["z q x", "z.q.x", "z-q-x", "b l o c k e d t e r m"],
    "leetspeak": ["bl0ck3dt3rm", "f0rbidd3nterm", "b4nnedterm", "$ome blockedterm"],
    # Cyrillic homoglyphs inside a Latin word (test fixture: о, с, е are Cyrillic).
    "cyrillic homoglyphs": ["blосkedterm", "zqх", "forbiddеnterm"],
    "repeated characters": ["zzqqxx", "bloockeddterm", "zqqqx"],
    "unicode compatibility forms": ["ｚｑｘ", "ｂｌｏｃｋｅｄｔｅｒｍ"],
    "combo, spaced and glued": ["alphaword betaword", "betaword alphaword", "alphawordbetaword", "gammaword deltaword"],
    "numeric pattern": ["12 dmu", "7dmu", "15-dmu", "3 demo units"],
    # Arabic-script normalisation of the demo term "محظور".
    "arabic plain": ["محظور"],
    "arabic with article": ["المحظور"],
    "arabic diacritics": ["مَحْظُور"],
    "arabic tatweel": ["محــظــور"],
    "arabic presentation forms": ["ﻣﺤﻈﻮﺭ"],
    "arabic per-character": ["م ح ظ و ر"],
    "arabic-indic digits in numeric pattern": ["١٢ dmu"],
}

# Must NOT be blocked.
ALLOW = {
    "ordinary queries": ["news", "business", "youtube", "games", "weather", "crypto"],
    # Multilingual ordinary queries (test fixtures in their own scripts).
    "multilingual": ["новости", "жаңалықтар", "yangiliklar", "اخبار", "قنوات الطبخ"],
    "near misses of exact token": ["zqxy", "azqx", "zq", "qx"],
    "substrings of prefix terms": ["blocked", "term", "banned", "forbidden", "termination"],
    "combo markers alone": ["alphaword", "betaword", "alphaword news", "deltaword gammaone"],
    "numbers without the unit": ["windows 10", "top 100", "year 2026", "iphone 15", "12 units"],
    "arabic near miss": ["محظوظ", "حظر"],
    "arabic article guard": ["الله", "لله"],
    "empty": ["", "   "],
}


def _cases(table):
    return [pytest.param(q, id=f"{group}:{q}") for group, items in table.items() for q in items]


@pytest.mark.parametrize("query", _cases(BLOCK))
def test_blocked(query: str) -> None:
    assert is_blocked_query(query), f"expected to be blocked: {query!r}"


@pytest.mark.parametrize("query", _cases(ALLOW))
def test_allowed(query: str) -> None:
    assert not is_blocked_query(query), f"false positive: {query!r}"


def test_none_is_allowed() -> None:
    assert not is_blocked_query(None)  # type: ignore[arg-type]
