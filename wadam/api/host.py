"""Wiring the send API to the engine.

Everything policy-shaped lives here rather than in the HTTP layer: which chat an
identifier resolves to, what each failure means in HTTP terms, and how long to
wait for a send before giving up. `server.py` stays a transport.

The threading is the same pattern the UI uses. An HTTP request arrives on one of
the server's worker threads, submits a coroutine to the engine's event loop with
`run_coroutine_threadsafe`, and blocks on the resulting future. The engine loop
is never blocked — only the request thread is, which is exactly what an HTTP
caller waiting for a definitive answer wants.
"""

from __future__ import annotations

import concurrent.futures
import logging

from wadam.api.resolver import resolve_chat
from wadam.api.server import SendApiServer, SendResponse
from wadam.config import Settings
from wadam.engine.engine import AutomationEngine
from wadam.storage.repository import Repository

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
                "error": f"'{identifier}' matches {len(resolution.candidates)} chats. "
                         f"Set a distinct contact ID on one of them.",
                "candidates": list(resolution.candidates),
            })

        if not resolution.ok:
            return SendResponse(404, {
                "ok": False, "code": "chat_not_found",
                "error": f"No chat matches '{identifier}'. The contact ID is the last four "
                         f"digits of the number; for a saved contact, set it in the "
                         f"configuration panel.",
            })

        chat = resolution.chat
        try:
            future = self._engine.submit(
                lambda: self._engine.send_message(chat.chat_id, text, origin="api")
            )
        except Exception as ex:  # noqa: BLE001
            return SendResponse(503, {"ok": False, "code": "engine_unavailable",
                                      "error": str(ex)})

        try:
            outcome = future.result(timeout=self._settings.api_send_timeout)
        except concurrent.futures.TimeoutError:
            # The send may still complete after this: it holds the automation
            # lock and will run to its own conclusion. Saying "timed out" rather
            # than "failed" is the honest description, and the chat's activity
            # panel will show what actually happened.
            return SendResponse(504, {
                "ok": False, "code": "timeout",
                "error": f"The send did not complete within "
                         f"{self._settings.api_send_timeout:.0f}s. It may still be in "
                         f"progress — check the chat before retrying.",
                "chat": chat.chat_name,
            })
        except Exception as ex:  # noqa: BLE001 - the engine raised
            logger.exception("Send API request failed inside the engine")
            return SendResponse(500, {"ok": False, "code": "internal",
                                      "error": f"{type(ex).__name__}: {ex}"})

        if not outcome.ok:
            # 502: the request was fine, the downstream (WhatsApp) did not
            # deliver. Distinguishing this from a 4xx matters — the caller
            # should retry this one, and not the others.
            return SendResponse(502, {
                "ok": False, "code": "send_failed", "error": outcome.error,
                "chat": chat.chat_name, "chat_id": chat.chat_id,
            })

        return SendResponse(200, {
            "ok": True,
            "id": identifier,
            "chat": chat.chat_name,
            "chat_id": chat.chat_id,
            "matched_by": resolution.matched_by,
            "strategy": outcome.strategy,
            "message_key": outcome.message_key,
        })
