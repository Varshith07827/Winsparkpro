"""Sending a message — over HTTP, to a running OpenWA instance.

This is the whole of what `wadam/whatsapp/sender.py` used to be. That file was
1,692 lines: a ladder of UI Automation fallbacks (InvokePattern, then a
viewport-checked coordinate click, then the search box), a second ladder for
filling the compose box (ValuePattern, then clipboard paste, then per-character
Unicode input), foreground forcing, and a verifier that counted outgoing
bubbles to prove the message actually landed.

None of that is needed against an API. A send is a POST, and its response says
whether it worked.

One piece of hard-won knowledge does carry over, and it is the reason
`send_text` refuses to build a chat id: sending to the wrong person is the one
failure that must never happen quietly.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


class SendError(RuntimeError):
    """A send that did not happen. Carries the HTTP status when there was one."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class OpenWAClient:
    """Sends messages and reads session state through OpenWA's REST API."""

    def __init__(self, base_url: str, api_key: str, session_id: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session_id = session_id
        self._timeout = timeout

    # ── the send path ─────────────────────────────────────────────────

    @property
    def send_url(self) -> str:
        """Where a text message goes. Exposed so a misconfigured session id is
        visible in a log line rather than only in a 404."""
        return f"{self._base_url}/api/sessions/{self._session_id}/messages/send-text"

    def send_text(self, chat_id: str, text: str) -> dict:
        """Send `text` to `chat_id`. Raises SendError if it did not go.

        `chat_id` is OpenWA's identifier, passed through exactly as it arrived,
        and is never composed from a phone number. WhatsApp's LID addressing
        (`216298915164281@lid`) is not derivable from digits, so building one
        would be sending to a guess. This is the same rule winSpark enforced by
        refusing an ambiguous id with a 409 rather than picking a chat.
        """
        if not chat_id:
            raise SendError("refusing to send without a chat id")

        return self._post(self.send_url, {"chatId": chat_id, "text": text})

    # ── session state, for the status bar ─────────────────────────────

    def session_status(self) -> dict:
        """The session's current state, or `{}` if OpenWA cannot be reached.

        Never raises: this drives a status indicator that polls on a timer, and
        an unreachable gateway is a thing to *display*, not an exception to
        handle at every call site.
        """
        try:
            sessions = self._get("/api/sessions")
        except SendError as error:
            return {"status": "unreachable", "error": str(error)}

        rows = sessions if isinstance(sessions, list) else sessions.get("data", [])
        for row in rows:
            if row.get("id") == self._session_id:
                return row
        return {"status": "missing", "error": f"session {self._session_id} not found"}

    # ── transport ─────────────────────────────────────────────────────

    def _request(self, method: str, url: str, body: Optional[dict] = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "x-api-key": self._api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise SendError(f"OpenWA returned {error.code}: {detail}", status=error.code) from error
        except urllib.error.URLError as error:
            raise SendError(f"cannot reach OpenWA at {self._base_url}: {error.reason}") from error
        except json.JSONDecodeError as error:
            # The request may well have succeeded; only the response was
            # unreadable. Surfaced rather than swallowed, and never retried.
            raise SendError(f"unreadable response from OpenWA: {error}") from error

    def _get(self, path: str):
        return self._request("GET", f"{self._base_url}{path}")

    def _post(self, url: str, body: dict):
        return self._request("POST", url, body)
