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
_NAME_KEYS = ("chatName", "notifyName", "pushName", "senderName")


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


def _as_int(value: Any) -> int:
    """An int, or 0. A size arriving as the string "1024" is still a size."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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
    """The sender's push name, or "" when the delivery carried none.

    Deliberately NOT falling back to the chat id. It used to, and the caller
    could then not tell "this delivery named the chat" from "there was no name,
    here is the id again" — so a message with no push name overwrote a good
    name, synced from OpenWA's chat list, with a raw `…@lid`. The chat would be
    correct after a sync and wrong again after the next message.
    """

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

    # --- the media object, when the delivery carried one ------------------
    # Defaulted and last so every existing construction site still compiles:
    # a message with no media is the overwhelming majority, and making callers
    # spell out five empty fields to say "no picture" is how they get wrong.
    #
    # OpenWA puts the bytes IN the delivery as base64 (`media.data`), so the
    # common case needs no second request -- which also means no race against
    # the gateway's own retention, and nothing to get a 404 from.
    media_mimetype: str = ""
    media_filename: str = ""

    media_base64: str = ""
    """Base64 of the file. Empty when `media_omitted`, and when the engine sent
    metadata without a payload."""

    media_omitted: bool = False
    """OpenWA dropped the payload itself -- its own size cap, a timeout, or
    concurrency saturation. `media_size` is then the size it would have been.
    Recorded as itself rather than as a missing file: it is configuration on
    someone else's side, not a failure here, and the two want different fixes."""

    media_size: int = 0

    @property
    def has_media(self) -> bool:
        """Did this carry media, whether or not the bytes came with it?"""
        return bool(self.media_kind or self.media_mimetype or self.media_base64)


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

    # OpenWA carries the sender's push name nested under `contact`, not at the
    # top level (`message-mapper.ts` sets `incoming.contact = { pushName }`).
    # Reading only the top level left every chat named after its raw
    # identifier — `111111111111111@lid` in the list instead of a person.
    contact = source.get("contact") if isinstance(source.get("contact"), Mapping) else {}
    chat_name = (_first(source, _NAME_KEYS) or _first(data, _NAME_KEYS)
                 or _first(contact, _NAME_KEYS) or "")

    # `media` sits beside the message fields, not inside them. Read leniently
    # like everything else here: a rename upstream should cost a missing
    # attachment, not a delivery this cannot parse at all.
    media = source.get("media") if isinstance(source.get("media"), Mapping) else {}
    if not media and isinstance(data.get("media"), Mapping):
        media = data["media"]

    return InboundMessage(
        chat_id=chat_id,
        chat_name=chat_name,
        message_id=_first(source, _ID_KEYS) or _first(data, _ID_KEYS),
        text=_first(source, _TEXT_KEYS),
        sender=_first(source, ("from", "author", "sender")),
        media_kind="" if media_kind in ("text", "chat") else media_kind,
        is_group=chat_id.endswith("@g.us"),
        is_outgoing=direction == "outgoing" or from_me,
        raw=source,
        media_mimetype=_first(media, ("mimetype", "mimeType", "contentType")),
        media_filename=_first(media, ("filename", "fileName", "name")),
        media_base64=_first(media, ("data", "base64", "body")),
        media_omitted=bool(media.get("omitted")),
        media_size=_as_int(media.get("sizeBytes") or media.get("size")),
    )


def parse_body(body: bytes) -> Optional[Mapping[str, Any]]:
    """Decode a webhook body, or None if it is not a JSON object."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None
