"""The persisted shapes. One dataclass per MongoDB collection.

Every model round-trips through plain dicts (`to_document` / `from_document`)
because the same shape is written twice — once to MongoDB as the primary, once
to the JSON mirror — and a single serializer for both is what keeps the two
from drifting apart.

Datetimes are always timezone-aware UTC in memory. MongoDB stores them as BSON
dates (which are UTC but come back naive), so `from_document` re-attaches the
timezone; JSON stores them as ISO-8601 strings.
"""

from __future__ import annotations

import hashlib
import re
import uuid
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


_ID_CLEAN_RE = re.compile(r"\s+")


def chat_id_for(chat_name: str) -> str:
    """A stable id for a chat, derived from its display name.

    WhatsApp Desktop's accessibility tree exposes no durable identifier for a
    chat — a row is a flattened string of name + preview + timestamp, all of
    which change. The name is the only stable part, so the id is a hash of it.

    The consequence, stated plainly: renaming a contact or group produces a NEW
    chat here, with its own configuration. That's the honest trade for having
    no real id, and it fails safe — a renamed chat starts with automation OFF
    rather than inheriting a webhook meant for a different conversation.
    """
    normalized = _ID_CLEAN_RE.sub(" ", (chat_name or "").strip()).casefold()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:24]


def phone_digits(value: str) -> str:
    """The digits of `value` when it looks like a phone number, else "".

    "Looks like" means at least seven digits and almost nothing else — a couple
    of stray non-phone characters are tolerated (a trailing tilde, a stray
    letter), but a name is not. This is what stops "CSE - C 2023-27" from being
    read as a phone number."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 7:
        return ""
    non_phone = re.sub(r"[\d+\-\s().]", "", value or "")
    if len(non_phone) > 2:
        return ""
    return digits


def contact_id_for(value: str) -> str:
    """The last four digits of a chat's phone number — the identifier the send
    API addresses chats by.

    Returns "" when the chat name is not a phone number, which is the common
    case for a **saved** contact: WhatsApp's sidebar shows such a chat by the
    contact's name and never exposes the number, so there is nothing to derive
    from. Those chats get their contact ID typed in once, in the configuration
    panel. See `ChatConfig.external_id`.

    Four digits is only 10,000 values, so collisions across a large chat list
    are not hypothetical. The resolver refuses an ambiguous ID rather than
    guessing — sending a message to the wrong person is the one failure this
    application must never produce quietly."""
    digits = phone_digits(value)
    return digits[-4:] if digits else ""


def message_key_for(chat_id: str, sender: str, text: str, time_text: str, direction: str) -> str:
    """The deduplication key for a message bubble.

    Every poll re-reads the same visible tail of the conversation, so identity
    has to come from content: which chat, who sent it, what it said, the
    bubble's own clock label, and which way it went. Two genuinely identical
    messages sent in the same minute collapse into one — accepted deliberately,
    because the alternative (treating a re-read of the same bubble as new)
    would webhook and reply to the same message on every three-second cycle.
    """
    raw = "".join((chat_id, sender or "", text or "", time_text or "", direction))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def outgoing_key_for(chat_id: str, text: str, moment: Optional[datetime] = None) -> str:
    """The key for a message this application SENDS, as opposed to one it read.

    Deliberately not `message_key_for`. That key is content-derived because a
    bubble read from WhatsApp has no identity of its own, which means two
    identical messages collapse into one — correct when re-reading the same
    visible tail every three seconds, and wrong here. When we send something we
    know for certain it is a distinct event, so the send moment goes into the
    key and a legitimate repeat gets its own record.

    Found by a live relay run: three messages went out and only two were
    stored, because the third repeated the first."""
    stamp = (moment or utcnow()).isoformat()
    raw = "".join((chat_id, "You", text or "", stamp, "out"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class ChatConfig:
    """One WhatsApp chat and its automation configuration + live status.

    Created automatically the first time a chat is seen in the sidebar, with
    automation OFF and no webhook — never through a dialog.
    """

    chat_id: str = ""
    chat_name: str = ""
    #: The contact's number, digits only, resolved at discovery where WhatsApp
    #: exposes it. First-class because the webhook URL is built from it. Empty
    #: means "not resolved" and is never guessed at — a wrong number would send
    #: someone else's conversation to a webhook.
    phone_number: str = ""

    # --- configuration -----------------------------------------------------
    #: The URL actually called for this chat. **Derived**, not typed in:
    #: regenerated from the global template and `phone_number` on every
    #: discovery pass, so changing the template updates every chat. Stored
    #: rather than computed at each use so the existing webhook, relay and
    #: recovery paths did not all need rewriting around a new signature.
    webhook_url: str = ""
    #: Set only when a chat deliberately points somewhere else. Wins over the
    #: template. The simplified UI does not offer this; the data model keeps it
    #: so an operator editing the database is not fighting the application.
    webhook_override: str = ""
    automation_enabled: bool = False
    # The identifier the inbound send API addresses this chat by — by default
    # the last four digits of the contact's number, auto-filled at discovery
    # when the chat name is itself a number. For a saved contact the sidebar
    # only ever shows the name, so there is nothing to derive and this is typed
    # in once from the configuration panel.
    external_id: str = ""

    # --- sidebar mirror (what the chat list renders) -----------------------
    last_message_preview: str = ""
    timestamp_text: str = ""
    unread_count: int = 0
    is_pinned: bool = False
    is_muted: bool = False
    is_group: bool = False

    #: Messages received for this chat that have NOT yet finished the
    #: automation round trip. Recomputed on every snapshot, never read back
    #: from storage — a stale count is worse than no count.
    pending_count: int = 0

    # --- live status -------------------------------------------------------
    last_poll_utc: Optional[datetime] = None
    last_incoming_text: str = ""
    last_incoming_sender: str = ""
    last_incoming_utc: Optional[datetime] = None
    last_outgoing_text: str = ""
    last_outgoing_utc: Optional[datetime] = None
    last_webhook_status: str = ""       # e.g. "200 OK", "timeout", "no reply"
    last_webhook_response: str = ""     # trimmed body / reply text
    last_webhook_utc: Optional[datetime] = None
    webhook_retry_count: int = 0        # attempts spent on the most recent call
    # The relay half: what the last GET of this chat's webhook produced, and
    # the text it last sent — which doubles as the consecutive-duplicate guard.
    last_relay_status: str = ""
    last_relay_text: str = ""
    last_relay_utc: Optional[datetime] = None
    # Set the first time this chat's endpoint answers a poll with "nothing
    # waiting". That proves it dequeues, which retires the content-based
    # duplicate guard for good — see RelayService.should_send.
    relay_dequeues: bool = False
    messages_stored: int = 0
    last_error: str = ""

    # --- bookkeeping -------------------------------------------------------
    # False until the chat's existing backlog has been read once and recorded
    # WITHOUT triggering automation. Without this, enabling a webhook on a busy
    # chat would fire it at every message already on screen.
    seeded: bool = False
    # Hash of the last sidebar row text seen, so the poll can tell "this chat
    # changed" from "this chat is the same as three seconds ago" without
    # opening it.
    row_signature: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "ChatConfig":
        return _build(cls, document)


class MessageStatus:
    """The lifecycle of a message, persisted at every transition.

    The states exist so that a crash at any point is *recoverable without
    guessing*. Each one answers "what has already happened to the outside
    world?", which is the only question that matters when deciding whether it
    is safe to resume:

    ============== ========================================= =================
    state          the outside world has…                    on restart
    ============== ========================================= =================
    SEEDED         nothing — pre-existing backlog            leave alone
    PENDING        nothing — stored, webhook not yet called  safe to process
    DISPATCHING    unknown — the webhook call was in flight  do NOT retry
    WEBHOOK_OK     seen the message, chose not to reply      done
    WEBHOOK_FAILED seen it, or not; it gave up either way    done
    AWAITING_SEND  replied; our send is unconfirmed          verify, then send
    REPLIED        received our reply, verified              done
    REPLY_FAILED   replied; our send provably did not land   done
    IGNORED        nothing — automation off, or no webhook   leave alone
    INTERRUPTED    unknown — was DISPATCHING when we died    left for a human
    ============== ========================================= =================

    DISPATCHING is the important one. It is written *before* the webhook call,
    so a message found in that state after a crash might have already reached
    the endpoint and might have already caused a side effect there. Retrying it
    would risk a duplicate webhook call, which the reliability requirements
    forbid outright — so it is marked INTERRUPTED, logged loudly, and left for
    a person to decide about. Losing an automatic reply is recoverable; sending
    someone's customer two of them is not.
    """

    SEEDED = "seeded"
    PENDING = "pending"
    DISPATCHING = "dispatching"
    WEBHOOK_OK = "webhook_ok"
    WEBHOOK_FAILED = "webhook_failed"
    AWAITING_SEND = "awaiting_send"
    REPLIED = "replied"
    REPLY_FAILED = "reply_failed"
    IGNORED = "ignored"
    INTERRUPTED = "interrupted"
    SENT = "sent"  # an outgoing message we originated

    #: States that mean work was in progress when the process stopped.
    INCOMPLETE = (PENDING, DISPATCHING, AWAITING_SEND)


@dataclass
class StoredMessage:
    """One message bubble, persisted the moment it is detected — before the
    webhook is called, before anything is sent. Nothing in this application is
    allowed to exist only in memory."""

    message_key: str = ""
    chat_id: str = ""
    chat_name: str = ""
    #: The chat's number as known when this message was stored. Denormalised on
    #: purpose: the specification asks messages to preserve it, and a consumer
    #: reading the messages collection should not have to join to find out whose
    #: number a message belongs to. Backfilled when a number is set later.
    phone_number: str = ""
    sender: str = ""
    text: str = ""
    direction: str = "in"          # "in" (received) | "out" (sent by us)
    media_kind: str = ""           # photo | voice | video | document | sticker | gif | ""
    media_note: str = ""           # duration / filename / caption
    time_text: str = ""            # the bubble's own clock label, e.g. "9:21 pm"
    # Where an outgoing message came from: "webhook_reply" (the pipeline
    # answered an incoming message), "api" (something POSTed it to the send
    # API), or "" for anything simply read back out of WhatsApp.
    origin: str = ""
    # The id the endpoint gave a relayed message, when it gave one. Empty for
    # everything else. This is what makes "send the same text twice" possible.
    external_ref: str = ""
    detected_at: datetime = field(default_factory=utcnow)
    status: str = MessageStatus.PENDING  # see MessageStatus for the lifecycle
    webhook_id: str = ""
    reply_text: str = ""
    error: str = ""

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "StoredMessage":
        return _build(cls, document)


class OutgoingStatus:
    """The life of a queued outgoing message.

    Separate from `MessageStatus` on purpose. That one describes an *incoming*
    message's journey through the webhook; this describes an *outgoing* one's
    journey to the screen, and the two failure vocabularies are different —
    "the endpoint 5xx'd" and "the bubble never appeared" want different
    responses from an operator."""

    QUEUED = "queued"           # persisted, nothing attempted
    SENDING = "sending"         # a worker has it; the outcome is unknown
    VERIFYING = "verifying"     # left the compose box, delivery unconfirmed
    DELIVERED = "delivered"     # a new outgoing bubble was found in the chat
    UNVERIFIED = "unverified"   # transport succeeded, delivery unproven
    FAILED = "failed"           # gave up after the retry policy
    CANCELLED = "cancelled"     # its chat was deleted underneath it

    #: States a restart must pick back up.
    RESUMABLE = (QUEUED,)
    #: In-flight when the process died — ambiguous, needs verifying not resending.
    AMBIGUOUS = (SENDING, VERIFYING)
    FINAL = (DELIVERED, UNVERIFIED, FAILED, CANCELLED)


@dataclass
class OutgoingMessage:
    """One message waiting to reach a chat.

    Persisted before anything is attempted, so the queue survives a crash. The
    per-chat `sequence` preserves ordering: two replies to the same
    conversation must arrive in the order they were produced, whatever the
    worker does in between."""

    outgoing_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    chat_id: str = ""
    chat_name: str = ""
    text: str = ""
    origin: str = ""                 # "webhook_reply" | "api" | "relay"
    status: str = OutgoingStatus.QUEUED
    sequence: int = 0                # per chat, ascending
    attempts: int = 0
    max_attempts: int = 3
    error: str = ""
    verification: str = ""           # Verification.* once attempted
    external_ref: str = ""           # a relay message id, when there was one
    source_message_key: str = ""     # the incoming message this answers
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    delivered_at: Optional[datetime] = None

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "OutgoingMessage":
        return _build(cls, document)


@dataclass
class WebhookRecord:
    """One webhook invocation: what was sent, what came back, how many attempts
    it took."""

    webhook_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    chat_id: str = ""
    chat_name: str = ""
    message_key: str = ""
    url: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    status_code: int = 0
    ok: bool = False
    attempts: int = 0
    response_body: str = ""
    reply_text: str = ""
    error: str = ""
    duration_ms: int = 0
    created_at: datetime = field(default_factory=utcnow)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "WebhookRecord":
        return _build(cls, document)


@dataclass
class AutomationLog:
    """A single line of the activity log — the thing you read when a message
    didn't get answered and you need to know which step gave up.

    The structured fields below are all optional and default to empty, because
    most lines (a chat discovered, a poll failure) have no direction or webhook
    to speak of. When a line *is* about a message, it carries the whole picture:
    which chat, which way the message was going, which endpoint, what came back,
    how many retries it cost, and what went wrong — so a question like "why did
    this chat stop replying at 14:36?" is answerable from the log alone."""

    log_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    level: str = "INFO"
    event: str = ""       # short machine-ish tag: "chat.discovered", "webhook.failed"
    chat_id: str = ""
    chat_name: str = ""
    message: str = ""
    direction: str = ""       # "in" | "out" | ""
    # Ties every line about one message together: the incoming message_key, or
    # the outgoing_id once it is queued. Without it a log can say a chat had
    # trouble but not WHICH message — and "which one" is the first question
    # anybody asks when a reply goes missing.
    correlation_id: str = ""
    webhook_url: str = ""
    response: str = ""
    retry_count: int = 0
    error: str = ""
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


@dataclass
class PollState:
    """The one document describing the polling loop's health."""

    cycle_count: int = 0
    last_cycle_utc: Optional[datetime] = None
    last_cycle_ms: int = 0
    whatsapp_found: bool = False
    chats_seen: int = 0
    queued_chats: int = 0
    last_error: str = ""
    updated_at: datetime = field(default_factory=utcnow)

    def to_document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PollState":
        return _build(cls, document)


def _build(cls, document: dict[str, Any]):
    """Construct a dataclass from a stored document, ignoring unknown keys
    (so an older document from a previous version still loads) and repairing
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
    instance = cls(**kwargs)
    return instance
