"""Wires the send API to OpenWA.

The API is the second way to send: something that wants to *push* a message in,
rather than answer one that arrived. `SendApiServer` owns the socket and knows
nothing about WhatsApp; this module is the policy.

**Resolution is a lookup, never a construction.** `wadam/api/resolver.py` used
to search chats by phone number, chat id and name, because a UI-Automation send
addressed a chat by whatever string could be found on screen. An OpenWA chat id
is exact, so an `id` that is already one is used as-is, and anything else is
matched against chats this application has actually seen. An identifier
matching more than one chat is **refused with 409**, never delivered to a guess
— that rule is inherited verbatim, because sending to the wrong person is the
one failure that must not happen quietly.

The response is not sent until the message is: the request blocks on the HTTP
call to OpenWA, so a 200 means the gateway accepted it.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from wadam.api.server import SendApiServer, SendResponse
from wadam.config import Settings
from wadam.domain.models import ChatConfig, phone_digits
from wadam.engine.service import AutomationService
from wadam.openwa import SendError
from wadam.storage.repository import Repository

logger = logging.getLogger(__name__)


class SendApiHost:
    """Starts the listener when a port is configured, and does the sending."""

    def __init__(self, settings: Settings, repository: Repository,
                 service: AutomationService) -> None:
        self._settings = settings
        self._repo = repository
        self._service = service
        self.server = SendApiServer(
            host=settings.api_host,
            port=settings.api_port,
            token=settings.api_token,
            send=self._send,
            # No status endpoint: there is no queue to look a message up in. A
            # send either happened by the time the request returns, or it did
            # not and the response says so.
            status=None,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._settings.api_port)

    def start(self) -> None:
        if not self.enabled:
            return
        self.server.start()
        logger.info("send API listening on %s", self.server.url)

    def stop(self) -> None:
        if self.enabled:
            self.server.stop()

    # ── resolution ────────────────────────────────────────────────────

    def _resolve(self, identifier: str) -> tuple[Optional[ChatConfig], List[ChatConfig]]:
        """Find the chat an identifier names.

        Returns `(chat, candidates)`. A chat is returned only when exactly one
        matched; `candidates` carries the ambiguity so the caller can say what
        it refused to choose between.
        """
        wanted = identifier.strip()
        chats = self._repo.list_chats()

        exact = [c for c in chats if c.chat_id == wanted]
        if exact:
            return exact[0], exact

        digits = phone_digits(wanted)
        matches = [
            c for c in chats
            if c.chat_name.strip().casefold() == wanted.casefold()
            or (digits and c.phone_number == digits)
        ]
        if len(matches) == 1:
            return matches[0], matches
        return None, matches

    def _send(self, identifier: str, text: str) -> SendResponse:
        chat, candidates = self._resolve(identifier)

        if chat is None and len(candidates) > 1:
            return SendResponse(409, {
                "ok": False, "code": "ambiguous",
                "error": (f"{identifier!r} matches {len(candidates)} chats. "
                          f"Use the chat id instead."),
                "candidates": [c.chat_id for c in candidates],
            })

        if chat is None:
            # An identifier shaped like an OpenWA chat id is passed through
            # even when unknown: a chat this application has never received a
            # message from is still a real chat, and refusing would make the
            # API useless for starting a conversation.
            if "@" in identifier:
                target, chat_name = identifier, identifier
            else:
                return SendResponse(404, {
                    "ok": False, "code": "unknown_chat",
                    "error": (f"No chat matches {identifier!r}. Use a full OpenWA chat id "
                              f"(e.g. 918985370703@c.us) to reach one this application "
                              f"has not seen yet."),
                })
        else:
            target, chat_name = chat.chat_id, chat.chat_name

        try:
            self._service.client.send_text(target, text)
        except SendError as error:
            logger.error("send API could not send to %s: %s", target, error)
            return SendResponse(502, {"ok": False, "code": "send_failed", "error": str(error)})

        if chat is not None:
            self._service.pipeline.record_outgoing(chat, text, origin="api")
            self._service.publish()

        return SendResponse(200, {"ok": True, "chat": chat_name, "chatId": target})
