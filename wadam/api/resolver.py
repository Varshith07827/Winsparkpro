"""Turning the `id` in a send request into exactly one chat.

The whole file exists to make one guarantee: **an ambiguous identifier is
refused, never guessed.** Sending a message to the wrong person is the single
worst thing this application could do, and it is a quiet failure — the caller
gets a 200 and nobody finds out until the wrong contact replies.

Matching runs in tiers, most specific first:

    1. phone_number   — the full number. A one-to-one chat.
    2. chat_id        — the application's own 24-char identifier.
    3. chat name      — exact, case-insensitive. How a GROUP is addressed,
                        because a group has no number to be addressed by.

A tier that matches exactly one chat resolves. A tier that matches several
stops everything and reports the conflict by name. A tier that matches nothing
falls through to the next.

**The four-digit short id is gone.** It was derived from the last four digits
of a number and used as the primary way to address a chat, and four digits is
10,000 values — with a few hundred chats a collision is not a remote
possibility but a likelihood (the birthday bound puts it above even odds at
~118 chats). It bought nothing a full number does not: the number is what a
caller already has, it is unambiguous, and it is what the endpoint was tested
with. An abbreviation whose only property is that it might silently address
the wrong person is not worth keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from wadam.domain.models import ChatConfig, phone_digits


@dataclass(frozen=True)
class Resolution:
    chat: Optional[ChatConfig] = None
    matched_by: str = ""
    candidates: tuple[str, ...] = ()   # names, when ambiguous

    @property
    def ok(self) -> bool:
        return self.chat is not None

    @property
    def ambiguous(self) -> bool:
        return self.chat is None and len(self.candidates) > 1


def resolve_chat(chats: Iterable[ChatConfig], identifier: str) -> Resolution:
    wanted = (identifier or "").strip()
    if not wanted:
        return Resolution()
    lowered = wanted.casefold()
    all_chats = list(chats)

    digits = phone_digits(wanted)

    tiers = (
        # Compared as digits on both sides: "+91 94231 55555" and
        # "919423155555" are the same contact, and a caller should not have to
        # know which spelling this application happens to store.
        ("phone_number", lambda c: bool(digits) and phone_digits(c.phone_number) == digits),
        ("chat_id", lambda c: c.chat_id == wanted),
        ("chat_name", lambda c: c.chat_name.strip().casefold() == lowered),
    )

    for name, predicate in tiers:
        matches = [c for c in all_chats if predicate(c)]
        if len(matches) == 1:
            return Resolution(chat=matches[0], matched_by=name)
        if len(matches) > 1:
            # Do not fall through to a later tier hoping for a cleaner answer.
            # Two chats genuinely answer to this identifier, and picking one is
            # the mistake this function exists to prevent.
            return Resolution(candidates=tuple(sorted(c.chat_name for c in matches)))
    return Resolution()
