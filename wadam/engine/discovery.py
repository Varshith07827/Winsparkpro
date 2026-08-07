"""Automatic chat discovery.

Every polling cycle:

    detect chats → compare with MongoDB → new chat? → create configuration
    (automation OFF, webhook empty) → save to MongoDB → write JSON →
    appear immediately

No dialogs, no confirmation, no "add chat" button. A chat exists in this
application because it exists in WhatsApp.

A newly discovered chat is deliberately inert: automation OFF and — unless
`DEFAULT_WEBHOOK` is configured — no webhook. Discovery is not consent. The
`seeded` flag carries the same idea to messages: the first read of a chat
records its visible backlog without triggering anything, so turning automation
on tomorrow doesn't fire a webhook at every message already on screen.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Iterable

from wadam.config import Settings
from wadam.domain.models import ChatConfig, chat_id_for, contact_id_for, utcnow
from wadam.storage.repository import Repository
from wadam.whatsapp.name_rules import is_system_or_list_view_title
from wadam.whatsapp.reader import ChatRow

logger = logging.getLogger(__name__)


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
                    webhook_url=self._settings.default_webhook,
                    automation_enabled=False,   # never on by discovery
                    # The send API's default addressing: the last four digits of
                    # the contact's number. Derivable only when the chat name IS
                    # the number, which is the case for an unsaved contact; a
                    # saved one shows its name and gets this typed in.
                    external_id=contact_id_for(name),
                    seeded=False,
                )
                self._apply_row(chat, row, signature)
                new.append(chat)
                changed.append(chat)
                seen.append(chat)
                continue

            if chat.row_signature != signature:
                self._apply_row(chat, row, signature)
                changed.append(chat)
            seen.append(chat)

        if changed:
            self._repo.save_chats(changed)
        for chat in new:
            self._repo.log("INFO", "chat.discovered", chat_id=chat.chat_id, chat_name=chat.chat_name,
                           message="New chat discovered — automation OFF, webhook "
                                   + (chat.webhook_url or "empty"))
        if seen:
            self._repo.touch_last_poll([c.chat_id for c in seen], utcnow())

        return DiscoveryResult(seen=seen, new=new, changed=changed)

    @staticmethod
    def _apply_row(chat: ChatConfig, row: ChatRow, signature: str) -> None:
        chat.chat_name = row.chat_name or chat.chat_name
        if not chat.external_id:
            # Backfill: chats stored before contact IDs existed, and chats whose
            # name has since become a number. Never overwrites one someone set
            # by hand — an explicit assignment outranks anything derived.
            chat.external_id = contact_id_for(chat.chat_name)
        chat.last_message_preview = row.last_message
        chat.timestamp_text = row.timestamp_text
        chat.unread_count = row.unread_count
        chat.is_pinned = row.is_pinned
        chat.is_muted = row.is_muted
        # Sticky: a group's preview only carries a speaker prefix when someone
        # else spoke last, so a single "You: …" preview must not un-group it.
        chat.is_group = chat.is_group or row.looks_like_group
        chat.row_signature = signature
        chat.updated_at = utcnow()
