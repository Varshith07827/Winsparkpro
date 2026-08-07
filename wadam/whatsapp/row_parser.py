"""Parses a WhatsApp Desktop chat-list row's accessible Name into fields.

Chromium flattens every descendant's text into the row's Name, so one row is a
single undelimited string:

    "4 unread messages Vishnu Cr Gvp Yesterday ekada grp names navi unaye..."
    "CSE - C Yesterday Chaitu: https://chat.whatsapp.com/... Pinned chat"

There is no separator between chat name, timestamp and message preview, so this
is a tuned heuristic rather than a grammar: strip the "View status" avatar
prefix, then the "N unread messages" prefix, then trailing flag phrases, then
split on the first day/time "anchor". It was written against real rows captured
from a live WhatsApp Desktop instance, and it will misparse a chat name that
happens to contain a day name or a time-like substring.
"""

from __future__ import annotations

import re

# WhatsApp labels a row's avatar "View status" when that contact has an unseen
# status, and Chromium flattens that button's name in ahead of everything else:
# "View status 2 unread messages Hasini …". Strip it first or it becomes part
# of the chat name.
_STATUS_PREFIX = re.compile(r"^View status\b[\s,]*", re.IGNORECASE)

_UNREAD_PREFIX = re.compile(r"^(\d+)\s+unread messages?\s+")

_TRAILING_FLAGS: tuple[tuple[str, str], ...] = (
    (" Starred chat", "is_starred"),
    (" Pinned chat", "is_pinned"),
    (" Muted chat", "is_muted"),
    (" Draft message", "is_draft"),
)

_ANCHOR = re.compile(
    r"\b(?:Yesterday|Today|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
    r"|\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?|\d{1,2}/\d{1,2}/\d{2,4})\b"
)

# A group's preview is prefixed with the speaker's name ("Chaitu: hello"); a
# one-to-one chat's is not, unless it's our own message ("You: hello"). Used as
# a hint only — a contact whose message starts "Re: something" would look like
# a group, which costs nothing but a badge.
_SPEAKER_PREFIX = re.compile(r"^(?!You:)[^:]{1,32}:\s")


def parse_chat_row(raw_text: str) -> dict:
    """Returns a dict of parsed fields. Kept as a plain dict so this module can
    be unit-tested with no other imports."""
    text = raw_text.strip()
    text = _STATUS_PREFIX.sub("", text, count=1)

    unread_count = 0
    match = _UNREAD_PREFIX.match(text)
    if match:
        unread_count = int(match.group(1))
        text = text[match.end():]

    flags = {"is_pinned": False, "is_muted": False, "is_starred": False, "is_draft": False}
    changed = True
    while changed:
        changed = False
        for phrase, flag_name in _TRAILING_FLAGS:
            if text.endswith(phrase):
                text = text[: -len(phrase)]
                flags[flag_name] = True
                changed = True

    anchor_match = _ANCHOR.search(text)
    if anchor_match:
        chat_name = text[: anchor_match.start()].strip()
        timestamp_text = anchor_match.group(0)
        last_message = text[anchor_match.end():].strip()
    else:
        chat_name = text.strip()
        timestamp_text = ""
        last_message = ""

    return {
        "chat_name": chat_name,
        "timestamp_text": timestamp_text,
        "last_message": last_message,
        "unread_count": unread_count,
        "raw_text": raw_text,
        "looks_like_group": bool(_SPEAKER_PREFIX.match(last_message)),
        **flags,
    }
