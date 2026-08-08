"""Draining a backlog as one run.

Per-message delivery paid a session probe, a quiet-desktop wait, a foreground
change, a pre-send census read and a post-send verification read — for every
message. A burst of twenty paid it twenty times, which is what made a queued
burst crawl.

Batching must not buy that back by weakening anything, so these tests pin the
guarantees rather than the speed: ordering, retry, and above all the counting
that makes repeated identical text verifiable.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, OutgoingMessage, OutgoingStatus, chat_id_for
from wadam.engine.delivery import DeliveryService
from wadam.engine.metrics import Metrics
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.verifier import SendVerifier
from tests.test_storage import FakeMongo


@dataclass
class FakeBubble:
    text: str
    is_incoming: bool = False
    time_text: str = "9:21 pm"


class FakeChat:
    def __init__(self, bubbles=None) -> None:
        self.bubbles = list(bubbles or [])
        self.readable = True
        self.reads = 0

    async def read(self, _chat_name):
        self.reads += 1
        return list(self.bubbles) if self.readable else None


class BatchSender:
    """Transport only, with the batch context the delivery service now uses."""

    def __init__(self, chat: FakeChat, delivers=True, fail_texts=()) -> None:
        self._chat = chat
        self.delivers = delivers
        self.fail_texts = set(fail_texts)
        self.sent: list[str] = []
        self.batches = 0

    @asynccontextmanager
    async def batch(self):
        self.batches += 1

        async def send(chat_name: str, text: str):
            from wadam.whatsapp.sender import SendResult

            if text in self.fail_texts:
                return SendResult.failed("the compose box still had text")
            self.sent.append(text)
            if self.delivers:
                self._chat.bubbles.append(FakeBubble(text=text))
            return SendResult.succeeded("test", duration_ms=120)

        yield send


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


def make_chat(repo: Repository, name: str) -> ChatConfig:
    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name, seeded=True)
    repo.save_chat(chat)
    return chat


def queue(repo: Repository, chat: ChatConfig, *texts) -> list:
    messages = []
    for text in texts:
        message = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                                  text=text, origin="api")
        repo.enqueue_outgoing(message)
        messages.append(message)
    return messages


def build(repo, chat_state, sender, timeout=2.0):
    return DeliveryService(repo, sender, SendVerifier(chat_state.read, timeout=timeout),
                           asyncio.to_thread, Metrics())


# ---------------------------------------------------------------------------


def test_ten_messages_are_delivered_in_one_batch(repo):
    state = FakeChat()
    sender = BatchSender(state)
    chat = make_chat(repo, "Alice")
    messages = queue(repo, chat, *[f"message {i}" for i in range(10)])

    asyncio.run(build(repo, state, sender).deliver_batch(messages))

    assert sender.batches == 1, "the foreground should be taken once, not ten times"
    assert [m.status for m in messages] == [OutgoingStatus.DELIVERED] * 10
    # One census + at least one confirmation, NOT one pair per message.
    assert state.reads <= 3, f"expected ~2 conversation reads, got {state.reads}"


def test_order_is_preserved_within_a_chat(repo):
    state = FakeChat()
    sender = BatchSender(state)
    chat = make_chat(repo, "Alice")
    messages = queue(repo, chat, "first", "second", "third")

    asyncio.run(build(repo, state, sender).deliver_batch(messages))

    assert sender.sent == ["first", "second", "third"]


def test_messages_for_different_chats_keep_their_interleaving(repo):
    """Grouping is by CONSECUTIVE chat, not by chat.

    Sorting the queue to batch each chat together would be faster still and
    would break the promise that messages leave in the order they were made."""
    state = FakeChat()
    sender = BatchSender(state)
    alice = make_chat(repo, "Alice")
    bob = make_chat(repo, "Bob")
    messages = (queue(repo, alice, "a1") + queue(repo, bob, "b1")
                + queue(repo, alice, "a2"))

    asyncio.run(build(repo, state, sender).deliver_batch(messages))

    assert sender.sent == ["a1", "b1", "a2"]


def test_three_identical_messages_each_need_their_own_bubble(repo):
    """The arithmetic batching could most easily get wrong.

    Presence is not enough: "OK" sent three times must produce three NEW
    bubbles. A check that merely looked for "OK" would pass after the first."""
    state = FakeChat(bubbles=[FakeBubble("OK")])   # one already there
    sender = BatchSender(state)
    chat = make_chat(repo, "Alice")
    messages = queue(repo, chat, "OK", "OK", "OK")

    asyncio.run(build(repo, state, sender).deliver_batch(messages))

    assert [m.status for m in messages] == [OutgoingStatus.DELIVERED] * 3
    assert sum(1 for b in state.bubbles if b.text == "OK") == 4


def test_a_batch_that_only_half_lands_marks_exactly_the_missing_ones(repo):
    """Two of three "OK"s arrive: one message is unverified, not all three."""
    state = FakeChat()
    sender = BatchSender(state)
    chat = make_chat(repo, "Alice")
    messages = queue(repo, chat, "OK", "OK", "OK")

    # The transport reports success every time, but the chat only ever gets two.
    original = sender._chat.bubbles

    async def run():
        service = build(repo, state, sender, timeout=0.5)
        # Drop the third bubble as it is appended.
        real_batch = sender.batch

        @asynccontextmanager
        async def limited():
            async with real_batch() as send:
                async def capped(chat_name, text):
                    result = await send(chat_name, text)
                    if len(original) > 2:
                        original.pop()          # the third never really lands
                    return result
                yield capped

        sender.batch = limited
        await service.deliver_batch(messages)

    asyncio.run(run())

    statuses = [m.status for m in messages]
    assert statuses.count(OutgoingStatus.DELIVERED) == 2
    assert statuses.count(OutgoingStatus.UNVERIFIED) == 1


def test_a_transport_failure_does_not_stop_the_rest_of_the_batch(repo):
    state = FakeChat()
    sender = BatchSender(state, fail_texts={"second"})
    chat = make_chat(repo, "Alice")
    messages = queue(repo, chat, "first", "second", "third")

    asyncio.run(build(repo, state, sender).deliver_batch(messages))

    assert sender.sent == ["first", "third"]
    assert messages[0].status == OutgoingStatus.DELIVERED
    assert messages[2].status == OutgoingStatus.DELIVERED
    # Retried, not abandoned: it never left the compose box.
    assert messages[1].status == OutgoingStatus.QUEUED
    assert messages[1].attempts == 1


def test_an_unreadable_conversation_makes_the_whole_batch_unverified(repo):
    """Never a guess. If the baseline could not be read, nothing is claimed."""
    state = FakeChat()
    state.readable = False
    sender = BatchSender(state)
    chat = make_chat(repo, "Alice")
    messages = queue(repo, chat, "one", "two")

    asyncio.run(build(repo, state, sender).deliver_batch(messages))

    assert [m.status for m in messages] == [OutgoingStatus.UNVERIFIED] * 2
    assert all(m.verification == "unreadable" for m in messages)
    # Sent exactly once each — an unverified message is never re-sent.
    assert sender.sent == ["one", "two"]


def test_a_deleted_chat_cancels_its_slice_without_touching_the_others(repo):
    state = FakeChat()
    sender = BatchSender(state)
    alice = make_chat(repo, "Alice")
    ghost = ChatConfig(chat_id="gone", chat_name="Ghost", seeded=True)
    messages = queue(repo, alice, "a1")
    orphan = OutgoingMessage(chat_id=ghost.chat_id, chat_name=ghost.chat_name,
                             text="never", origin="api")
    repo.enqueue_outgoing(orphan)

    asyncio.run(build(repo, state, sender).deliver_batch(messages + [orphan]))

    assert messages[0].status == OutgoingStatus.DELIVERED
    assert orphan.status == OutgoingStatus.CANCELLED
    assert sender.sent == ["a1"]
