"""Wires the send API to OpenWA.

The API is the second way to send: something that wants to *push* a message in,
rather than answer one that arrived. `SendApiServer` owns the socket and knows
nothing about WhatsApp; this module is the policy.

**Resolution is a lookup, never a construction**, and it lives in
`engine/directory.py` rather than here — the window and this API must agree
about what a name means, and two implementations of "which chat is this?" is
two chances to disagree silently.

So an `id` may be a chat id, a phone number with its country code, or a contact
name, and it reaches anyone in the address book rather than only chats that
have already spoken. An identifier matching more than one is **refused with
409**, never delivered to a guess: sending to the wrong person is the one
failure that must not happen quietly.

The response is not sent until the message is: the request blocks on the HTTP
call to OpenWA, so a 200 means the gateway accepted it.
"""

from __future__ import annotations

import logging
from wadam.api.server import SendApiServer, SendResponse
from wadam.config import Settings
from wadam.domain.models import ChatConfig
from wadam.engine.pipeline import MediaError
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

    # Resolution itself lives in `Directory`, because the window and this API
    # must agree about what a name means. Two implementations of "which chat is
    # this?" is two chances to disagree, and the disagreement would be silent.

    def _send(self, identifier: str, text: str, media=None) -> SendResponse:
        answer = self._service.directory.resolve(identifier)

        if answer.ambiguous:
            return SendResponse(409, {
                "ok": False, "code": "ambiguous",
                "error": (f"{identifier!r} matches {len(answer.candidates)} chats. "
                          f"Use the chat id instead."),
                "candidates": list(answer.candidates),
            })

        if not answer.ok:
            # An identifier shaped like a chat id is passed through even when
            # unknown: a chat this application has never seen is still a real
            # chat, and refusing would make the API useless for starting one.
            if "@" in identifier:
                target, chat_name = identifier, identifier
            else:
                return SendResponse(404, {
                    "ok": False, "code": "unknown_chat", "error": answer.reason,
                })
        else:
            target, chat_name = answer.chat_id, answer.display

        chat = self._repo.get_chat(target)

        recorded = text
        try:
            if media is not None:
                # Through the pipeline, not the client: `send_media` is where
                # the confinement rule for local paths lives, and routing round
                # it here would give the API a second, weaker answer to the
                # question of which files may leave this machine.
                sent = self._service.pipeline.send_media(target, media, caption=text)
                recorded = text or f"[{media.kind or 'media'}] {sent}"
            else:
                self._service.client.send_text(target, text)
        except MediaError as error:
            # Refused before OpenWA was asked, so the request is what is wrong.
            # 502 here would send the caller to read the gateway's logs for a
            # send it never saw.
            logger.warning("send API refused media for %s: %s", target, error)
            return SendResponse(400, {"ok": False, "code": "bad_media", "error": str(error)})
        except SendError as error:
            logger.error("send API could not send to %s: %s", target, error)
            return SendResponse(502, {"ok": False, "code": "send_failed", "error": str(error)})

        if chat is not None:
            self._service.pipeline.record_outgoing(chat, recorded, origin="api")
            self._service.publish()

        return SendResponse(200, {"ok": True, "chat": chat_name, "chatId": target})
