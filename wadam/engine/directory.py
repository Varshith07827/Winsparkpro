"""The directory: every chat, every contact, and how to find one.

Two jobs that belong together because the second depends entirely on the first.

**Sync** pulls OpenWA's chat list and address book into MongoDB. **Resolve**
turns whatever a caller typed — a name, a number, a chat id — into the one chat
id a message can be sent to.

---

## Why this is not a one-line lookup

The two id spaces do not match. A chat is `216298915164281@lid`; the same
person in the address book is `917981149423@c.us`. Nothing in either id says
they are the same person, and a LID cannot be turned into a number by
inspection.

The only join is `GET /contacts/{lid}/phone`, which costs about a second per
call. So the phone is resolved once per chat, when the chat is first seen, and
cached forever — a LID's phone number does not change.

That gives the chain a lookup actually walks:

    "Prasanthi Gvpt"  ──▶ contacts  ──▶ 919100251854  ──▶ chat 132671606911101@lid
    "919100251854"    ──▶ chat by cached phone ──▶ 132671606911101@lid
    unknown number    ──▶ GET /contacts/check/{number} ──▶ 132671606911101@lid

---

## Refusing rather than guessing

`resolve` returns every candidate it found, and the caller refuses when there is
more than one. On a real address book that is rare — 4 shared names out of 494
— but "rare" is not "never", and the failure it prevents is sending someone
else's message to the wrong person.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import List, Optional

from wadam.domain.models import ChatConfig, Contact, phone_digits, utcnow
from wadam.openwa import OpenWAClient
from wadam.storage.repository import Repository

logger = logging.getLogger(__name__)


def normalize_phone(value: str) -> str:
    """Digits only, or "" if this does not look like a phone number.

    Callers are expected to include the country code — `919100251854`,
    `+91 91002 51854` and `91-9100-251854` are the same number here. A bare
    ten-digit national number is deliberately NOT expanded: the account may
    message Indian and US numbers, and guessing between +91 and +1 would send
    to a real person who is not the intended one.
    """
    return phone_digits(value)


@dataclass(frozen=True)
class Resolution:
    """What an identifier resolved to."""

    chat_id: str = ""
    display: str = ""
    candidates: tuple = ()
    """Every match found. More than one means the caller must refuse."""

    reason: str = ""
    """Why nothing was resolved, when `chat_id` is empty."""

    @property
    def ok(self) -> bool:
        return bool(self.chat_id)

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


class Directory:
    """Keeps chats and contacts in step with OpenWA, and answers lookups."""

    def __init__(self, repository: Repository, client: OpenWAClient) -> None:
        self._repo = repository
        self._client = client
        self._lock = threading.Lock()

    # ── sync ──────────────────────────────────────────────────────────

    def sync(self) -> dict:
        """Pull the chat list and address book. Returns a summary.

        Never raises: this runs on a timer and at startup, and an OpenWA that
        is briefly unreachable is a thing to log and retry, not a reason to
        fail a launch. The previous contents stay in MongoDB either way.
        """
        with self._lock:
            summary = {"chats": 0, "new_chats": 0, "contacts": 0, "phones_resolved": 0}
            try:
                contacts = self._sync_contacts()
                summary["contacts"] = contacts
                chats, new_chats, resolved = self._sync_chats()
                summary.update(chats=chats, new_chats=new_chats, phones_resolved=resolved)
            except Exception:  # noqa: BLE001
                logger.exception("directory sync failed")
                summary["error"] = "sync failed; see the log"
                return summary

            logger.info("directory: %d chats (%d new), %d contacts, %d phones resolved",
                        summary["chats"], summary["new_chats"],
                        summary["contacts"], summary["phones_resolved"])
            return summary

    def _sync_contacts(self) -> int:
        rows = self._client.list_contacts()
        contacts = []
        for row in rows:
            contact_id = str(row.get("id") or "")
            if not contact_id:
                continue
            contacts.append(Contact(
                contact_id=contact_id,
                name=str(row.get("name") or ""),
                push_name=str(row.get("pushName") or ""),
                phone_number=normalize_phone(str(row.get("number") or ""))
                             or normalize_phone(contact_id.split("@", 1)[0]),
                is_my_contact=bool(row.get("isMyContact")),
            ))
        return self._repo.save_contacts(contacts)

    def _sync_chats(self) -> tuple[int, int, int]:
        rows = self._client.list_chats()
        seen = new = resolved = 0

        for row in rows:
            chat_id = str(row.get("id") or "")
            if not chat_id:
                continue
            seen += 1

            chat = self._repo.get_chat(chat_id)
            if chat is None:
                # Automation OFF. A sync that switched chats on would start
                # answering every conversation in the account at once.
                chat = ChatConfig(chat_id=chat_id, automation_enabled=False)
                new += 1

            chat.chat_name = str(row.get("name") or "") or chat.chat_name
            chat.is_group = bool(row.get("isGroup")) or chat_id.endswith("@g.us")
            preview = str(row.get("lastMessage") or "")
            if preview:
                chat.last_message_preview = preview

            # Resolved once, then never again — the call is ~1s and the answer
            # cannot change.
            if not chat.phone_number:
                phone = normalize_phone(self._client.resolve_phone(chat_id))
                if phone:
                    chat.phone_number = phone
                    resolved += 1

            if chat.phone_number:
                contact = self._repo.contact_by_phone(chat.phone_number)
                if contact is not None:
                    chat.contact_name = contact.name or contact.push_name

            chat.updated_at = utcnow()
            self._repo.save_chat(chat)

        return seen, new, resolved

    # ── resolve ───────────────────────────────────────────────────────

    def resolve(self, identifier: str) -> Resolution:
        """Turn a chat id, phone number or contact name into a chat id.

        Tried in order of how certain each is. An exact chat id is beyond
        doubt; a name is the least certain and the only one that regularly
        finds more than one answer.
        """
        wanted = (identifier or "").strip()
        if not wanted:
            return Resolution(reason="no identifier given")

        chats = self._repo.list_chats()

        # 1. An exact chat id.
        for chat in chats:
            if chat.chat_id == wanted:
                return Resolution(chat.chat_id, self._display(chat), (chat.chat_id,))

        # 2. A phone number, against chats first so an existing conversation
        #    is preferred over starting a new one.
        digits = normalize_phone(wanted)
        if digits:
            matches = [c for c in chats if c.phone_number == digits]
            if len(matches) == 1:
                return Resolution(matches[0].chat_id, self._display(matches[0]),
                                  (matches[0].chat_id,))
            if len(matches) > 1:
                return Resolution(candidates=tuple(c.chat_id for c in matches),
                                  reason=f"{wanted!r} matches {len(matches)} chats")

        # 3. A contact name — against chats, then the address book.
        named = [c for c in chats
                 if wanted.casefold() in (c.contact_name.strip().casefold(),
                                          c.chat_name.strip().casefold())]
        if len(named) == 1:
            return Resolution(named[0].chat_id, self._display(named[0]), (named[0].chat_id,))
        if len(named) > 1:
            return Resolution(candidates=tuple(c.chat_id for c in named),
                              reason=f"{wanted!r} matches {len(named)} chats")

        contacts = self._repo.contacts_named(wanted)
        if len(contacts) > 1:
            return Resolution(candidates=tuple(c.contact_id for c in contacts),
                              reason=f"{wanted!r} matches {len(contacts)} contacts")
        if len(contacts) == 1 and contacts[0].phone_number:
            digits = contacts[0].phone_number

        # A number with no country code cannot be resolved without guessing
        # one. India and the US both use ten national digits, so "9100251854"
        # is a real person in either country and picking would send to whoever
        # the guess landed on. Checked before the live lookup, so the caller is
        # told what is actually wrong rather than "not on WhatsApp".
        if digits and len(digits) <= 10:
            return Resolution(reason=(f"{wanted!r} has no country code — send the full "
                                      f"international number, e.g. 919100251854"))

        # 4. A number with no chat yet. OpenWA answers with the chat id it
        #    would use, which is what makes a first message possible.
        if digits:
            chat_id = self._client.check_number(digits)
            if chat_id:
                return Resolution(chat_id, digits, (chat_id,))
            return Resolution(reason=f"{digits} is not on WhatsApp")

        return Resolution(reason=f"nothing matches {wanted!r}")

    @staticmethod
    def _display(chat: ChatConfig) -> str:
        return chat.contact_name or chat.chat_name or chat.phone_number or chat.chat_id
