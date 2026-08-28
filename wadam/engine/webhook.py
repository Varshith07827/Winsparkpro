"""Calling your endpoint, and understanding whatever it says back.

This is winSpark's contract, restored. An incoming message in a switched-on
chat is POSTed to that chat's URL, and whatever comes back is sent to the chat
through OpenWA. What sits behind that URL — a rules engine, a language model, a
person with a keyboard — is your business and completely invisible here.

The application is a bridge. It does not decide anything.

---

## Being lenient about the answer

Five shapes are accepted, because the endpoint should be as simple as you like
and a bridge that only understood one of them would be a bridge you had to
write code for:

    {"reply": "Confirmed"}      {"message": …}      {"text": …}
    {"data": {"reply": …}}      "Confirmed"         Confirmed

**An empty reply is a successful outcome, not an error.** `{"reply": ""}`,
`{}`, a `204`, or an empty body all mean "seen, don't answer" — recorded, not
retried. Most messages in a live chat do not want an answer.

## Being strict about retrying

Retries cover transport failures, 5xx and 429, with exponential backoff. A 4xx
is the endpoint saying the request itself is wrong; repeating it verbatim would
be noise, so it fails immediately.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from wadam import constants
from wadam.domain.models import ChatConfig, StoredMessage

logger = logging.getLogger(__name__)

#: Read the reply from the first of these present, at the top level or inside
#: `data`. Mirrors the leniency of the send API's own parser.
_REPLY_KEYS = ("reply", "message", "text", "body", "answer")

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class WebhookOutcome:
    """What one call to an endpoint produced."""

    ok: bool
    status_text: str
    reply_text: str = ""
    body: str = ""
    error: str = ""
    attempts: int = 1
    duration_ms: float = 0.0

    @property
    def wants_reply(self) -> bool:
        return bool(self.reply_text.strip())


def build_payload(chat: ChatConfig, message: StoredMessage) -> dict:
    """The JSON POSTed to the endpoint.

    winSpark's envelope, with the fields the new transport can actually fill.
    `time_text` is gone — it was the message bubble's own clock label, scraped
    off the screen, and OpenWA sends no such string. `chat.phone` is new and
    worth having: an endpoint keyed on phone number no longer has to resolve
    one itself.
    """
    return {
        "event": "message.received",
        "app": {"name": constants.APP_NAME, "version": constants.APP_VERSION},
        "chat": {
            "id": chat.chat_id,
            "name": chat.contact_name or chat.chat_name,
            "phone": chat.phone_number,
            "is_group": chat.is_group,
        },
        "message": {
            "key": message.message_key,
            "sender": message.sender,
            "text": message.text,
            "direction": message.direction,
            "media_kind": message.media_kind,
            "detected_at": message.detected_at.isoformat() if message.detected_at else "",
        },
    }


def parse_reply(body: str) -> str:
    """The reply text in a response body, or "" for "nothing to send"."""
    text = (body or "").strip()
    if not text:
        return ""

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Not JSON at all. A bare line of text is a legitimate answer, and an
        # endpoint that returns one should not have to learn JSON to be used.
        return text

    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, (int, float)):
        return str(parsed)
    if not isinstance(parsed, dict):
        return ""

    for key in _REPLY_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested = parsed.get("data")
    if isinstance(nested, dict):
        for key in _REPLY_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class WebhookClient:
    """POSTs a message to an endpoint and returns what it said."""

    def __init__(self, timeout: float = 20.0, max_retries: int = 3,
                 api_key: str = "", backoff: float = 1.0) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._api_key = api_key
        self._backoff = backoff

    def call(self, url: str, payload: dict, sleep=time.sleep) -> WebhookOutcome:
        """Call `url` with `payload`. Never raises."""
        if not url:
            return WebhookOutcome(False, "no url", error="no webhook configured for this chat")

        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "User-Agent": f"{constants.APP_NAME}/{constants.APP_VERSION}"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        started = time.monotonic()
        last: Optional[WebhookOutcome] = None

        for attempt in range(1, self._max_retries + 2):
            outcome = self._attempt(url, body, headers, attempt)
            outcome.duration_ms = (time.monotonic() - started) * 1000
            if outcome.ok or not self._retryable(outcome):
                return outcome
            last = outcome
            if attempt <= self._max_retries:
                sleep(self._backoff * (2 ** (attempt - 1)))

        return last or WebhookOutcome(False, "failed", error="no attempt was made")

    def _attempt(self, url: str, body: bytes, headers: dict, attempt: int) -> WebhookOutcome:
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                return WebhookOutcome(
                    ok=True,
                    status_text=f"{response.status} {response.reason}".strip(),
                    reply_text=parse_reply(raw),
                    body=raw[:1000],
                    attempts=attempt,
                )
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:1000]
            return WebhookOutcome(
                ok=False,
                status_text=str(error.code),
                body=detail,
                error=f"HTTP {error.code}",
                attempts=attempt,
            )
        except urllib.error.URLError as error:
            return WebhookOutcome(False, "transport", error=str(error.reason), attempts=attempt)
        except TimeoutError:
            return WebhookOutcome(False, "timeout",
                                  error=f"no response within {self._timeout:g}s",
                                  attempts=attempt)
        except Exception as error:  # noqa: BLE001 - a webhook must not take the service down
            return WebhookOutcome(False, "error", error=str(error), attempts=attempt)

    @staticmethod
    def _retryable(outcome: WebhookOutcome) -> bool:
        """Transport failures, 5xx and 429 are worth repeating. A 4xx is the
        endpoint saying the request itself is wrong, and sending it again
        unchanged would only be noise."""
        if outcome.status_text in ("transport", "timeout", "error"):
            return True
        try:
            return int(outcome.status_text) in _RETRYABLE_STATUS
        except ValueError:
            return False
