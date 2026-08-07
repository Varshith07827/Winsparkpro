"""Matching a requested chat name against a sidebar row's name.

The sidebar truncates long names and search results render them slightly
differently, so an exact comparison isn't enough to answer "is this the chat I
asked for?". `chat_names_match` is truncation-tolerant but deliberately strict
about it: a prefix match only counts with enough coverage, at a word boundary,
and with at least two matching leading words — because the cost of a false
positive here is sending a message to the wrong person.
"""

from __future__ import annotations

import re

_SIDEBAR_NOISE = {
    s.lower()
    for s in (
        "WhatsApp", "Search", "Chats", "Updates", "Channels", "Communities", "Calls",
        "Settings", "Archived", "Starred", "All", "Unread", "Groups", "Favourites",
        "Favorites", "Search or start new chat", "Get WhatsApp for Windows",
        "Starred messages", "Starred Messages",
    )
}

_WORD_SPLIT_RE = re.compile(r"[ \-–—]+")


def is_system_or_list_view_title(title: str | None) -> bool:
    """Whether a "chat name" is really a piece of WhatsApp's own navigation."""
    if not title or not title.strip():
        return True
    trimmed = title.strip()
    if trimmed.lower() in _SIDEBAR_NOISE:
        return True
    lower = trimmed.lower()
    return (
        "starred" in lower
        or "archived" in lower
        or lower.startswith("calls")
        or lower.startswith("settings")
        or "search results" in lower
    )


def chat_names_match(requested: str | None, candidate: str | None) -> bool:
    if not requested or not requested.strip() or not candidate or not candidate.strip():
        return False

    left = _normalize_name(requested)
    right = _normalize_name(candidate)

    if left.lower() == right.lower():
        return True

    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) < 4:
        return False

    if not longer.lower().startswith(shorter.lower()):
        return False

    coverage = len(shorter) / len(longer)
    if coverage < 0.65 and len(longer) - len(shorter) > 3:
        return False

    if len(longer) > len(shorter):
        boundary = longer[len(shorter)]
        at_word_boundary = boundary in (" ", "-", ".", "…", "(", ")")
        if not at_word_boundary and coverage < 0.80:
            return False

    shorter_words = _get_words(shorter)
    longer_words = _get_words(longer)
    if len(shorter_words) >= 2 and len(longer_words) >= 2:
        matched = 0
        for a, b in zip(shorter_words, longer_words):
            if a.lower() != b.lower():
                break
            matched += 1
        if matched < 2:
            return False

    return True


def _normalize_name(value: str) -> str:
    return value.strip().rstrip(".… ")


def _get_words(value: str) -> list[str]:
    return [w for w in _WORD_SPLIT_RE.split(value.strip()) if w]
