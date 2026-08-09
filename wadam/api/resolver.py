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

from wadam.domain.models import ChatConfig, contact_id_for, phone_digits


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


def _last4(chat: ChatConfig) -> str:
    """A chat's last four digits, from its stored number if it has one and from
    its name otherwise.

    The stored number comes first because it is the one somebody typed in
    deliberately; the name is only a number at all for an unsaved contact."""
    return contact_id_for(chat.phone_number) or contact_id_for(chat.chat_name)


def resolve_chat(chats: Iterable[ChatConfig], identifier: str) -> Resolution:
    wanted = (identifier or "").strip()
    if not wanted:
        return Resolution()
    lowered = wanted.casefold()
    all_chats = list(chats)

    digits = phone_digits(wanted)

    tiers = (
        ("external_id", lambda c: (c.external_id or "").strip().casefold() == lowered),
        # A full number is the most specific thing anyone can send, so it ranks
        # above every abbreviation. Compared as digits on both sides:
        # "+91 94231 55555" and "919423155555" are the same contact, and a
        # caller should not have to know which spelling this application stores.
        ("phone_number", lambda c: bool(digits) and phone_digits(c.phone_number) == digits),
        ("contact_last4", lambda c: bool(_last4(c)) and _last4(c) == wanted),
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
    """What this chat's contact ID should default to — the last four digits of
    its number, or "" when neither the stored number nor the name gives us
    anything to work with."""
    return _last4(chat)
