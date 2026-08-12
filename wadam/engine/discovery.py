"""Automatic chat discovery.

Every polling cycle:

    detect chats → compare with MongoDB → new chat? → create configuration
    (automation ON, webhook derived) → save to MongoDB → write JSON →
    appear immediately

No dialogs, no confirmation, no "add chat" button. A chat exists in this
application because it exists in WhatsApp.

A newly discovered chat is **watched**. This used to be off, on the principle
that discovery is not consent, and the result was an application that did
nothing at all until every box had been ticked by hand — for a tool whose only
job is watching chats, an off switch disguised as a default.

What makes that safe is `seeded`, which is unchanged and now carries the whole
weight of it: the first read of a chat records its entire visible backlog
without triggering anything. So a chat is watched from the moment it appears,
and the conversation that was already on screen when it appeared is not.

Unticking a chat is therefore the deliberate act, and it deletes what the chat
stored — see `Repository.purge_chat_records`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Iterable

from wadam.config import Settings
from wadam.domain.models import (
    ChatConfig,
    chat_id_for,
    phone_digits,
    utcnow,
)
from wadam.domain.webhook_url import webhook_url_for
from wadam.storage.repository import Repository
from wadam.whatsapp.name_rules import is_system_or_list_view_title
from wadam.whatsapp.reader import ChatRow

logger = logging.getLogger(__name__)


def _confirmed_group(chat: ChatConfig) -> bool:
    """A group according to the INFO PANEL, not according to the sidebar.

    `is_group` is a guess until the panel has been read; `phone_probed_at` is
    what marks it as having been read. Acting on the guess would let a preview
    reading "Re: the invoice" withhold a real contact's number."""
    return chat.is_group and chat.phone_probed_at is not None


def row_signature(row: ChatRow) -> str:
    """A hash of everything the sidebar shows for a chat. When it changes,
    something happened in that conversation — a new message, a read receipt, a
    draft. It's the cheap "is this chat worth opening?" test that keeps the
    three-second poll from opening every chat every cycle."""
    raw = f"{row.raw_text}|{row.unread_count}|{row.timestamp_text}|{row.last_message}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class DiscoveryResult:
    seen: list[ChatConfig]
    new: list[ChatConfig]
    changed: list[ChatConfig]

    @property
    def seen_ids(self) -> list[str]:
        return [c.chat_id for c in self.seen]


class ChatDiscovery:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings

    def sync(self, rows: Iterable[ChatRow]) -> DiscoveryResult:
        """Reconcile a sidebar reading with what's stored. Blocking (it talks to
        MongoDB); the engine calls it on a worker thread."""
        seen: list[ChatConfig] = []
        new: list[ChatConfig] = []
        changed: list[ChatConfig] = []

        for row in rows:
            name = (row.chat_name or "").strip()
            if not name or is_system_or_list_view_title(name):
                continue

            chat_id = chat_id_for(name)
            signature = row_signature(row)
            chat = self._repo.get_chat(chat_id)

            if chat is None:
                chat = ChatConfig(
                    chat_id=chat_id,
                    chat_name=name,
                    # Resolvable only when the sidebar shows a number, which is
                    # the case for a contact that is NOT in the address book.
                    # Left empty otherwise — never invented, because the
                    # webhook URL is built from it.
                    phone_number=phone_digits(name),
                    webhook_url="",
                    # ON. A chat is discovered because it is in the sidebar, and
                    # a tool whose whole job is watching chats that starts by
                    # watching none of them makes every user tick every box.
                    #
                    # Safe only because of seeding: the first read of a new chat
                    # records its entire visible backlog as SEEDED and returns
                    # nothing to automate, so switching this on cannot fire a
                    # webhook for a conversation that happened before install.
                    automation_enabled=True,
                    seeded=False,
                )
                self._apply_row(chat, row, signature)
                self._refresh_webhook(chat)
                new.append(chat)
                changed.append(chat)
                seen.append(chat)
                continue

            if chat.row_signature != signature:
                self._apply_row(chat, row, signature)
                self._refresh_webhook(chat)
                changed.append(chat)
            elif self._refresh_webhook(chat):
                # The template or the number changed under a chat whose sidebar
                # row did not. Without this, editing WEBHOOK_URL would only
                # reach chats that happened to receive a message afterwards.
                changed.append(chat)
            seen.append(chat)

        if changed:
            self._repo.save_chats(changed)
        for chat in new:
            self._repo.log("INFO", "chat.discovered", chat_id=chat.chat_id, chat_name=chat.chat_name,
                           message="New chat discovered — automation ON, number "
                                   + (chat.phone_number or "unresolved"))
        if seen:
            self._repo.touch_last_poll([c.chat_id for c in seen], utcnow())

        return DiscoveryResult(seen=seen, new=new, changed=changed)

    def _refresh_webhook(self, chat: ChatConfig) -> bool:
        """Rebuild the chat's URL from the template. True when it changed."""
        wanted = webhook_url_for(self._settings.webhook_template,
                                 chat.phone_number, chat.webhook_override,
                                 chat.chat_name)
        if wanted == chat.webhook_url:
            return False
        chat.webhook_url = wanted
        return True

    @staticmethod
    def _apply_row(chat: ChatConfig, row: ChatRow, signature: str) -> None:
        chat.chat_name = row.chat_name or chat.chat_name
        if not chat.phone_number and not _confirmed_group(chat):
            # Backfill, and pick up a chat whose name has since become a number
            # (an address-book entry removed). Never overwrites a resolved one.
            #
            # Skipped for a confirmed group: a group has no single contact, so
            # any number is somebody else's. Only the panel can confirm one —
            # the sidebar guess is not allowed to withhold a number on its own.
            chat.phone_number = phone_digits(chat.chat_name)
        chat.last_message_preview = row.last_message
        chat.timestamp_text = row.timestamp_text
        chat.unread_count = row.unread_count
        chat.is_pinned = row.is_pinned
        chat.is_muted = row.is_muted
        # A HINT, and only until the info panel has said what this chat is.
        #
        # It used to be sticky (`is_group or looks_like_group`) on the reasoning
        # that a group's preview only carries a speaker prefix when someone else
        # spoke last. True, but it also made a single false positive permanent —
        # a one-to-one chat whose contact wrote "Re: the invoice" was a group
        # forever. Measured: Varshith, a 1:1 chat with a readable Contact info
        # panel, was flagged as a group.
        #
        # `phone_probed_at` marks the panel as having been read, and the panel
        # is authoritative, so from that point the guess is not allowed to
        # overwrite what was actually seen.
        if chat.phone_probed_at is None:
            chat.is_group = row.looks_like_group
        chat.row_signature = signature
        chat.updated_at = utcnow()
