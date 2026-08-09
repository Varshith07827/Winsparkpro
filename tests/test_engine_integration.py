"""The polling loop end to end, with WhatsApp replaced by a fake.

The reader is the only thing standing between the engine and a real WhatsApp
window, so substituting it exercises everything else for real: discovery,
change detection, the decision about which chats are worth opening, ingestion,
dedup, and reconnection after the window disappears.

This is where the acceptance criteria that say "within one polling cycle" and
"reconnects automatically without requiring a restart" are actually checked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import pytest

from wadam.config import Settings
from wadam.domain.models import MessageStatus, chat_id_for
from wadam.engine.engine import AutomationEngine
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.reader import ChatRow, WhatsAppMessage
from wadam.whatsapp.row_parser import parse_chat_row

from tests.test_storage import FakeMongo


def row(name: str, message: str = "hi", unread: int = 0, timestamp: str = "12:00 pm") -> ChatRow:
    prefix = f"{unread} unread messages " if unread else ""
    return ChatRow(**parse_chat_row(f"{prefix}{name} {timestamp} {message}"))


class FakeReader:
    """Stands in for WhatsApp. `window` is the handle it reports; setting it to
    None is WhatsApp closing, and setting it to a new number is WhatsApp being
    restarted with a fresh window."""

    def __init__(self, rows: Optional[list[ChatRow]] = None) -> None:
        self.window: Optional[int] = 1001
        self.rows: list[ChatRow] = rows or []
        self.active_name: Optional[str] = None
        self.messages: list[WhatsAppMessage] = []
        self.find_calls = 0
        self.message_reads = 0

    async def find_window_async(self) -> Optional[int]:
        self.find_calls += 1
        return self.window

    async def window_is_alive_async(self, handle: int) -> bool:
        return self.window is not None and handle == self.window

    async def read_chat_rows_async(self, _handle: int) -> list[ChatRow]:
        return list(self.rows)

    async def read_chat_rows_deep_async(self, _handle: int, _max_scrolls: int = 8) -> list[ChatRow]:
        return list(self.rows)

    async def get_active_conversation_name_async(self, _handle: int) -> Optional[str]:
        return self.active_name

    async def read_recent_messages_async(self, _handle: int, _limit: int = 25):
        self.message_reads += 1
        return list(self.messages)


class FakeSender:
    def __init__(self, reader: FakeReader) -> None:
        self._reader = reader
        self.opened: list[str] = []
        self.sent: list[tuple[str, str]] = []

    async def open_and_read_async(self, chat_name: str, limit: int = 25):
        self.opened.append(chat_name)
        return self._reader.window, list(self._reader.messages)

    async def send_async(self, chat_name: str, text: str):
        from wadam.whatsapp.sender import SendResult

        self.sent.append((chat_name, text))
        return SendResult.succeeded("test")


class NoopPipeline:
    """Records what the engine decided to process, without calling anything."""

    def __init__(self) -> None:
        self.processed: list[str] = []

    async def process(self, _chat, message) -> None:
        self.processed.append(message.text)

    async def resume_send(self, *_a, **_k) -> None:
        pass


@pytest.fixture()
def engine(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()

    snapshots: list = []
    instance = AutomationEngine(settings, repository, snapshots.append)
    reader = FakeReader()
    instance._reader = reader
    instance._sender = FakeSender(reader)
    instance._pipeline = NoopPipeline()
    instance.snapshots = snapshots

    yield instance, reader, repository
    instance._sta.dispose()
    repository.stop()


async def drain(instance: AutomationEngine) -> None:
    """Run every queued job to completion, the way the worker would."""
    while not instance._queue.empty():
        job = instance._queue.get_nowait()
        chat = instance._repo.get_chat(job.chat_id)
        if chat is None:
            continue
        if job.kind == "scan":
            await instance._scan_chat(chat)
        elif job.kind == "process":
            await instance._pipeline.process(chat, job.message)
        instance._queued_chats.discard(job.chat_id)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_a_new_chat_is_registered_within_one_cycle(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice"), row("Bob")]

    asyncio.run(instance._cycle())

    assert {c.chat_name for c in repo.list_chats()} == {"Alice", "Bob"}
    for chat in repo.list_chats():
        assert chat.automation_enabled is False, "discovery never switches a chat on"
        # Addressed by name until a number is supplied, so it can forward the
        # moment it is ticked.
        assert chat.webhook_url.endswith(f"?{chat.chat_name}")


def test_a_chat_appearing_later_is_picked_up_on_the_next_cycle(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    assert len(repo.list_chats()) == 1

    reader.rows.append(row("Carol"))
    asyncio.run(instance._cycle())
    assert {c.chat_name for c in repo.list_chats()} == {"Alice", "Carol"}


def test_discovery_writes_through_to_the_json_mirror(engine, tmp_path: Path):
    import json

    from wadam import constants

    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    repo.flush_json(force=True)

    mirrored = json.loads((tmp_path / constants.JSON_CHATS).read_text(encoding="utf-8"))
    assert [c["chat_name"] for c in mirrored] == ["Alice"]


# ---------------------------------------------------------------------------
# Which chats get opened
# ---------------------------------------------------------------------------


def test_only_automated_chats_are_opened(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice"), row("Bob")]
    asyncio.run(instance._cycle())

    alice = repo.get_chat(chat_id_for("Alice"))
    alice.automation_enabled = True
    alice.seeded = True
    repo.save_chat(alice)

    # Both chats change; only the automated one is worth interrupting the user's
    # WhatsApp window for.
    reader.rows = [row("Alice", "new message", unread=1), row("Bob", "also new", unread=1)]
    asyncio.run(instance._cycle())
    asyncio.run(drain(instance))

    assert instance._sender.opened == ["Alice"]


def test_an_unchanged_chat_is_never_opened(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    alice = repo.get_chat(chat_id_for("Alice"))
    alice.automation_enabled = True
    alice.seeded = True
    repo.save_chat(alice)

    for _ in range(3):
        asyncio.run(instance._cycle())
        asyncio.run(drain(instance))

    assert instance._sender.opened == [], "nothing changed — nothing should have been opened"


def test_the_open_conversation_is_read_without_switching_chats(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    alice = repo.get_chat(chat_id_for("Alice"))
    alice.automation_enabled = True
    alice.seeded = True
    repo.save_chat(alice)

    reader.active_name = "Alice"
    reader.messages = [WhatsAppMessage(sender="Alice", text="are you there?",
                                       is_incoming=True, time_text="12:01 pm")]
    reader.rows = [row("Alice", "are you there?", unread=1)]
    asyncio.run(instance._cycle())
    asyncio.run(drain(instance))

    # It was read, and no chat was opened to do it.
    assert instance._pipeline.processed == ["are you there?"]
    assert instance._sender.opened == []


# ---------------------------------------------------------------------------
# Ingestion through the loop
# ---------------------------------------------------------------------------


def test_the_backlog_of_a_newly_automated_chat_is_not_answered(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    alice = repo.get_chat(chat_id_for("Alice"))
    alice.automation_enabled = True          # switched on with history on screen
    repo.save_chat(alice)

    reader.active_name = "Alice"
    reader.messages = [
        WhatsAppMessage(sender="Alice", text="old one", is_incoming=True, time_text="11:00 am"),
        WhatsAppMessage(sender="Alice", text="old two", is_incoming=True, time_text="11:01 am"),
    ]
    reader.rows = [row("Alice", "old two", unread=2)]
    asyncio.run(instance._cycle())
    asyncio.run(drain(instance))

    assert instance._pipeline.processed == []
    assert {m.status for m in repo.messages_for(alice.chat_id)} == {MessageStatus.SEEDED}

    # The next genuinely new message IS answered.
    reader.messages.append(
        WhatsAppMessage(sender="Alice", text="a new one", is_incoming=True, time_text="11:05 am")
    )
    reader.rows = [row("Alice", "a new one", unread=3)]
    asyncio.run(instance._cycle())
    asyncio.run(drain(instance))
    assert instance._pipeline.processed == ["a new one"]


def test_re_reading_the_same_conversation_produces_no_repeat_work(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    alice = repo.get_chat(chat_id_for("Alice"))
    alice.automation_enabled = True
    alice.seeded = True
    repo.save_chat(alice)

    reader.active_name = "Alice"
    reader.messages = [WhatsAppMessage(sender="Alice", text="hello",
                                       is_incoming=True, time_text="12:01 pm")]
    for index in range(4):
        reader.rows = [row("Alice", "hello", unread=1, timestamp=f"12:0{index} pm")]
        asyncio.run(instance._cycle())
        asyncio.run(drain(instance))

    # The poll re-reads the same visible tail every three seconds; the dedup key
    # is what stops that becoming four webhook calls.
    assert instance._pipeline.processed == ["hello"]


# ---------------------------------------------------------------------------
# Reconnection
# ---------------------------------------------------------------------------


def test_whatsapp_closing_is_survived_and_reconnected_to(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    assert instance._hwnd == 1001

    # WhatsApp closes.
    reader.window = None
    asyncio.run(instance._cycle())
    assert instance._hwnd is None
    assert repo.get_chat(chat_id_for("Alice")) is not None, "state is kept while it is away"

    # WhatsApp restarts — a different window handle, no application restart.
    reader.window = 2002
    reader.rows = [row("Alice"), row("Dave")]
    asyncio.run(instance._cycle())

    assert instance._hwnd == 2002
    assert {c.chat_name for c in repo.list_chats()} == {"Alice", "Dave"}


def test_a_recreated_window_is_detected_even_while_one_is_open(engine):
    instance, reader, _repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())

    # The old handle is dead but a new window exists — the "minimize, restore,
    # window recreated" case. The engine must not keep talking to the corpse.
    reader.window = 3003
    asyncio.run(instance._cycle())
    assert instance._hwnd == 3003


def test_an_empty_read_does_not_wipe_the_chat_list(engine):
    """A transient COMError read returns no rows. That is not the same as "the
    user has no chats", and must never be treated as one."""
    instance, reader, repo = engine
    reader.rows = [row("Alice"), row("Bob")]
    asyncio.run(instance._cycle())

    reader.rows = []
    asyncio.run(instance._cycle())

    assert len(repo.list_chats()) == 2


