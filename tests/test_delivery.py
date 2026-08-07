"""Verification and the outgoing queue.

These two features exist to close the same gap: "the compose box cleared" is
evidence about an input box, not about a conversation. The tests below are
mostly about the ways that gap can be closed *wrongly* — confirming a message
that was already there, re-sending one that already arrived, losing one because
the process died at the wrong moment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, OutgoingMessage, OutgoingStatus, chat_id_for
from wadam.engine.delivery import DeliveryService
from wadam.engine.metrics import Metrics
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.verifier import SendVerifier, Verification, count_outgoing, normalise

from tests.test_storage import FakeMongo


@dataclass
class FakeBubble:
    text: str
    is_incoming: bool = False
    time_text: str = "9:21 pm"


class FakeChat:
    """A conversation the verifier can read, that a send can append to."""

    def __init__(self, bubbles=None) -> None:
        self.bubbles = list(bubbles or [])
        self.readable = True
        self.reads = 0

    async def read(self, _chat_name):
        self.reads += 1
        return list(self.bubbles) if self.readable else None


class FakeSender:
    """Transport only — it puts a bubble in the chat, or doesn't."""

    def __init__(self, chat: FakeChat, ok: bool = True, actually_delivers: bool = True) -> None:
        self._chat = chat
        self.ok = ok
        self.actually_delivers = actually_delivers
        self.sent: list[str] = []

    async def send_async(self, chat_name: str, text: str):
        from wadam.whatsapp.sender import SendResult

        self.sent.append(text)
        if self.ok and self.actually_delivers:
            self._chat.bubbles.append(FakeBubble(text=text))
        return (SendResult.succeeded("test", duration_ms=120) if self.ok
                else SendResult.failed("the compose box still had text"))


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    yield repository
    repository.stop()


def make_chat(repo: Repository, name: str = "Alice") -> ChatConfig:
    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name, seeded=True)
    repo.save_chat(chat)
    return chat


def build(repo: Repository, chat_state: FakeChat, sender: FakeSender):
    verifier = SendVerifier(chat_state.read, timeout=2.0)
    return DeliveryService(repo, sender, verifier, asyncio.to_thread, Metrics())


# ---------------------------------------------------------------------------
# Verification arithmetic
# ---------------------------------------------------------------------------


def test_counting_ignores_rendering_differences():
    bubbles = [FakeBubble("Hello  Varshith"), FakeBubble("Hello Varshith", is_incoming=True)]
    # Whitespace collapses, and only OUTGOING bubbles count.
    assert count_outgoing(bubbles, "Hello Varshith") == 1
    assert normalise("Hello  Varshith") == normalise("hello varshith")


def test_verification_requires_the_count_to_increase():
    """The obvious 'is the text present?' check passes when the message was
    already there from an earlier send — exactly what a verifier exists to
    catch."""
    chat = FakeChat([FakeBubble("OK")])
    verifier = SendVerifier(chat.read, timeout=1.0)

    before = asyncio.run(verifier.census("Alice", "OK"))
    assert before == 1

    # Nothing new arrives: presence alone would wrongly confirm.
    result = asyncio.run(verifier.confirm("Alice", "OK", before))
    assert not result.ok
    assert result.status == Verification.NOT_FOUND

    chat.bubbles.append(FakeBubble("OK"))
    assert asyncio.run(verifier.confirm("Alice", "OK", before)).ok


def test_an_unreadable_chat_is_unverified_not_confirmed():
    chat = FakeChat()
    chat.readable = False
    verifier = SendVerifier(chat.read, timeout=1.0)

    before = asyncio.run(verifier.census("Alice", "hi"))
    assert before is None
    result = asyncio.run(verifier.confirm("Alice", "hi", before))
    assert result.status == Verification.UNREADABLE
    assert not result.ok


