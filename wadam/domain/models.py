"""The persisted shapes. One dataclass per MongoDB collection.

Every model round-trips through plain dicts (`to_document` / `from_document`)
because the same shape is written twice — once to MongoDB as the primary, once
to the JSON mirror — and a single serializer for both is what keeps the two
from drifting apart.

Datetimes are always timezone-aware UTC in memory. MongoDB stores them as BSON
dates (which are UTC but come back naive), so `from_document` re-attaches the
timezone; JSON stores them as ISO-8601 strings.

**What moving to OpenWA deleted from this file.** Two hashes and a queue:

* `chat_id_for(chat_name)` derived a chat's id from its display name, because
  WhatsApp Desktop's accessibility tree exposed no durable identifier — a
  sidebar row was a flattened string of name + preview + timestamp, all of
  which change. The honest consequence was that renaming a contact produced a
  *new* chat with its own configuration. OpenWA supplies a real id
  (`216298915164281@lid`), so the hash and its consequence are both gone.
* `message_key_for(...)` hashed a message's content, because re-reading the
  same visible bubble every three seconds would otherwise store it repeatedly.
  It could not tell two people genuinely sending "ok" a minute apart from one
  message read twice. WhatsApp's own message id replaces it.
* `OutgoingMessage` / `OutgoingStatus` was a durable send queue, needed because
  a UI-Automation send took seconds, could not run concurrently, and might
  leave the compose box without ever reaching the conversation. A send is now
  an HTTP call whose response says whether it worked.

`PollState` went with them: there is no polling loop. The per-chat webhook did
NOT go — it is what this application is for — but its call history now lives on
the chat itself rather than in a separate `webhooks` collection, because the
only question ever asked of it was "what did this endpoint last say".
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    """Normalize whatever came back from Mongo or JSON into an aware UTC
    datetime (or None). Mongo hands back naive UTC; JSON hands back a string."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def phone_digits(value: str) -> str:
    """The digits of `value` when it looks like a phone number, else "".

    "Looks like" means at least seven digits and almost nothing else — a couple
    of stray non-phone characters are tolerated, but a name is not. This is
    what stops "CSE - C 2023-27" from being read as a phone number.
    """
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 7:
        return ""
    non_phone = re.sub(r"[\d+\-\s().]", "", value or "")
    if len(non_phone) > 2:
        return ""
    return digits


def phone_from_chat_id(chat_id: str) -> str:
    """The phone number in an OpenWA chat id, when there genuinely is one.

    `918985370703@c.us` yields the number. `216298915164281@lid` yields ""
    — a LID is an opaque identifier, and reading it as a phone number would
    display a plausible-looking number belonging to nobody.
    """
    if not chat_id or "@" not in chat_id:
        return ""
    local, _, domain = chat_id.partition("@")
    return phone_digits(local) if domain == "c.us" else ""


class MessageStatus:
    """The life of one message.

    Five states, down from eleven. The ones that went were all about a send
    that might silently not have happened: DISPATCHING, AWAITING_SEND and
    INTERRUPTED existed so a crash mid-send could be reconstructed, and SEEDED
    marked messages already on screen when a chat was first enabled.
    """

    PENDING = "pending"        # stored; nothing decided yet
    COLLECTED = "collected"    # stored; no reply wanted, and that is fine
    REPLIED = "replied"        # answered
    FAILED = "failed"          # the send did not happen
    SENT = "sent"              # an outgoing message this application originated