# ---------------------------------------------------------------------------
# Bulk automation
# ---------------------------------------------------------------------------


def test_the_global_switch_writes_every_chat_and_individuals_still_override(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice"), row("Bob"), row("Carol")]
    asyncio.run(instance._cycle())

    asyncio.run(instance.set_global_automation(True))
    assert all(c.automation_enabled for c in repo.list_chats())
    assert repo.app_state.global_automation_enabled is True

    # An individual chat overrides the bulk action, and the override stands.
    asyncio.run(instance.set_chat_automation(chat_id_for("Bob"), False))
    assert repo.get_chat(chat_id_for("Bob")).automation_enabled is False

    asyncio.run(instance.set_global_automation(False))
    assert not any(c.automation_enabled for c in repo.list_chats())

    # Switching one back on after a global OFF works — it is a bulk action, not
    # a master gate that would veto this.
    asyncio.run(instance.set_chat_automation(chat_id_for("Carol"), True))
    assert repo.get_chat(chat_id_for("Carol")).automation_enabled is True


def test_resetting_a_chat_rebaselines_it(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]
    asyncio.run(instance._cycle())
    alice = repo.get_chat(chat_id_for("Alice"))
    alice.automation_enabled = True
    alice.seeded = True
    alice.webhook_retry_count = 3
    alice.last_error = "something went wrong"
    repo.save_chat(alice)

    asyncio.run(instance.reset_automation(alice.chat_id))

    after = repo.get_chat(alice.chat_id)
    assert after.automation_enabled is False
    assert after.seeded is False
    assert after.webhook_retry_count == 0
    assert after.last_error == ""
    assert after.webhook_url == alice.webhook_url, "the URL is kept — reset is not delete"
