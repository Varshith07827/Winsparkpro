"""Restart recovery — the reliability requirements' hardest promise.

"Never lose a message", "no duplicate webhook calls", "no duplicate outgoing
messages" and "persistent queues survive restarts" pull against each other: the
safest way to never lose a message is to retry everything, and the safest way to
never duplicate is to retry nothing. What reconciles them is knowing, for each
message, what the outside world has already seen — which is why the pipeline
persists a state *before* each irreversible step rather than after.

These tests kill the process at each of those points and assert what happens
next.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, MessageStatus, StoredMessage, chat_id_for, message_key_for
from wadam.engine.engine import AutomationEngine, _Job
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository

from tests.test_storage import FakeMongo


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


class _RecoveryHarness:
    """`_recover_incomplete` reads the repository and enqueues jobs; binding it
    to a stand-in exercises the real decision table without an STA thread, a
    WhatsApp window or a running loop."""

    def __init__(self, repository: Repository) -> None:
        self._repo = repository
        self.jobs: list[_Job] = []

    def _enqueue(self, job: _Job) -> None:
        self.jobs.append(job)

    _recover_incomplete = None  # bound below


def make_harness(repository: Repository) -> _RecoveryHarness:
    harness = _RecoveryHarness(repository)
    harness._recover_incomplete = AutomationEngine._recover_incomplete.__get__(
        harness, _RecoveryHarness
    )
    return harness


def seed_chat(repo: Repository, name: str = "Alice") -> ChatConfig:
    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name,
                      webhook_url="https://x.test/hook", automation_enabled=True, seeded=True)
    repo.save_chat(chat)
    return chat


def store(repo: Repository, chat: ChatConfig, text: str, status: str,
          reply: str = "") -> StoredMessage:
    message = StoredMessage(
        message_key=message_key_for(chat.chat_id, "Alice", text, "9:00 am", "in"),
        chat_id=chat.chat_id, chat_name=chat.chat_name, sender="Alice", text=text,
        direction="in", time_text="9:00 am", status=status, reply_text=reply,
    )
    repo.save_message(message)
    return message


# ---------------------------------------------------------------------------
# The decision table
# ---------------------------------------------------------------------------


def test_a_message_stored_but_not_yet_dispatched_is_resumed(repo: Repository):
    chat = seed_chat(repo)
    store(repo, chat, "hello", MessageStatus.PENDING)
    harness = make_harness(repo)

    asyncio.run(harness._recover_incomplete())

    # PENDING is written before the webhook is called, so the endpoint provably
    # has not seen this. Resuming it cannot duplicate anything.
    assert [(j.kind, j.message.text) for j in harness.jobs] == [("process", "hello")]


def test_a_message_interrupted_mid_webhook_is_never_retried(repo: Repository):
    chat = seed_chat(repo)
    message = store(repo, chat, "hello", MessageStatus.DISPATCHING)
    harness = make_harness(repo)

    asyncio.run(harness._recover_incomplete())

    # The endpoint may already have received it and may already have acted on
    # it. Retrying risks a duplicate webhook call, which is worse than a missed
    # reply — so it is parked, loudly, for a person.
    assert harness.jobs == []
    stored = [m for m in repo.messages_for(chat.chat_id) if m.message_key == message.message_key][0]
    assert stored.status == MessageStatus.INTERRUPTED
    assert "may already have received it" in stored.error
    assert any(entry.event == "recovery.interrupted" for entry in repo.recent_logs())


def test_an_unsent_reply_is_queued_for_a_verified_resume(repo: Repository):
    chat = seed_chat(repo)
    store(repo, chat, "hello", MessageStatus.AWAITING_SEND, reply="hi there")
    harness = make_harness(repo)

    asyncio.run(harness._recover_incomplete())

    # Not "send it again" — "go and check, then decide".
    assert [j.kind for j in harness.jobs] == ["resume"]
    assert harness.jobs[0].message.reply_text == "hi there"


def test_finished_and_seeded_messages_are_left_alone(repo: Repository):
    chat = seed_chat(repo)
    for status in (MessageStatus.SEEDED, MessageStatus.REPLIED, MessageStatus.WEBHOOK_OK,
                   MessageStatus.WEBHOOK_FAILED, MessageStatus.IGNORED,
                   MessageStatus.REPLY_FAILED):
        store(repo, chat, f"text-{status}", status)
    harness = make_harness(repo)

    asyncio.run(harness._recover_incomplete())

    assert harness.jobs == []


def test_work_for_a_deleted_chat_is_dropped(repo: Repository):
    chat = seed_chat(repo)
    store(repo, chat, "hello", MessageStatus.PENDING)
    repo.delete_chat(chat.chat_id)
    harness = make_harness(repo)

    asyncio.run(harness._recover_incomplete())

    assert harness.jobs == []


def test_recovery_is_silent_when_there_is_nothing_to_recover(repo: Repository):
    seed_chat(repo)
    harness = make_harness(repo)
    asyncio.run(harness._recover_incomplete())
    assert harness.jobs == []
    assert not any(e.event.startswith("recovery.") for e in repo.recent_logs())


# ---------------------------------------------------------------------------
# The verified resume itself
# ---------------------------------------------------------------------------


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    async def send_async(self, chat_name: str, text: str):
        from wadam.whatsapp.sender import SendResult

        self.sent.append((chat_name, text))
        return SendResult.succeeded("test") if self.ok else SendResult.failed("not verified")


def build_pipeline(repo: Repository, sender: FakeSender):
    from wadam.engine.pipeline import MessagePipeline
    from wadam.engine.webhook import WebhookClient

    return MessagePipeline(repo, WebhookClient(), sender, asyncio.to_thread)


def test_a_reply_already_in_the_chat_is_not_sent_twice(repo: Repository):
    chat = seed_chat(repo)
    message = store(repo, chat, "hello", MessageStatus.AWAITING_SEND, reply="hi there")
    sender = FakeSender()

    # The crash happened after the send landed but before it was recorded.
    asyncio.run(build_pipeline(repo, sender).resume_send(chat, message, already_sent=True))

    assert sender.sent == [], "the reply was already delivered — sending it again is a duplicate"
    assert message.status == MessageStatus.REPLIED


def test_a_reply_missing_from_the_chat_is_sent(repo: Repository):
    chat = seed_chat(repo)
    message = store(repo, chat, "hello", MessageStatus.AWAITING_SEND, reply="hi there")
    sender = FakeSender()

    asyncio.run(build_pipeline(repo, sender).resume_send(chat, message, already_sent=False))

    assert sender.sent == [("Alice", "hi there")]
    assert message.status == MessageStatus.REPLIED
    assert chat.last_outgoing_text == "hi there"


def test_an_unreadable_chat_defers_rather_than_guessing(repo: Repository):
    chat = seed_chat(repo)
    message = store(repo, chat, "hello", MessageStatus.AWAITING_SEND, reply="hi there")
    sender = FakeSender()

    asyncio.run(build_pipeline(repo, sender).resume_send(chat, message, already_sent=None))

    assert sender.sent == []
    # Still AWAITING_SEND: it stays recoverable, and the next restart will try
    # to verify again. "I don't know" is answered by waiting, not by sending.
    assert message.status == MessageStatus.AWAITING_SEND


# ---------------------------------------------------------------------------
# State survives a full restart
# ---------------------------------------------------------------------------


def test_configuration_and_dedup_survive_a_restart(tmp_path: Path):
    """Close and reopen: chats, their settings, and the knowledge of which
    messages have already been seen all come back."""
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    mongo = FakeMongo()  # the same instance stands in for a persistent database

    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    first = Repository(settings, mongo, backup)
    first.start()
    chat = seed_chat(first, "Alice")
    chat.automation_enabled = True
    chat.webhook_url = "https://x.test/alice"
    first.save_chat(chat)
    key = message_key_for(chat.chat_id, "Alice", "hello", "9:00 am", "in")
    store(first, chat, "hello", MessageStatus.REPLIED)
    first.stop()

    second = Repository(settings, JsonBackupStore(tmp_path, 0), backup)
    second._mongo = mongo
    second.start()

    restored = second.get_chat(chat.chat_id)
    assert restored is not None
    assert restored.automation_enabled is True
    assert restored.webhook_url == "https://x.test/alice"
    # And the message is still known, so the first poll after a restart does not
    # treat the whole visible conversation as new.
    assert second.has_message(key) is True
    second.stop()


def test_mongodb_wiped_but_json_intact_restores_the_configuration(tmp_path: Path):
    """Disaster recovery: the primary is gone, the mirror is not."""
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    first = Repository(settings, FakeMongo(), backup)
    first.start()
    chat = seed_chat(first, "Alice")
    chat.webhook_url = "https://x.test/alice"
    first.save_chat(chat)
    first.flush_json(force=True)
    first.stop()

    # A brand new, empty database — but the same backup folder.
    second = Repository(settings, FakeMongo(), JsonBackupStore(tmp_path, 0))
    second.start()

    assert second.recovered_from_json is True
    restored = second.get_chat(chat.chat_id)
    assert restored is not None and restored.webhook_url == "https://x.test/alice"
    # And it was written back, so the primary is whole again.
    assert second._mongo.chat_configs.documents
    second.stop()


# ---------------------------------------------------------------------------
# Queue behaviour
# ---------------------------------------------------------------------------


class _QueueHarness:
    def __init__(self) -> None:
        self._queued_chats: set[str] = set()
        self.items: list[_Job] = []

    class _Q:
        def __init__(self, items):
            self.items = items

        def put_nowait(self, job):
            self.items.append(job)

    _enqueue = None


def make_queue_harness() -> _QueueHarness:
    harness = _QueueHarness()
    harness._queue = _QueueHarness._Q(harness.items)
    harness._enqueue = AutomationEngine._enqueue.__get__(harness, _QueueHarness)
    return harness


def test_a_chat_is_only_queued_for_scanning_once(tmp_path: Path):
    harness = make_queue_harness()
    harness._enqueue(_Job("scan", "chat-1"))
    harness._enqueue(_Job("scan", "chat-1"))
    harness._enqueue(_Job("scan", "chat-2"))

    # Three chatty seconds should not mean three passes over the same chat —
    # each one opens the conversation in front of the user.
    assert [j.chat_id for j in harness.items] == ["chat-1", "chat-2"]


def test_message_jobs_are_never_deduplicated(tmp_path: Path):
    harness = make_queue_harness()
    harness._enqueue(_Job("process", "chat-1", StoredMessage(message_key="a", text="one")))
    harness._enqueue(_Job("process", "chat-1", StoredMessage(message_key="b", text="two")))

    # Two messages from one chat are two pieces of work, not one.
    assert [j.message.text for j in harness.items] == ["one", "two"]
