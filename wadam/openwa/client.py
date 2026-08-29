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
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

#: Ceiling on one media transfer. Separate from the MediaStore's cap: this one
#: bounds what is held in memory while downloading, before anything has decided
#: whether the file is worth keeping.
_MAX_MEDIA_BYTES = 64 * 1024 * 1024


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
        (`111111111111111@lid`) is not derivable from digits, so building one
        would be sending to a guess. This is the same rule winSpark enforced by
        refusing an ambiguous id with a 409 rather than picking a chat.
        """
        if not chat_id:
            raise SendError("refusing to send without a chat id")

        return self._post(self.send_url, {"chatId": chat_id, "text": text})

    # ── media ─────────────────────────────────────────────────────────

    #: Which send endpoint a kind of media goes to. WhatsApp treats these as
    #: genuinely different message types — an image sent as a document renders
    #: as a file card with no preview — so the choice is not cosmetic.
    MEDIA_ENDPOINTS = {
        "image": "send-image",
        "video": "send-video",
        "audio": "send-audio",
        "ptt": "send-audio",
        "voice": "send-audio",
        "document": "send-document",
        "sticker": "send-sticker",
    }

    @staticmethod
    def kind_for(mimetype: str, filename: str = "") -> str:
        """The kind of media `mimetype` should be sent as.

        Document is the fallback rather than image, because it is the only kind
        that carries an arbitrary payload without WhatsApp trying to decode it:
        an unrecognised type sent as an image is refused or renders broken,
        whereas anything at all can be a document.
        """
        major = (mimetype or "").split("/", 1)[0].strip().lower()
        if major in ("image", "video", "audio"):
            return major
        return "document"

    def download_media(self, chat_id: str, message_id: str) -> tuple[bytes, str]:
        """The bytes of a message's media, and its mimetype.

        Raises SendError, including for the 404 OpenWA returns when it has
        nothing stored — which is a normal answer rather than a fault: media
        download can be switched off on the instance, the payload may have been
        over its cap when it arrived, and a URL-based send never stores bytes
        at all. The caller records the reason and keeps the message.
        """
        if not chat_id or not message_id:
            raise SendError("need both a chat id and a message id to fetch media")
        return self._request_bytes(
            f"{self._base_url}/api/sessions/{self._session_id}"
            f"/messages/{_quote(chat_id)}/{_quote(message_id)}/media")

    def send_media(self, chat_id: str, kind: str, *, url: str = "", base64_data: str = "",
                   mimetype: str = "", filename: str = "", caption: str = "") -> dict:
        """Send media to `chat_id`. Raises SendError if it did not go.

        Exactly one of `url` or `base64_data`. OpenWA lets base64 win when both
        are present, so passing both is not an error there — it is only a way
        to be surprised later about which one went. This refuses instead.
        """
        if not chat_id:
            raise SendError("refusing to send without a chat id")
        if bool(url) == bool(base64_data):
            raise SendError("send_media needs exactly one of url or base64_data")

        endpoint = self.MEDIA_ENDPOINTS.get((kind or "").lower())
        if endpoint is None:
            raise SendError(
                f"{kind!r} is not a kind of media this can send. One of: "
                + ", ".join(sorted(set(self.MEDIA_ENDPOINTS))))

        body: dict = {"chatId": chat_id}
        if url:
            body["url"] = url
        else:
            body["base64"] = base64_data
        if mimetype:
            body["mimetype"] = mimetype
        if filename:
            body["filename"] = filename
        # Audio and sticker sends render no caption. WhatsApp drops it silently
        # rather than refusing, which is the worse of the two outcomes — the
        # caller believes they sent something that nobody will ever see.
        if caption and endpoint not in ("send-audio", "send-sticker"):
            body["caption"] = caption

        return self._post(
            f"{self._base_url}/api/sessions/{self._session_id}/messages/{endpoint}", body)

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

    # ── provisioning ──────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """Every session on this OpenWA instance."""
        return _rows(self._get("/api/sessions"), "sessions")

    def list_webhooks(self, session_id: str) -> list[dict]:
        return _rows(self._get(f"/api/sessions/{session_id}/webhooks"), "webhooks")

    def ensure_webhook(self, session_id: str, url: str, secret: str,
                       events=("message.received",)) -> str:
        """Make sure OpenWA will deliver to `url`, and return the webhook's id.

        Idempotent, and deliberately narrow: it only ever touches a webhook
        whose URL is exactly ours. Any other webhook on the session belongs to
        something else and is left alone.

        This exists because registering it by hand was a curl invocation with
        four fields that had to agree with `.env` — and when the secret did not
        match, every delivery was refused with a 401 that looked like a bug in
        this application.
        """
        body = {"url": url, "events": list(events), "retryCount": 3}
        if secret:
            body["secret"] = secret

        for row in self.list_webhooks(session_id):
            if row.get("url") == url:
                webhook_id = str(row.get("id"))
                self._request("PUT",
                              f"{self._base_url}/api/sessions/{session_id}/webhooks/{webhook_id}",
                              body)
                return webhook_id

        created = self._post(
            f"{self._base_url}/api/sessions/{session_id}/webhooks", body)
        return str(created.get("id") or "") if isinstance(created, dict) else ""

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

    def _request_bytes(self, url: str) -> tuple[bytes, str]:
        """GET raw bytes and the Content-Type. Used only for media.

        Separate from `_request` rather than a flag on it: that one sets a JSON
        Content-Type, decodes as UTF-8 and parses the result, and every one of
        those steps is wrong for a JPEG. `read()` is bounded because the
        response is written to disk and nothing upstream promises a size.
        """
        request = urllib.request.Request(
            url, method="GET", headers={"x-api-key": self._api_key})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                data = response.read(_MAX_MEDIA_BYTES + 1)
                if len(data) > _MAX_MEDIA_BYTES:
                    raise SendError(
                        f"media at {url} is larger than the {_MAX_MEDIA_BYTES} byte "
                        f"transfer limit")
                mimetype = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                return data, mimetype
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise SendError(f"OpenWA returned {error.code}: {detail}", status=error.code) from error
        except urllib.error.URLError as error:
            raise SendError(f"cannot reach OpenWA at {self._base_url}: {error.reason}") from error

    def _get(self, path: str):
        """GET a path relative to the base URL."""
        return self._request("GET", f"{self._base_url}{path}")

    def _post(self, url: str, body: dict):
        return self._request("POST", url, body)


def _quote(value: str) -> str:
    """One path segment, escaped.

    Chat ids and WhatsApp message ids contain "@" and "+", and message ids also
    contain "/" — which without this ends the segment early and turns a media
    fetch into a request for a route that does not exist.
    """
    return urllib.parse.quote(str(value or ""), safe="")


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
