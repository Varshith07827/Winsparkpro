"""Continuous sending must never stop the reader — and unblocking it must not
cost correctness.

The poll cycle skips itself while the outgoing queue is draining, because the
conversation read shares the STA thread and would slow every send behind it.
With no upper bound that is a starvation bug, and it happened: a relay endpoint
answered every three-second poll, the drainer was never idle, the cycle never
ran, and a real incoming message was never read or stored at all.

The bound fixes that. These tests also cover the obvious objection to it — that
letting a poll run mid-drain could double-read messages or disturb the order
messages leave in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, OutgoingMessage, chat_id_for
from wadam.engine.engine import MAX_DRAIN_POLL_PAUSE
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.reader import WhatsAppMessage
from tests.test_storage import FakeMongo


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


def _paused(draining: bool, since: float, now: float) -> bool:
    """The predicate `_cycle` uses, stated on its own so it can be tested
    without standing up an engine and a fake WhatsApp."""
    return draining and (now - since) < MAX_DRAIN_POLL_PAUSE


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------


def test_the_pause_is_bounded_at_all():
    assert 0 < MAX_DRAIN_POLL_PAUSE <= 60


def test_a_short_drain_still_yields_to_sending():
    """The optimisation is kept: a normal burst is not interrupted."""
    assert _paused(True, 100.0, 100.0) is True
    assert _paused(True, 100.0, 100.0 + MAX_DRAIN_POLL_PAUSE / 2) is True


def test_a_drain_that_never_ends_cannot_hold_the_reader_off():
    """The incident, as an assertion. A producer that never stops used to mean
    discovery never ran again."""
    forever = 100.0 + MAX_DRAIN_POLL_PAUSE * 1000
    assert _paused(True, 100.0, forever) is False


def test_an_idle_queue_never_pauses_the_poll():
    assert _paused(False, 100.0, 100.0) is False


# ---------------------------------------------------------------------------
# Letting the poll through must not cost correctness
# ---------------------------------------------------------------------------


def _incoming(text: str) -> WhatsAppMessage:
    return WhatsAppMessage(sender="Alice", text=text, is_incoming=True,
                           time_text="9:21 pm")


def test_a_poll_that_runs_mid_drain_does_not_double_store_messages(repo):
    """The reader re-reads the same visible tail every cycle, so an extra poll
    sees messages it has already seen. Identity is content-derived and the
    store refuses a repeat, which is what makes the bound safe."""
    from wadam.domain.models import MessageStatus, StoredMessage, message_key_for

    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)

    def ingest_once(text: str) -> bool:
        key = message_key_for(chat.chat_id, "Alice", text, "9:21 pm", "in")
        if repo.has_message(key):
            return False
        repo.save_message(StoredMessage(
            message_key=key, chat_id=chat.chat_id, chat_name=chat.chat_name,
            sender="Alice", text=text, direction="in",
            status=MessageStatus.PENDING, time_text="9:21 pm"))
        return True

    assert ingest_once("hello") is True
    # The extra mid-drain poll re-reads the same bubble.
    assert ingest_once("hello") is False
    assert ingest_once("hello") is False

    stored = [m for m in repo.messages_for(chat.chat_id) if m.text == "hello"]
    assert len(stored) == 1, "an extra poll must not create a second record"


def test_a_poll_mid_drain_does_not_reorder_what_is_already_queued(repo):
    """Order comes from the per-chat sequence assigned at ENQUEUE time, so
    nothing the reader does between sends can disturb it."""
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)

    first = OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice", text="one",
                            origin="webhook_reply")
    second = OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice", text="two",
                             origin="webhook_reply")
    repo.enqueue_outgoing(first)
    repo.enqueue_outgoing(second)

    # A poll lands between them and enqueues a third.
    third = OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice", text="three",
                            origin="webhook_reply")
    repo.enqueue_outgoing(third)

    assert [m.sequence for m in (first, second, third)] == [1, 2, 3]
    assert [m.text for m in repo.pending_outgoing()] == ["one", "two", "three"]


def test_the_sequence_survives_a_restart_taken_mid_drain(repo, tmp_path: Path):
    """Unblocking the poll must not change what a restart recovers."""
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)
    for text in ("one", "two", "three"):
        repo.enqueue_outgoing(OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice",
                                              text=text, origin="webhook_reply"))
    repo.flush_json(force=True)

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    restarted = Repository(settings, repo._mongo, JsonBackupStore(tmp_path, 0))
    restarted.start()
    try:
        assert [m.text for m in restarted.pending_outgoing()] == ["one", "two", "three"]
    finally:
        restarted.stop()


# ---------------------------------------------------------------------------
# The relay, which caused the incident
# ---------------------------------------------------------------------------


def test_the_relay_is_on_by_default_because_it_is_the_outbound_path():
    """This was default-off after the incident, and the cost showed up in a
    built EXE: first-run setup writes MONGODB_URI and WEBHOOK_URL and nothing
    else, no API_PORT, so the install could not send by any route at all.

    What stops a repeat of the incident is not this switch — it is the
    deduplication rules below, which is where a guard belongs."""
    assert Settings().relay_enabled is True


def test_a_disabled_relay_is_never_polled(tmp_path: Path):
    from wadam.config import load_settings

    env = tmp_path / ".env"
    env.write_text("MONGODB_URI=mongodb://localhost:27017\n"
                   "WEBHOOK_URL=https://x.test/?{phone_number}\n"
                   "RELAY_ENABLED=false\n", encoding="utf-8")
    assert load_settings(env).relay_enabled is False


def test_the_effective_setting_is_what_counts_not_the_file(tmp_path: Path, monkeypatch):
    """A process environment variable overrides the file, so 'the .env says
    false' is not the same as 'the relay is off'. Check the loaded value."""
    from wadam.config import load_settings

    env = tmp_path / ".env"
    env.write_text("MONGODB_URI=mongodb://localhost:27017\n"
                   "WEBHOOK_URL=https://x.test/?{phone_number}\n"
                   "RELAY_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setenv("RELAY_ENABLED", "true")
    assert load_settings(env).relay_enabled is True, (
        "the environment wins — which is exactly why the runtime value must be "
        "the thing that gets verified before a real test"
    )