def test_verification_reports_the_bubble_timestamp():
    chat = FakeChat()
    verifier = SendVerifier(chat.read, timeout=2.0)
    before = asyncio.run(verifier.census("Alice", "hi"))
    chat.bubbles.append(FakeBubble("hi", time_text="11:04 am"))
    result = asyncio.run(verifier.confirm("Alice", "hi", before))
    assert result.ok and result.bubble_time == "11:04 am"
    assert "confirmed" in result.describe()


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_a_message_is_durable_before_anything_is_attempted(repo: Repository):
    chat = make_chat(repo)
    state = FakeChat()
    service = build(repo, state, FakeSender(state))

    queued = asyncio.run(service.enqueue(chat, "hello", origin="api"))

    assert queued.status == OutgoingStatus.QUEUED
    assert repo.queue_depth() == 1
    # On disk in both stores from the moment it exists.
    assert repo._mongo.outgoing.documents
    assert (Path(repo._backup.folder) / "outgoing.json").exists()


def test_per_chat_ordering_is_assigned_at_enqueue_time(repo: Repository):
    alice, bob = make_chat(repo, "Alice"), make_chat(repo, "Bob")
    state = FakeChat()
    service = build(repo, state, FakeSender(state))

    a1 = asyncio.run(service.enqueue(alice, "first", origin="api"))
    b1 = asyncio.run(service.enqueue(bob, "bob one", origin="api"))
    a2 = asyncio.run(service.enqueue(alice, "second", origin="api"))

    assert (a1.sequence, a2.sequence) == (1, 2), "a chat's messages must keep their order"
    assert b1.sequence == 1, "sequences are per chat, not global"


def test_a_delivered_message_is_verified_and_recorded(repo: Repository):
    chat = make_chat(repo)
    state = FakeChat()
    sender = FakeSender(state)
    service = build(repo, state, sender)

    queued = asyncio.run(service.enqueue(chat, "Hello Varshith", origin="relay"))
    result = asyncio.run(service.deliver(queued))

    assert result.status == OutgoingStatus.DELIVERED
    assert result.verification == Verification.VERIFIED
    assert result.delivered_at is not None
    assert sender.sent == ["Hello Varshith"]
    assert [m.text for m in repo.messages_for(chat.chat_id)] == ["Hello Varshith"]
    assert repo.queue_depth() == 0


def test_transport_failure_requeues_and_eventually_gives_up(repo: Repository):
    """Never left the compose box, so nothing was delivered — safe to retry."""
    chat = make_chat(repo)
    state = FakeChat()
    sender = FakeSender(state, ok=False)
    service = build(repo, state, sender)

    queued = asyncio.run(service.enqueue(chat, "hello", origin="api"))
    for _ in range(3):
        queued = asyncio.run(service.deliver(queued))

    assert queued.attempts == 3
    assert queued.status == OutgoingStatus.FAILED
    assert repo.messages_for(chat.chat_id) == [], "nothing is claimed as sent"


def test_a_send_that_leaves_the_box_but_never_arrives_is_not_retried(repo: Repository):
    """The dangerous case: the transport succeeded, so a retry risks a
    duplicate. Recorded as unverified and surfaced instead."""
    chat = make_chat(repo)
    state = FakeChat()
    sender = FakeSender(state, ok=True, actually_delivers=False)
    service = build(repo, state, sender)

    queued = asyncio.run(service.enqueue(chat, "hello", origin="api"))
    result = asyncio.run(service.deliver(queued))

    assert result.status == OutgoingStatus.UNVERIFIED
    assert result.verification == Verification.NOT_FOUND
    assert sender.sent == ["hello"], "sent exactly once — never retried"
    assert repo.messages_for(chat.chat_id) == []
    assert any(e.event == "outgoing.unverified" for e in repo.recent_logs())


def test_verification_failure_is_logged_separately_from_transport_failure(repo: Repository):
    chat = make_chat(repo)
    state = FakeChat()
    service = build(repo, state, FakeSender(state, ok=True, actually_delivers=False))
    asyncio.run(service.deliver(asyncio.run(service.enqueue(chat, "a", origin="api"))))

    service2 = build(repo, state, FakeSender(state, ok=False))
    asyncio.run(service2.deliver(asyncio.run(service2.enqueue(chat, "b", origin="api"))))

    events = {e.event for e in repo.recent_logs()}
    assert "outgoing.unverified" in events   # could not prove it arrived
    assert "outgoing.retry" in events        # could not get it out of the box


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


