"""
TeleSeek helper functions.
"""

import re
import unicodedata


# Maximum Telegram callback_data length — 64 bytes
_CALLBACK_DATA_MAX = 64


def cb_query(query: str, max_len: int = 44) -> str:
    """
    Truncate a query to a safe length for use in callback_data.

    Telegram limit: 64 bytes for the whole callback_data.
    Our longest prefix: "page:10:documents:" = 18 characters.
    Remaining for the query: 64 - 18 = 46. We take 44 for headroom.

    Args:
        query: Search query.
        max_len: Maximum query length in the callback (default 44).

    Returns:
        Truncated query (without a "..." suffix).
    """
    return query[:max_len]


def safe_callback(*parts: str, sep: str = ":") -> str:
    """
    Build callback_data from parts and check its length.
    If it exceeds 64 bytes, the last part (query) is truncated.

    Args:
        *parts: callback_data parts (prefix, type, query, etc.).
        sep: Separator.

    Returns:
        A callback_data string no longer than 64 bytes.
    """
    result = sep.join(parts)
    if len(result.encode("utf-8")) <= _CALLBACK_DATA_MAX:
        return result

    # Truncate the last part (query) to fit the limit
    prefix = sep.join(parts[:-1]) + sep
    available = _CALLBACK_DATA_MAX - len(prefix.encode("utf-8"))
    # Truncate by bytes, but safely for Unicode
    last = parts[-1].encode("utf-8")[:available].decode("utf-8", errors="ignore")
    return prefix + last


def sanitize_query(query: str) -> str:
    """
    Clean a search query of potentially dangerous characters.

    - Removes Unicode control characters
    - Collapses repeated whitespace
    - Truncates to 100 characters

    Args:
        query: Raw user input.

    Returns:
        Cleaned query.
    """
    # Remove control characters (categories Cc, Cf)
    cleaned = "".join(
        ch for ch in query
        if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    # Collapse repeated whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Limit length
    return cleaned[:100]


def is_valid_query(query: str) -> bool:
    """
    Check that the query is non-empty and has at least one meaningful character.

    Args:
        query: Cleaned query.

    Returns:
        True if the query is valid.
    """
    if not query or len(query.strip()) < 1:
        return False
    # A query consisting only of punctuation or special characters is invalid
    meaningful = re.sub(r"[^\w\s]", "", query, flags=re.UNICODE).strip()
    return len(meaningful) >= 1
