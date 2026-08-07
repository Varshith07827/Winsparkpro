"""The webhook call — the application's one and only decision-maker.

There is no AI in this program. When an incoming message arrives, its content
is POSTed to the chat's configured webhook and whatever comes back is what gets
sent. What sits behind that URL — a rules engine, a language model, a person
with a keyboard — is entirely the operator's business and completely invisible
here.

Request (`POST`, `Content-Type: application/json`)::

    {
      "event": "message.received",
      "app": {"name": "...", "version": "..."},
      "chat": {"id": "...", "name": "...", "is_group": false},
      "message": {
        "key": "<dedup hash>", "sender": "Alice", "text": "hello",
        "direction": "in", "media_kind": "", "media_note": "",
        "time_text": "9:21 pm", "detected_at": "2026-08-06T09:21:44+00:00"
      }
    }

Response — any of these is understood, so the endpoint can be almost anything:

* ``{"reply": "..."}`` (also ``message`` / ``text`` / ``response`` / ``answer``)
* the same keys nested under ``data`` or ``result``
* a bare JSON string, or a plain-text body
* ``{"reply": ""}``, ``{}``, ``204``, or an empty body → **no reply is sent**

An empty reply is a first-class, successful outcome: it's how an endpoint says
"I saw it, don't answer". It is recorded, not retried.

Retries use exponential backoff and cover transport errors, 5xx, and 429 —
the failures where trying again is meaningful. A 4xx (other than 429) is the
endpoint saying the request itself is wrong; repeating it verbatim would just
be noise, so it fails immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from wadam import constants

logger = logging.getLogger(__name__)

_REPLY_KEYS = ("reply", "message", "text", "response", "answer")
_ENVELOPE_KEYS = ("data", "result", "payload")

# How much of a response body is kept for the record / the UI. Bodies can be
# arbitrarily large; the status panel needs a readable excerpt, not a dump.
_BODY_EXCERPT = 2000


@dataclass(frozen=True)
class WebhookOutcome:
    ok: bool
    status_code: int = 0
    reply_text: str = ""
    body: str = ""
    error: str = ""
    attempts: int = 0
    duration_ms: int = 0

    @property
    def status_text(self) -> str:
        if self.ok and self.reply_text:
            return f"{self.status_code} OK · reply"
        if self.ok:
            return f"{self.status_code} OK · no reply"
        if self.status_code:
            return f"{self.status_code} failed"
        return self.error[:60] or "failed"


def build_payload(chat, message) -> dict[str, Any]:
    return {
        "event": "message.received",
        "app": {"name": constants.APP_NAME, "version": constants.APP_VERSION},
        "chat": {
            "id": chat.chat_id,
            "name": chat.chat_name,
            "is_group": chat.is_group,
        },
        "message": {
            "key": message.message_key,
            "sender": message.sender,
            "text": message.text,
            "direction": message.direction,
            "media_kind": message.media_kind,
            "media_note": message.media_note,
            "time_text": message.time_text,
            "detected_at": message.detected_at.isoformat() if message.detected_at else None,
        },
    }


def extract_reply(body: str) -> str:
    """Pull the reply text out of a response body, understanding the shapes
    listed in the module docstring. Returns "" when the endpoint said nothing —
    which is a valid answer, not a parse failure."""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        # Not JSON. A plain-text body IS the reply — that's the simplest
        # possible endpoint and it should just work.
        return text
    return _reply_from_json(parsed)


def _reply_from_json(parsed: Any, depth: int = 0) -> str:
    if depth > 3:
        return ""
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, list):
        # A list of messages: join the readable ones, so an endpoint that
        # answers with several lines doesn't silently lose all but one.
        parts = [_reply_from_json(item, depth + 1) for item in parsed]
        return "\n".join(p for p in parts if p).strip()
    if not isinstance(parsed, dict):
        return ""
    for key in _REPLY_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (dict, list)):
            nested = _reply_from_json(value, depth + 1)
            if nested:
                return nested
    for key in _ENVELOPE_KEYS:
        if key in parsed:
            nested = _reply_from_json(parsed[key], depth + 1)
            if nested:
                return nested
    return ""


@dataclass(frozen=True)
class RelayMessage:
    """One message the relay found waiting at a chat's webhook URL.

    `external_id` is whatever identifier the endpoint gave it. When present it
    is authoritative for deduplication — it is the endpoint saying "this is a
    distinct message", which is the only way to send the same text twice on
    purpose."""

    text: str
    external_id: str = ""


# Field names a relay response may carry its text under, and its id under.
# Broader than the reply parser's: this is winSpark's set, kept so an endpoint
# written for that still works here unchanged.
_RELAY_TEXT_KEYS = ("message", "text", "content", "body", "msg", "reply")
# `_id` first, deliberately. A queue that hands back its own document carries
# the message's identity in `_id`, while `id` is often the DESTINATION — a chat
# or contact identifier that is the same for every message and would suppress
# all but the first.
_RELAY_ID_KEYS = ("_id", "message_id", "messageId", "external_id", "externalId",
                  "uid", "id")


def parse_relay_messages(body: str) -> list[RelayMessage]:
    """Everything a poll response is offering to send.

    Understands plain text, a single object, an array of objects, and either
    nested under `data`/`result`/`messages`. An array yields **every** message
    in it, not just the first — an endpoint answering a burst with three
    objects means three messages, and quietly delivering one of them is how a
    backlog disappears."""
    text = (body or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        # A plain-text body IS the message. The simplest possible endpoint is
        # `echo "Hello"` behind a CGI script, and it should just work.
        return [RelayMessage(text=text)]
    return _relay_from_json(parsed)


def _relay_from_json(parsed: Any, depth: int = 0) -> list[RelayMessage]:
    if depth > 3:
        return []
    if isinstance(parsed, str):
        return [RelayMessage(text=parsed.strip())] if parsed.strip() else []
    if isinstance(parsed, list):
        found: list[RelayMessage] = []
        for item in parsed:
            found.extend(_relay_from_json(item, depth + 1))
        return found
    if not isinstance(parsed, dict):
        return []

    external_id = ""
    for key in _RELAY_ID_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            external_id = value.strip()
            break
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            external_id = str(value)
            break

    for key in _RELAY_TEXT_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return [RelayMessage(text=value.strip(), external_id=external_id)]
        if isinstance(value, (dict, list)):
            nested = _relay_from_json(value, depth + 1)
            if nested:
                return nested

    for key in ("data", "result", "payload", "messages"):
        if key in parsed:
            nested = _relay_from_json(parsed[key], depth + 1)
            if nested:
                # An id on the envelope covers a single nested message that
                # didn't carry one of its own.
                if external_id and len(nested) == 1 and not nested[0].external_id:
                    return [RelayMessage(text=nested[0].text, external_id=external_id)]
                return nested
    return []


def _is_retryable(status: int) -> bool:
    return status == 0 or status == 429 or 500 <= status < 600


class WebhookClient:
    """stdlib `urllib` wrapped in `asyncio.to_thread` — no extra HTTP
    dependency for what is one POST with a timeout."""

    def __init__(self, api_key: str = "", timeout: float = 20.0, max_retries: int = 3) -> None:
        self._api_key = (api_key or "").strip()
        self._timeout = timeout
        self._max_retries = max(0, max_retries)

    async def call(self, url: str, payload: dict[str, Any]) -> WebhookOutcome:
        url = (url or "").strip()
        if not url:
            return WebhookOutcome(ok=False, error="No webhook URL is configured for this chat.")
        if not url.startswith(("http://", "https://")):
            return WebhookOutcome(ok=False, error=f"Webhook URL must be http:// or https:// (got {url!r}).")

        started = time.monotonic()
        last: WebhookOutcome = WebhookOutcome(ok=False, error="not attempted")
        for attempt in range(1, self._max_retries + 2):
            status, body, error = await asyncio.to_thread(self._post, url, payload)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if 200 <= status < 300:
                return WebhookOutcome(
                    ok=True, status_code=status, reply_text=extract_reply(body),
                    body=body[:_BODY_EXCERPT], attempts=attempt, duration_ms=elapsed_ms,
                )
            last = WebhookOutcome(
                ok=False, status_code=status, body=body[:_BODY_EXCERPT],
                error=error or f"HTTP {status}", attempts=attempt, duration_ms=elapsed_ms,
            )
            if not _is_retryable(status) or attempt > self._max_retries:
                return last
            # 1s, 2s, 4s … capped. Long enough for a restarting endpoint to come
            # back, short enough that a reply still feels like a reply.
            backoff = min(2 ** (attempt - 1), 8)
            logger.warning("webhook %s failed (%s) — retrying in %ss", url, last.error, backoff)
            await asyncio.sleep(backoff)
        return last

    async def fetch(self, url: str) -> WebhookOutcome:
        """GET a chat's webhook URL to see whether it has anything to send.

        The relay half of the contract, and deliberately **not** retried: this
        runs every few seconds anyway, so a failed poll simply becomes the next
        poll. Retrying inside a call that is about to repeat only multiplies
        load on an endpoint that is already struggling."""
        url = (url or "").strip()
        if not url:
            return WebhookOutcome(ok=False, error="No webhook URL is configured for this chat.")
        if not url.startswith(("http://", "https://")):
            return WebhookOutcome(ok=False, error=f"Webhook URL must be http:// or https:// (got {url!r}).")

        started = time.monotonic()
        status, body, error = await asyncio.to_thread(self._get, url)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if 200 <= status < 300:
            return WebhookOutcome(ok=True, status_code=status, body=body[:_BODY_EXCERPT],
                                  attempts=1, duration_ms=elapsed_ms)
        return WebhookOutcome(ok=False, status_code=status, body=body[:_BODY_EXCERPT],
                              error=error or f"HTTP {status}", attempts=1, duration_ms=elapsed_ms)

    def _get(self, url: str) -> tuple[int, str, str]:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/json, text/plain")
        request.add_header("User-Agent", f"{constants.APP_SHORT_NAME}/{constants.APP_VERSION}")
        if self._api_key:
            request.add_header("Authorization", f"Bearer {self._api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read().decode("utf-8", errors="replace"), ""
        except urllib.error.HTTPError as ex:
            body = ex.read().decode("utf-8", errors="replace") if ex.fp else ""
            return ex.code, body, f"HTTP {ex.code}"
        except urllib.error.URLError as ex:
            return 0, "", f"{type(ex.reason).__name__ if ex.reason else 'URLError'}: {ex.reason}"
        except Exception as ex:  # noqa: BLE001
            return 0, "", f"{type(ex).__name__}: {ex}"

    def _post(self, url: str, payload: dict[str, Any]) -> tuple[int, str, str]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("Accept", "application/json, text/plain")
        request.add_header("User-Agent", f"{constants.APP_SHORT_NAME}/{constants.APP_VERSION}")
        if self._api_key:
            request.add_header("Authorization", f"Bearer {self._api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, body, ""
        except urllib.error.HTTPError as ex:
            body = ex.read().decode("utf-8", errors="replace") if ex.fp else ""
            return ex.code, body, f"HTTP {ex.code}"
        except urllib.error.URLError as ex:
            return 0, "", f"{type(ex.reason).__name__ if ex.reason else 'URLError'}: {ex.reason}"
        except Exception as ex:  # noqa: BLE001 - a timeout, a DNS failure, a reset socket
            return 0, "", f"{type(ex).__name__}: {ex}"

    async def probe(self, url: str) -> WebhookOutcome:
        """A no-op call used by the "Test" button. Same transport, a payload the
        endpoint can recognise and ignore."""
        return await self.call(url, {
            "event": "webhook.test",
            "app": {"name": constants.APP_NAME, "version": constants.APP_VERSION},
        })


def optional_reply(outcome: WebhookOutcome) -> Optional[str]:
    """The reply to send, or None when the endpoint chose not to answer."""
    text = (outcome.reply_text or "").strip()
    return text or None
