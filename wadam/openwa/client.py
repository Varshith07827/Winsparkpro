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

    # ── the directory ─────────────────────────────────────────────────

    def list_chats(self) -> list[dict]:
        """Every chat OpenWA can see, newest first.

        These carry `@lid` ids. Contacts carry `@c.us` ids for the same people,
        which is why `resolve_phone` exists — the two id spaces only join
        through a phone number.
        """
        return _rows(self._get(f"/api/sessions/{self._session_id}/chats"), "chats")

    def list_contacts(self, page_size: int = 1000) -> list[dict]:
        """The whole address book, paginated and deduplicated.

        Both are necessary and neither is optional:

        * **Paginated.** The endpoint caps at 1000 rows and says nothing about
          it. Measured on a live account: 1000 rows on the first page, 815 on
          the second. Reading one page silently loses nearly half the address
          book, and the failure looks like "that contact does not exist".
        * **Deduplicated by id.** Every contact comes back twice — once with
          the phone in `number`, once with a LID-shaped value. 1815 rows were
          501 people. The row carrying the phone is preferred, since that is
          the one that can be matched against a number.
        """
        rows: list[dict] = []
        offset = 0
        while True:
            page = _rows(
                self._get(f"/api/sessions/{self._session_id}/contacts"
                          f"?limit={page_size}&offset={offset}"),
                "contacts",
            )
            if not page:
                break
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        best: dict[str, dict] = {}
        for row in rows:
            contact_id = row.get("id")
            if not contact_id:
                continue
            current = best.get(contact_id)
            if current is None or _looks_like_phone(row.get("number"), contact_id):
                best[contact_id] = row
        return list(best.values())

    def resolve_phone(self, contact_id: str) -> str:
        """The phone number behind a chat id, or "" if OpenWA cannot say.

        Best-effort by OpenWA's own description, and about a second per call —
        so callers cache the answer. A LID's phone number does not change.
        """
        try:
            answer = self._get(
                f"/api/sessions/{self._session_id}/contacts/{contact_id}/phone")
        except SendError as error:
            logger.debug("could not resolve %s to a phone: %s", contact_id, error)
            return ""
        return str(answer.get("phone") or "") if isinstance(answer, dict) else ""

    def check_number(self, number: str) -> str:
        """The chat id for a phone number, or "" if it is not on WhatsApp.

        This is what makes a number reachable when no chat exists for it yet.
        """
        digits = "".join(c for c in (number or "") if c.isdigit())
        if not digits:
            return ""
        try:
            answer = self._get(
                f"/api/sessions/{self._session_id}/contacts/check/{digits}")
        except SendError as error:
            logger.debug("could not check %s: %s", digits, error)
            return ""
        if not isinstance(answer, dict) or not answer.get("exists"):
            return ""
        return str(answer.get("whatsappId") or "")

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
        """GET a path relative to the base URL."""
        return self._request("GET", f"{self._base_url}{path}")

    def _post(self, url: str, body: dict):
        return self._request("POST", url, body)


def _rows(payload, key: str) -> list[dict]:
    """The list inside a response, whatever shape it arrived in.

    OpenWA returns a bare array on some endpoints and `{key: [...]}` or
    `{data: [...]}` on others, and which is which has changed between
    releases.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for candidate in (key, "data", "items"):
            value = payload.get(candidate)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _looks_like_phone(number, contact_id: str) -> bool:
    """Is this the duplicate row carrying the real phone number?

    A contact's two rows differ only in `number`: one holds the phone, the
    other a LID-shaped value that matches nothing. When the id is `<digits>@c.us`
    the phone is in the id, so the row agreeing with it is the useful one.
    """
    digits = "".join(c for c in str(number or "") if c.isdigit())
    if not digits:
        return False
    local = contact_id.split("@", 1)[0]
    return digits == local
