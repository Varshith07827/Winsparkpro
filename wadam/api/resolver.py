"""Turning the `id` in a send request into exactly one chat.

The whole file exists to make one guarantee: **an ambiguous identifier is
refused, never guessed.** Sending a message to the wrong person is the single
worst thing this application could do, and it is a quiet failure — the caller
gets a 200 and nobody finds out until the wrong contact replies.

Four digits is 10,000 values. With a few hundred chats a collision is not a
remote possibility but a likelihood (the birthday bound puts it above even odds
at ~118 chats), so this is a real operational concern, not defensive paranoia.

Matching runs in tiers, most deliberate first:

    1. external_id       — someone typed this in for this chat. Explicit wins.
    2. last 4 digits     — derived from a chat whose name is a phone number.
    3. chat_id           — the application's own 24-char identifier.
    4. chat name         — exact, case-insensitive.

A tier that matches exactly one chat resolves. A tier that matches several
stops everything and reports the conflict by name. A tier that matches nothing
falls through to the next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from wadam.domain.models import ChatConfig, contact_id_for


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

    tiers = (
        ("external_id", lambda c: (c.external_id or "").strip().casefold() == lowered),
        ("contact_last4", lambda c: bool(contact_id_for(c.chat_name))
                                    and contact_id_for(c.chat_name) == wanted),
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


def suggest_external_id(chat: ChatConfig) -> str:
    """What this chat's contact ID should default to — its number's last four
    digits, or "" when the name gives us nothing to work with."""
    return contact_id_for(chat.chat_name)
