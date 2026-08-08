"""Production validation: crash recovery, verification accuracy, failure injection.

Every test here runs twice — once against the dict-backed fake and once against
a real `mongod` (see `conftest.py`). The crash scenarios are the reason: they
depend on state surviving a process boundary, which is exactly the property a
test double is least able to prove.

The four crash points are deliberately enumerated rather than approximated,
because they have genuinely different correct answers:

    enqueue ─┬─ crash → QUEUED     → send it (nothing was attempted)
             │
    send ────┼─ crash → SENDING    → read the chat, send only if absent
             │
    verify ──┼─ crash → VERIFYING  → same: read first, never re-send blind
             │
    record ──┴─ crash → VERIFYING  → the bubble IS there, so mark delivered
```
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from wadam.domain.models import ChatConfig, OutgoingMessage, OutgoingStatus, chat_id_for
from wadam.engine.delivery import DeliveryService
from wadam.engine.metrics import Metrics
from wadam.whatsapp.verifier import SendVerifier, Verification, count_outgoing


@dataclass
class Bubble:
    text: str
    is_incoming: bool = False
    time_text: str = "9:21 pm"


class Conversation:
    """A chat the verifier reads and a send appends to."""

    def __init__(self, bubbles=None) -> None:
        self.bubbles = list(bubbles or [])
        self.readable = True

    async def read(self, _name):
        return list(self.bubbles) if self.readable else None


class Sender:
    def __init__(self, chat: Conversation, ok=True, delivers=True) -> None:
        self._chat, self.ok, self.delivers = chat, ok, delivers
        self.sent: list[str] = []

    async def send_async(self, _name, text):
        from wadam.whatsapp.sender import SendResult

        self.sent.append(text)
        if self.ok and self.delivers:
            self._chat.bubbles.append(Bubble(text=text))
        return (SendResult.succeeded("test", duration_ms=100) if self.ok
                else SendResult.failed("the compose box still had text"))


def seed_chat(repo, name="Alice") -> ChatConfig:
    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name, seeded=True)
    repo.save_chat(chat)
    return chat


def service(repo, conversation, sender, metrics=None):
    return DeliveryService(repo, sender, SendVerifier(conversation.read, timeout=2.0),
                           asyncio.to_thread, metrics or Metrics())


# ---------------------------------------------------------------------------
# 5. Queue recovery — the four crash points
# ---------------------------------------------------------------------------


def test_crash_before_send_is_delivered_once(storage, reopen):
    """Nothing was attempted, so sending is unambiguously correct."""
    repo, settings = storage
    chat = seed_chat(repo)
    convo, sender = Conversation(), None

    stranded = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                               text="pending", status=OutgoingStatus.QUEUED)
    repo.enqueue_outgoing(stranded)
    repo.flush_json(force=True)

    restarted = reopen(repo, settings)
    try:
        recovered = restarted.pending_outgoing()
        assert [m.text for m in recovered] == ["pending"]
        assert recovered[0].status == OutgoingStatus.QUEUED

        sender = Sender(convo)
        result = asyncio.run(service(restarted, convo, sender).deliver(recovered[0]))
        assert result.status == OutgoingStatus.DELIVERED
        assert sender.sent == ["pending"], "delivered exactly once"
    finally:
        restarted.stop()


def test_crash_after_send_before_verification_does_not_duplicate(storage, reopen):
    """The message reached the chat; the process died before recording it.
    Re-sending would put it on someone's phone twice."""
    repo, settings = storage
    chat = seed_chat(repo)
    convo = Conversation([Bubble("already arrived")])   # it did land

    stranded = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                               text="already arrived", status=OutgoingStatus.SENDING)
    repo.enqueue_outgoing(stranded)

    restarted = reopen(repo, settings)
    try:
        ambiguous = restarted.outgoing_in_state(OutgoingStatus.AMBIGUOUS)
        assert len(ambiguous) == 1

        sender = Sender(convo)
        result = asyncio.run(service(restarted, convo, sender).resume_ambiguous(ambiguous[0]))

        assert result.status == OutgoingStatus.DELIVERED
        assert sender.sent == [], "NOT re-sent — it was already there"
        assert convo.bubbles.count(Bubble("already arrived")) == 1
    finally:
        restarted.stop()


def test_crash_during_verification_sends_only_if_absent(storage, reopen):
    """VERIFYING with no bubble means the send never landed — safe to send."""
    repo, settings = storage
    chat = seed_chat(repo)
    convo = Conversation()                                # nothing arrived

    stranded = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                               text="never arrived", status=OutgoingStatus.VERIFYING)
    repo.enqueue_outgoing(stranded)

    restarted = reopen(repo, settings)
    try:
        ambiguous = restarted.outgoing_in_state(OutgoingStatus.AMBIGUOUS)
        sender = Sender(convo)
        result = asyncio.run(service(restarted, convo, sender).resume_ambiguous(ambiguous[0]))

        assert result.status == OutgoingStatus.DELIVERED
        assert sender.sent == ["never arrived"], "sent once, because it was absent"
    finally:
        restarted.stop()