@dataclass
class ChatConfig:
    """One WhatsApp chat and its automation setting.

    Registered the first time a message arrives from it, with automation OFF.
    winSpark defaulted it ON because sidebar discovery found every chat whether
    it had ever spoken or not; here a chat only exists once someone has
    messaged it, and answering a stranger automatically on first contact is
    precisely the behaviour that gets a number restricted.
    """

    chat_id: str = ""
    """OpenWA's identifier, verbatim. Durable across renames."""

    chat_name: str = ""
    """What OpenWA's chat list calls this chat. For an unsaved number that is
    the number itself; for a saved contact it is whatever WhatsApp displays,
    which may be stylised unicode rather than anything you would type."""

    contact_name: str = ""
    """The address-book name, joined from `contacts` through the phone number.
    Preferred for display and for addressing a chat by name, because it is what
    a person would actually type."""

    phone_number: str = ""
    """Resolved once via OpenWA and cached. A chat id is a `@lid` and carries
    no number, so this is the only join to the address book — and the only way
    `{"id": "919100251854"}` can find this chat."""

    webhook_url: str = ""
    """Where this chat's incoming messages are POSTed. Empty means the global
    default; empty with no default means nothing is dispatched."""

    automation_enabled: bool = False
    is_group: bool = False

    # --- what the chat list renders ---------------------------------------
    last_message_preview: str = ""
    last_incoming_text: str = ""
    last_incoming_sender: str = ""
    last_incoming_utc: Optional[datetime] = None
    last_outgoing_text: str = ""
    last_outgoing_utc: Optional[datetime] = None
    messages_stored: int = 0
    last_error: str = ""

    # --- what the endpoint last said --------------------------------------
    #: Kept whether the call succeeded or not. "The endpoint answered 502 three
    #: times" is exactly what someone debugging a silent chat needs, and it is
    #: invisible if only successes are recorded.
    last_webhook_status: str = ""
    last_webhook_response: str = ""
    last_webhook_utc: Optional[datetime] = None
    webhook_retry_count: int = 0

    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "ChatConfig":
        return _build(cls, document)


@dataclass
class Contact:
    """One entry from WhatsApp's address book.

    Stored so a name can be resolved to a chat even when no chat exists yet —
    an address book of ~900 people is not something to re-fetch per lookup, and
    OpenWA pages it 1000 rows at a time.
    """

    contact_id: str = ""
    """`<number>@c.us`. NOT a chat id: the same person's chat is a `@lid`."""

    name: str = ""
    push_name: str = ""
    phone_number: str = ""
    """Digits only. The join to a chat."""

    is_my_contact: bool = False
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def display_name(self) -> str:
        return self.name or self.push_name or self.phone_number or self.contact_id

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Contact":
        return _build(cls, document)


@dataclass
class StoredMessage:
    """One message, persisted the moment it is understood — before any reply is
    decided. Nothing in this application is allowed to exist only in memory."""

    message_key: str = ""
    """WhatsApp's own message id for inbound messages. Unique-indexed, so a
    retried webhook delivery cannot store the same message twice."""

    chat_id: str = ""
    chat_name: str = ""
    sender: str = ""
    text: str = ""
    direction: str = "in"      # "in" (received) | "out" (sent by us)
    media_kind: str = ""       # image | video | audio | document | sticker | ""
    origin: str = ""           # "reply" | "api" | ""
    detected_at: datetime = field(default_factory=utcnow)
    status: str = MessageStatus.PENDING
    reply_text: str = ""
    error: str = ""

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "StoredMessage":
        return _build(cls, document)


@dataclass
class AutomationLog:
    """One line of the activity log the UI renders."""

    correlation_id: str = ""
    level: str = "INFO"
    event: str = ""
    chat_id: str = ""
    chat_name: str = ""
    message: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "AutomationLog":
        return _build(cls, document)


@dataclass
class ApplicationState:
    """The one document describing the application itself."""

    global_automation_enabled: bool = False
    version: str = ""
    started_at: Optional[datetime] = None
    last_shutdown_at: Optional[datetime] = None
    run_count: int = 0
    updated_at: datetime = field(default_factory=utcnow)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "ApplicationState":
        return _build(cls, document)


def _build(cls, document: dict[str, Any]):
    """Construct a dataclass from a stored document, ignoring unknown keys
    (so a document written by an older version still loads) and repairing
    datetimes."""
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for name, spec in known.items():
        if name not in document:
            continue
        value = document[name]
        annotation = str(spec.type)
        if "datetime" in annotation:
            kwargs[name] = _as_utc(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)
