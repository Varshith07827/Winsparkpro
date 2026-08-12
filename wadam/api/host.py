"""Wiring the send API to the engine.

Everything policy-shaped lives here rather than in the HTTP layer: which chat an
identifier resolves to, and what each failure means in HTTP terms. `server.py`
stays a transport.

**A request is bounded by the enqueue, never by the send.** An HTTP request
arrives on one of the server's worker threads, submits a coroutine to the
engine's loop with `run_coroutine_threadsafe`, and waits only for the message to
be written to the queue — about a millisecond. The physical send costs seconds
and happens afterwards, on the engine's single drainer.

This used to block on the send itself, and the arithmetic was fatal: at ~17s per
send a burst of twenty needs almost six minutes, so every caller past the third
got a `timeout` response for a message that was in fact delivered. A caller that
retried on timeout would have duplicated real messages. Delivery is now reported
through `GET /wam/status/<outgoing_id>` instead of through the response to the
send.
"""

from __future__ import annotations

import concurrent.futures
import logging

from wadam.api.resolver import resolve_chat
from wadam.api.server import SendApiServer, SendResponse
from wadam.config import Settings
from wadam.engine.engine import AutomationEngine
from wadam.storage.repository import Repository

# How long to wait for the ENGINE to accept a message, not for
# WhatsApp to send it. Enqueue is two sub-millisecond writes.
_ENQUEUE_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger(__name__)


class SendApiHost:
    def __init__(self, settings: Settings, repository: Repository,
                 engine: AutomationEngine) -> None:
        self._settings = settings
        self._repo = repository
        self._engine = engine
        self.server = SendApiServer(
            host=settings.api_host,
            port=settings.api_port,
            token=settings.api_token,
            send=self._send,
            status=self.status,
        )

    @property
    def enabled(self) -> bool:
        return self._settings.api_port > 0

    def start(self) -> None:
        if not self.enabled:
            return
        self.server.start()
        if self.server.authentication_required:
            self._repo.log("INFO", "api.started",
                           message=f"Send API listening on {self.server.url}")
        else:
            # Not silent. Configuration only permits this on loopback, but the
            # operator should still see it in the log every time it starts.
            logger.warning("Send API is running WITHOUT a token on %s — any process on this "
                           "machine can send WhatsApp messages", self.server.url)
            self._repo.log("WARNING", "api.started",
                           message=f"Send API listening on {self.server.url} with no token "
                                   f"(loopback only — any local process can send messages).")

    def stop(self) -> None:
        if self.server.running:
            self.server.stop()

    # -- the callback the HTTP layer invokes -------------------------------

    def _send(self, identifier: str, text: str) -> SendResponse:
        resolution = resolve_chat(self._repo.list_chats(), identifier)

        if resolution.ambiguous:
            # Two chats answer to this identifier. Picking one is the mistake
            # this refuses to make; the caller is told both names so they can
            # give one of them a distinct contact ID.
            self._repo.log(
                "WARNING", "api.ambiguous_id",
                message=f"'{identifier}' matches {len(resolution.candidates)} chats: "
                        + ", ".join(resolution.candidates),
            )
            return SendResponse(409, {
                "ok": False, "code": "ambiguous_id",
                # The advice has to be something the caller can actually do.
                # Two chats sharing an exact name is the only way to get here
                # now that abbreviated ids are gone, so the answer is the number
                # for a one-to-one chat and the chat_id for a group.
                "error": f"'{identifier}' matches {len(resolution.candidates)} chats "
                         f"and nothing was sent. Address this chat by its full "
                         f"phone number, or by its chat_id if it is a group.",
                "candidates": list(resolution.candidates),
                "resolves_by": ["phone_number", "chat_id", "chat_name"],
            })

        if not resolution.ok:
            return SendResponse(404, {
                "ok": False, "code": "chat_not_found",
                "error": f"No chat matches '{identifier}'. Use the chat's full "
                         f"phone number, or its exact name as shown in WhatsApp "
                         f"(which is how a group is addressed, since a group has "
                         f"no number).",
            })

        chat = resolution.chat
        try:
            future = self._engine.submit(
                lambda: self._engine.queue_message(chat.chat_id, text, origin="api")
            )
        except Exception as ex:  # noqa: BLE001
            return SendResponse(503, {"ok": False, "code": "engine_unavailable",
                                      "error": str(ex)})

        try:
            # Bounded by the ENQUEUE, not by the send. Writing to Mongo and the
            # JSON mirror takes about a millisecond; if that cannot finish in
            # ten seconds the machine has a much larger problem.
            outcome = future.result(timeout=_ENQUEUE_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return SendResponse(503, {
                "ok": False, "code": "busy",
                "error": "The engine did not accept the message within "
                         f"{_ENQUEUE_TIMEOUT_SECONDS:.0f}s. Nothing was queued; "
                         "this one is safe to retry.",
                "chat": chat.chat_name,
            })
        except Exception as ex:  # noqa: BLE001 - the engine raised
            logger.exception("Send API request failed inside the engine")
            return SendResponse(500, {"ok": False, "code": "internal",
                                      "error": f"{type(ex).__name__}: {ex}"})

        if not outcome.ok:
            return SendResponse(502, {
                "ok": False, "code": "send_failed", "error": outcome.error,
                "chat": chat.chat_name, "chat_id": chat.chat_id,
            })

        # 202, not 200: the message is ACCEPTED and durably queued, which is a
        # different promise from "delivered". Callers that need delivery ask
        # GET /wam/status/<outgoing_id>; the queue retries transport failures
        # and verifies arrival against the conversation on its own.
        return SendResponse(202, {
            "ok": True,
            "status": "queued",
            "id": identifier,
            "chat": chat.chat_name,
            "chat_id": chat.chat_id,
            "matched_by": resolution.matched_by,
            "outgoing_id": outcome.outgoing_id,
            "status_url": f"/wam/status/{outcome.outgoing_id}",
        })

    def status(self, outgoing_id: str) -> SendResponse:
        """What happened to one queued message.

        The states worth acting on: `delivered` is confirmed present in the
        conversation; `failed` exhausted its retries and never left the compose
        box, so it is safe to resend; `unverified` left the box but was never
        found in the chat, and is deliberately NOT retried automatically —
        resending risks a duplicate, which is the worse failure."""
        message = self._engine.outgoing_status(outgoing_id)
        if message is None:
            return SendResponse(404, {
                "ok": False, "code": "unknown_id",
                "error": f"No queued message with id '{outgoing_id}'.",
            })
        return SendResponse(200, {
            "ok": True,
            "outgoing_id": message.outgoing_id,
            "status": str(message.status),
            "chat": message.chat_name,
            "chat_id": message.chat_id,
            "text": message.text,
            "attempts": message.attempts,
            "verification": message.verification or "",
            "error": message.error or "",
            "queued_at": message.created_at.isoformat() if message.created_at else "",
            "delivered_at": message.delivered_at.isoformat() if message.delivered_at else "",
        })