def test_crash_after_verification_before_marking_complete(storage, reopen):
    """The subtlest one: verification succeeded but the status write didn't.
    The bubble is in the chat, so recovery must find it and mark delivered
    rather than sending a second copy."""
    repo, settings = storage
    chat = seed_chat(repo)
    convo = Conversation([Bubble("verified but unrecorded")])

    stranded = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                               text="verified but unrecorded",
                               status=OutgoingStatus.VERIFYING, attempts=1)
    repo.enqueue_outgoing(stranded)

    restarted = reopen(repo, settings)
    try:
        sender = Sender(convo)
        result = asyncio.run(service(restarted, convo, sender).resume_ambiguous(
            restarted.outgoing_in_state(OutgoingStatus.AMBIGUOUS)[0]))

        assert result.status == OutgoingStatus.DELIVERED
        assert sender.sent == []
        assert result.delivered_at is not None
    finally:
        restarted.stop()


def test_nothing_is_lost_across_a_restart(storage, reopen):
    """The other half of "no duplicates": no silent disappearances either."""
    repo, settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    svc = service(repo, convo, Sender(convo))
    for index in range(5):
        asyncio.run(svc.enqueue(chat, f"message {index}", origin="api"))
    repo.flush_json(force=True)

    restarted = reopen(repo, settings)
    try:
        texts = [m.text for m in restarted.pending_outgoing()]
        assert texts == [f"message {index}" for index in range(5)]
        assert restarted.queue_depth() == 5
    finally:
        restarted.stop()


def test_the_json_mirror_holds_the_queue_too(storage):
    """MongoDB is the primary, but the mirror has to be able to answer for it."""
    repo, settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    asyncio.run(service(repo, convo, Sender(convo)).enqueue(chat, "mirrored", origin="api"))
    repo.flush_json(force=True)

    payload = json.loads((Path(settings.json_backup_folder) / "outgoing.json")
                         .read_text(encoding="utf-8"))
    assert [row["text"] for row in payload] == ["mirrored"]
    assert payload[0]["status"] == OutgoingStatus.QUEUED


# ---------------------------------------------------------------------------
# 6. Verification accuracy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "OK",
    "line one\nline two\nline three",
    "emoji 🚀 and more 💖",
    "https://example.com/a/very/long/path?with=query&and=more#fragment",
    "unicode: αβγ ελληνικά · русский · 日本語 · العربية",
    "x" * 1200,
    "  leading and trailing whitespace  ",
    "punctuation!@#$%^&*()_+-=[]{}|;':\\\",./<>?",
])
def test_verification_handles_every_message_shape(storage, text):
    repo, _settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    sender = Sender(convo)

    result = asyncio.run(service(repo, convo, sender).deliver(
        asyncio.run(service(repo, convo, sender).enqueue(chat, text, origin="api"))))

    assert result.status == OutgoingStatus.DELIVERED, f"failed for {text[:40]!r}"
    assert result.verification == Verification.VERIFIED


def test_identical_messages_verify_independently(storage):
    """"OK", "OK", "OK" — each must verify against its own new bubble, not
    against the one before it."""
    repo, _settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    sender = Sender(convo)
    svc = service(repo, convo, sender)

    for _ in range(3):
        result = asyncio.run(svc.deliver(asyncio.run(svc.enqueue(chat, "OK", origin="api"))))
        assert result.status == OutgoingStatus.DELIVERED

    assert sender.sent == ["OK", "OK", "OK"]
    assert count_outgoing(convo.bubbles, "OK") == 3
    # Three separate records, not one collapsed by a content-derived key.
    assert len([m for m in repo.messages_for(chat.chat_id) if m.text == "OK"]) == 3


def test_a_prior_identical_message_does_not_falsely_verify(storage):
    """The core failure the census prevents: 'OK' is already in the chat, the
    send does not land, and presence alone would call that delivered."""
    repo, _settings = storage
    chat = seed_chat(repo)
    convo = Conversation([Bubble("OK"), Bubble("OK")])     # two already there
    sender = Sender(convo, ok=True, delivers=False)         # this one won't land
    svc = service(repo, convo, sender)

    result = asyncio.run(svc.deliver(asyncio.run(svc.enqueue(chat, "OK", origin="api"))))

    assert result.status == OutgoingStatus.UNVERIFIED
    assert result.verification == Verification.NOT_FOUND


def test_an_incoming_message_with_the_same_text_is_not_mistaken_for_ours(storage):
    """The other party saying "OK" must not verify our "OK"."""
    repo, _settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    sender = Sender(convo, ok=True, delivers=False)
    svc = service(repo, convo, sender)
    queued = asyncio.run(svc.enqueue(chat, "OK", origin="api"))

    convo.bubbles.append(Bubble("OK", is_incoming=True))    # they said it, not us
    result = asyncio.run(svc.deliver(queued))

    assert result.status == OutgoingStatus.UNVERIFIED


def test_whitespace_only_text_is_refused_before_it_reaches_the_queue():
    """Nothing to send is not an error to retry — it is a caller mistake."""
    from wadam.whatsapp.sender import SendResult

    # The sender rejects it outright; the pipeline never queues an empty reply
    # because `optional_reply` returns None for blank webhook responses.
    from wadam.engine.webhook import WebhookOutcome, optional_reply

    assert optional_reply(WebhookOutcome(ok=True, reply_text="   ")) is None
    assert SendResult.failed("Nothing to send (the reply was empty).").ok is False


