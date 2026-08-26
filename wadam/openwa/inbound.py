"""Receiving a message — OpenWA delivers, this parses and proves it.

This replaces `wadam/whatsapp/reader.py` (1,084 lines) and `row_parser.py`,
which walked WhatsApp's accessibility tree every three seconds and pulled
messages out of a flattened string of name + preview + timestamp. There is no
polling here and no parsing heuristic: OpenWA POSTs a JSON body the moment a
message arrives.

Two things this module is strict about:

**The signature covers the raw bytes.** OpenWA signs exactly what it sent, so
verification must happen before `json.loads` — re-serializing a parsed object
reorders keys and changes whitespace, and the signature stops matching.

**The chat id is never rebuilt.** It is echoed back to OpenWA verbatim when
replying. See `client.send_text`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

SIGNATURE_HEADER = "X-OpenWA-Signature"
IDEMPOTENCY_HEADER = "X-OpenWA-Idempotency-Key"
EVENT_HEADER = "X-OpenWA-Event"

# Each field is read from the first key present rather than one exact path.
# OpenWA has moved field names between releases, and being strict about a shape
# you do not control turns somebody else's rename into your outage.
_TEXT_KEYS = ("body", "text", "message", "caption")
_CHAT_KEYS = ("chatId", "chat_id", "from", "chat")
_ID_KEYS = ("waMessageId", "messageId", "id", "message_id")
_TYPE_KEYS = ("type", "messageType")
_NAME_KEYS = ("chatName", "notifyName", "pushName", "senderName", "author")


def verify_signature(body: bytes, header_value: Optional[str], secret: str) -> bool:
    """Check `X-OpenWA-Signature` (`sha256=<hex>`) against the raw body.

    An empty configured secret disables the check. That is only safe on
    loopback, and `config.py` refuses to start bound anywhere else without one.
    """
    if not secret:
        return True
    if not header_value:
        return False

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.strip())


def _first(source: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


@dataclass(frozen=True)
class InboundMessage:
    """One inbound WhatsApp message, normalized."""

    chat_id: str
    """OpenWA's identifier for the chat. Durable, unlike winSpark's
    name-derived hash — renaming a contact no longer produces a new chat."""

    chat_name: str
    message_id: str
    """WhatsApp's own id. Replaces the content-hash `message_key`, which
    existed only because re-reading the same visible bubble every three
    seconds would otherwise have stored it repeatedly."""

    text: str
    sender: str
    media_kind: str
    is_group: bool
    is_outgoing: bool
    """True for this account's own traffic. Never answered."""

    raw: Mapping[str, Any]


def parse_delivery(payload: Mapping[str, Any]) -> Optional[InboundMessage]:
    """Return the message in a webhook body, or None if there is not one.

    None means "nothing to act on" — an event carrying no message, or one with
    no chat to answer. Callers treat it as a no-op success: a payload this
    cannot understand is not a payload retrying will fix.
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None

    inner = data.get("message")
    source: Mapping[str, Any] = inner if isinstance(inner, Mapping) else data

    chat_id = _first(source, _CHAT_KEYS) or _first(data, _CHAT_KEYS)
    if not chat_id:
        return None

    direction = str(source.get("direction") or data.get("direction") or "").lower()
    from_me = bool(source.get("fromMe", data.get("fromMe", False)))
    media_kind = _first(source, _TYPE_KEYS)

    return InboundMessage(
        chat_id=chat_id,
        chat_name=_first(source, _NAME_KEYS) or _first(data, _NAME_KEYS) or chat_id,
        message_id=_first(source, _ID_KEYS) or _first(data, _ID_KEYS),
        text=_first(source, _TEXT_KEYS),
        sender=_first(source, ("from", "author", "sender")),
        media_kind="" if media_kind in ("text", "chat") else media_kind,
        is_group=chat_id.endswith("@g.us"),
        is_outgoing=direction == "outgoing" or from_me,
        raw=source,
    )


def parse_body(body: bytes) -> Optional[Mapping[str, Any]]:
    """Decode a webhook body, or None if it is not a JSON object."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None
