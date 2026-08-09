"""The baseline must be read from the TARGET chat, before the send.

A real outbound send arrived in WhatsApp and was still marked UNVERIFIED. The
cause was not the census but the read behind it: `_read_for_verification`
refuses to switch chats, on the reasoning that after a send the right
conversation is already on screen. True for the post-send confirmation, false
for the pre-send baseline — the target chat may not be open at all, and then
the baseline is unreadable and everything in the batch is UNVERIFIED.

The census itself is not weakened by any of this. `after > before` remains the
only thing that counts as delivery.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, OutgoingMessage, OutgoingStatus, chat_id_for
from wadam.engine.delivery import DeliveryService
from wadam.engine.metrics import Metrics
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.verifier import SendVerifier
from tests.test_batch_delivery import FakeBubble
from tests.test_storage import FakeMongo


class Screen:
    """WhatsApp with several chats, one of them on screen.

    `read` is the passive reader: it can only see the chat that is open.
    `open_and_read` is the controlled chat switch the sender already uses."""

    def __init__(self, open_chat: str = "") -> None:
        self.chats: dict[str, list] = {}
        self.open_chat = open_chat
        self.opens: list[str] = []

    async def read(self, chat_name: str):
        if chat_name != self.open_chat:
            return None          # a different chat is open — cannot verify
        return list(self.chats.get(chat_name, []))

    async def open_and_read(self, chat_name: str):
        self.opens.append(chat_name)
        self.open_chat = chat_name
        return list(self.chats.get(chat_name, []))


class Sender:
    def __init__(self, screen: Screen, delivers=True, deliver_limit=None) -> None:
        self.screen = screen
        self.delivers = delivers
        self.deliver_limit = deliver_limit
        self.sent: list[str] = []

    @asynccontextmanager
    async def batch(self):
        async def send(chat_name: str, text: str):
            from wadam.whatsapp.sender import SendResult

            self.sent.append(text)
            landed = self.delivers and (self.deliver_limit is None
                                        or len(self.sent) <= self.deliver_limit)
            if landed:
                self.screen.open_chat = chat_name
                self.screen.chats.setdefault(chat_name, []).append(
                    FakeBubble(text=text))
            return SendResult.succeeded("test", duration_ms=10)

        yield send


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    yield repository
    repository.stop()


def _chat(repo, name="Alice"):
    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name, seeded=True)
    repo.save_chat(chat)
    return chat


def _queue(repo, chat, *texts):
    out = []
    for text in texts:
        message = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                                  text=text, origin="api")
        repo.enqueue_outgoing(message)
        out.append(message)
    return out


def _service(repo, screen, sender):
    verifier = SendVerifier(screen.read, timeout=1.0,
                            open_and_read=screen.open_and_read)
    return DeliveryService(repo, sender, verifier, asyncio.to_thread, Metrics())


# ---------------------------------------------------------------------------
# 1-3. Where the target chat is
# ---------------------------------------------------------------------------


def test_target_chat_already_open(repo):
    screen = Screen(open_chat="Alice")
    sender = Sender(screen)
    chat = _chat(repo)
    messages = _queue(repo, chat, "hello")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert messages[0].status == OutgoingStatus.DELIVERED
    assert messages[0].verification == "verified"


def test_a_different_chat_is_open(repo):
    """The bug. The baseline used to be unreadable and the send UNVERIFIED
    despite arriving."""
    screen = Screen(open_chat="Someone Else")
    sender = Sender(screen)
    chat = _chat(repo)
    messages = _queue(repo, chat, "hello")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert screen.opens == ["Alice"], "the target chat must be brought on screen"
    assert messages[0].status == OutgoingStatus.DELIVERED
    assert messages[0].verification == "verified"


def test_no_chat_open_at_all(repo):
    screen = Screen(open_chat="")
    sender = Sender(screen)
    chat = _chat(repo)
    messages = _queue(repo, chat, "hello")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert messages[0].verification == "verified"


def test_the_baseline_is_read_before_the_send(repo):
    """Order matters: a baseline taken afterwards would count our own bubble
    and could never detect a loss."""
    screen = Screen(open_chat="Someone Else")
    sender = Sender(screen)
    chat = _chat(repo)
    messages = _queue(repo, chat, "hello")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert messages[0].verification == "verified"
    # The open happened, and the sender ran after it.
    assert screen.opens and sender.sent == ["hello"]


# ---------------------------------------------------------------------------
# 4-6. The census is not weakened
# ---------------------------------------------------------------------------


def test_a_pre_existing_identical_message_does_not_verify_a_new_one(repo):
    """The whole reason the census counts instead of looking."""
    screen = Screen(open_chat="Someone Else")
    screen.chats["Alice"] = [FakeBubble(text="OK")]     # already there
    sender = Sender(screen, delivers=False)              # nothing new lands
    chat = _chat(repo)
    messages = _queue(repo, chat, "OK")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert messages[0].status == OutgoingStatus.UNVERIFIED
    assert messages[0].verification == "not_found"


def test_two_identical_messages_each_need_their_own_bubble(repo):
    screen = Screen(open_chat="Someone Else")
    screen.chats["Alice"] = [FakeBubble(text="OK")]
    sender = Sender(screen)
    chat = _chat(repo)
    messages = _queue(repo, chat, "OK", "OK")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert [m.status for m in messages] == [OutgoingStatus.DELIVERED] * 2
    assert sum(1 for b in screen.chats["Alice"] if b.text == "OK") == 3


def test_two_identical_sends_where_only_one_lands(repo):
    """Exactly one is marked unverified — not both, not neither. This is the
    real-world shape the live run produced."""
    screen = Screen(open_chat="Someone Else")
    sender = Sender(screen, deliver_limit=1)
    chat = _chat(repo)
    messages = _queue(repo, chat, "SAME", "SAME")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    statuses = [m.status for m in messages]
    assert statuses.count(OutgoingStatus.DELIVERED) == 1
    assert statuses.count(OutgoingStatus.UNVERIFIED) == 1


def test_an_unreadable_conversation_is_still_unverified(repo):
    """If the chat cannot be opened or read at all, nothing is claimed."""
    screen = Screen(open_chat="Someone Else")

    async def refuse(_chat_name):
        return None

    screen.open_and_read = refuse
    sender = Sender(screen)
    chat = _chat(repo)
    messages = _queue(repo, chat, "hello")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert messages[0].status == OutgoingStatus.UNVERIFIED
    assert messages[0].verification == "unreadable"


def test_transport_success_is_never_delivery_success(repo):
    """The sender reports success for every send here; only the bubble decides."""
    screen = Screen(open_chat="Alice")
    sender = Sender(screen, delivers=False)
    chat = _chat(repo)
    messages = _queue(repo, chat, "hello")

    asyncio.run(_service(repo, screen, sender).deliver_batch(messages))

    assert sender.sent == ["hello"], "transport ran and reported success"
    assert messages[0].status == OutgoingStatus.UNVERIFIED