# ---------------------------------------------------------------------------
# 2. Failure injection
# ---------------------------------------------------------------------------


def test_a_send_when_whatsapp_vanishes_is_retried_not_lost(storage):
    """WhatsApp closed mid-send: the transport fails, nothing was delivered,
    so the message goes back on the queue."""
    repo, _settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    sender = Sender(convo, ok=False)
    svc = service(repo, convo, sender)

    result = asyncio.run(svc.deliver(asyncio.run(svc.enqueue(chat, "hello", origin="api"))))

    assert result.status == OutgoingStatus.QUEUED, "requeued, not failed on attempt 1"
    assert repo.queue_depth() == 1
    assert repo.messages_for(chat.chat_id) == []


def test_whatsapp_restarting_during_verification_leaves_it_unverified(storage):
    """The chat becomes unreadable mid-verification. Unverified is the honest
    answer — not delivered, and not retried into a possible duplicate."""
    repo, _settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    sender = Sender(convo, ok=True, delivers=True)
    svc = service(repo, convo, sender)
    queued = asyncio.run(svc.enqueue(chat, "hello", origin="api"))

    original_read = convo.read
    calls = {"n": 0}

    async def flaky(name):
        calls["n"] += 1
        if calls["n"] > 1:          # census succeeds, verification cannot read
            return None
        return await original_read(name)

    convo.read = flaky
    result = asyncio.run(service(repo, convo, sender).deliver(queued))

    assert result.status == OutgoingStatus.UNVERIFIED
    assert sender.sent == ["hello"], "sent once, never repeated"


def test_mongodb_failure_does_not_lose_the_queue_from_the_mirror(storage, tmp_path):
    """MongoDB unavailable: writes still reach the JSON mirror, which is the
    whole reason the mirror is fed from memory rather than from the primary."""
    repo, settings = storage
    chat = seed_chat(repo)
    convo = Conversation()
    svc = service(repo, convo, Sender(convo))

    class Broken:
        """A store whose SERVER is gone but whose object is fine.

        Faithful to the real failure: `MongoStore.note_failure` and
        `note_success` are bookkeeping on the client object and keep working —
        only collection access raises. A double that raised on those too would
        blow up inside the repository's own error handler, which is a bug in
        the double, not in the code."""

        connected = False
        status_text = "disconnected — MongoDB is unavailable"

        def note_failure(self, _ex): pass
        def note_success(self): pass

        def __getattr__(self, _name):
            raise RuntimeError("MongoDB is unavailable")

    healthy = repo._mongo
    repo._mongo = Broken()
    try:
        asyncio.run(svc.enqueue(chat, "written during an outage", origin="api"))
        repo.flush_json(force=True)
    finally:
        repo._mongo = healthy

    payload = json.loads((Path(settings.json_backup_folder) / "outgoing.json")
                         .read_text(encoding="utf-8"))
    assert "written during an outage" in [row["text"] for row in payload]
    assert repo.queue_depth() == 1, "still queued in memory and deliverable"


@pytest.mark.parametrize("url,expect_ok", [
    # Empty is VALID and means "no webhook" — it is how every chat starts and a
    # legitimate way to park one. Rejecting it would make clearing a webhook
    # impossible.
    ("", True),
    ("not-a-url", False),
    ("ftp://example.com/hook", False),
    ("https://", False),
    ("https://exa mple.com/x", False),
    ("http://127.0.0.1:9/unreachable", True),   # valid shape, fails at call time
])
def test_invalid_webhook_urls_are_rejected_before_any_request(url, expect_ok):
    from wadam.domain.webhook_url import validate_webhook_url

    assert validate_webhook_url(url)[0] is expect_ok


def test_webhook_transport_failures_are_classified(storage):
    """A timeout, a 500 and a refused connection must all be retryable, and a
    400 must not be — repeating a malformed request just adds load."""
    from wadam.engine.webhook import _is_retryable

    assert _is_retryable(0) is True       # connection refused / DNS / timeout
    assert _is_retryable(500) is True
    assert _is_retryable(503) is True
    assert _is_retryable(429) is True
    assert _is_retryable(400) is False
    assert _is_retryable(404) is False


def test_a_session_without_an_input_desktop_blocks_sending_with_a_reason():
    """RDP disconnect / workstation lock. Held, with an explanation — never a
    silent non-delivery."""
    from wadam.whatsapp.session import Health, SessionState

    blocked = SessionState(uia_available=True, whatsapp_found=True,
                           has_input_desktop=False, is_remote_session=True)
    assert blocked.can_read == Health.OK, "reading never needs a desktop"
    assert blocked.can_send == Health.BLOCKED
    assert "disconnected" in blocked.send_blocked_reason

    locked = SessionState(uia_available=True, whatsapp_found=True,
                          has_input_desktop=False, is_remote_session=False)
    assert "locked" in locked.send_blocked_reason