def test_an_in_flight_message_that_did_arrive_is_not_sent_again(repo: Repository):
    """The process died between sending and recording. The message is on the
    recipient's screen; sending it again would be a duplicate."""
    chat = make_chat(repo)
    state = FakeChat([FakeBubble("already there")])
    sender = FakeSender(state)
    service = build(repo, state, sender)

    stranded = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                               text="already there", status=OutgoingStatus.SENDING)
    repo.enqueue_outgoing(stranded)

    result = asyncio.run(service.resume_ambiguous(stranded))

    assert result.status == OutgoingStatus.DELIVERED
    assert sender.sent == [], "it was already delivered — nothing re-sent"
    assert any(e.event == "outgoing.recovered" for e in repo.recent_logs())


def test_an_in_flight_message_that_never_arrived_is_sent(repo: Repository):
    chat = make_chat(repo)
    state = FakeChat()
    sender = FakeSender(state)
    service = build(repo, state, sender)

    stranded = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                               text="never arrived", status=OutgoingStatus.VERIFYING)
    repo.enqueue_outgoing(stranded)

    result = asyncio.run(service.resume_ambiguous(stranded))

    assert result.status == OutgoingStatus.DELIVERED
    assert sender.sent == ["never arrived"]


def test_the_queue_survives_a_restart(repo: Repository, tmp_path: Path):
    chat = make_chat(repo)
    state = FakeChat()
    service = build(repo, state, FakeSender(state))
    asyncio.run(service.enqueue(chat, "pending one", origin="api"))
    asyncio.run(service.enqueue(chat, "pending two", origin="api"))
    mongo = repo._mongo
    repo.stop()

    settings = Settings(mongodb_uri="x", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    restarted = Repository(settings, mongo, JsonBackupStore(tmp_path, 0))
    restarted.start()

    pending = restarted.pending_outgoing()
    assert [m.text for m in pending] == ["pending one", "pending two"]
    assert restarted.queue_depth() == 2
    restarted.stop()


def test_deleting_a_chat_cancels_what_it_still_owed(repo: Repository):
    chat = make_chat(repo)
    state = FakeChat()
    service = build(repo, state, FakeSender(state))
    asyncio.run(service.enqueue(chat, "never going out", origin="api"))

    cancelled = repo.cancel_outgoing_for_chat(chat.chat_id)

    assert cancelled == 1
    assert repo.queue_depth() == 0
    assert repo.outgoing_in_state((OutgoingStatus.CANCELLED,))[0].error


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_track_the_stages_separately(repo: Repository):
    chat = make_chat(repo)
    state = FakeChat()
    metrics = Metrics()
    verifier = SendVerifier(state.read, timeout=2.0)
    service = DeliveryService(repo, FakeSender(state), verifier, asyncio.to_thread, metrics)

    asyncio.run(service.deliver(asyncio.run(service.enqueue(chat, "one", origin="api"))))
    snapshot = metrics.snapshot(repo.queue_depth())

    assert snapshot.messages_queued == 1
    assert snapshot.messages_sent == 1
    assert snapshot.messages_verified == 1
    assert snapshot.verification_failures == 0
    assert snapshot.avg_send_ms > 0
    assert dict(snapshot.rows())["Delivery rate"] == "100%"


def test_metrics_averages_use_a_window_not_all_time():
    """A lifetime mean stops moving and will report a healthy number while
    everything recent is failing."""
    metrics = Metrics()
    for _ in range(200):
        metrics.record_sent(10)
    for _ in range(50):
        metrics.record_sent(1000)
    # The window is 50, so the recent slow sends dominate entirely.
    assert metrics.snapshot().avg_send_ms == pytest.approx(1000, rel=0.01)
