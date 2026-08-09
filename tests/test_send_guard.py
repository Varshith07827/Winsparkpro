"""Only one producer may speak while a real person is on the other end.

Written after a controlled end-to-end test sent about thirty unintended
messages to a real contact. The pipeline under test was not at fault: a relay
endpoint answered a poll every three seconds and the relay sent what it got.
Nothing was wrong — nothing had been told not to.

These tests pin the instrument that makes the next attempt safe.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, OutgoingMessage, OutgoingStatus, chat_id_for
from wadam.engine import send_guard
from wadam.engine.delivery import DeliveryService
from wadam.engine.metrics import Metrics
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.verifier import SendVerifier
from tests.test_batch_delivery import BatchSender, FakeChat


@pytest.fixture(autouse=True)
def _disarmed(monkeypatch):
    """The guard is off unless a test arms it, and never leaks between tests."""
    monkeypatch.delenv(send_guard.ENV_VAR, raising=False)
    yield


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    from tests.test_storage import FakeMongo

    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    yield repository
    repository.stop()


def _queued(repo, chat, text, origin):
    message = OutgoingMessage(chat_id=chat.chat_id, chat_name=chat.chat_name,
                              text=text, origin=origin)
    repo.enqueue_outgoing(message)
    return message


def _service(repo, state, sender):
    return DeliveryService(repo, sender, SendVerifier(state.read, timeout=1.0),
                           asyncio.to_thread, Metrics())


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_unarmed_by_default_so_production_is_untouched():
    assert send_guard.armed() is False
    send_guard.check("relay")          # must not raise
    send_guard.check("api")


def test_armed_it_permits_only_the_named_origin(monkeypatch):
    monkeypatch.setenv(send_guard.ENV_VAR, "webhook_reply")
    assert send_guard.armed() is True
    send_guard.check("webhook_reply")  # the pipeline under test

    for forbidden in ("relay", "api", "", "unknown", "manual"):
        with pytest.raises(send_guard.SendRefused):
            send_guard.check(forbidden)


def test_the_refusal_names_the_origin(monkeypatch):
    """The question during the incident was 'which producer sent this?'."""
    monkeypatch.setenv(send_guard.ENV_VAR, "webhook_reply")
    with pytest.raises(send_guard.SendRefused, match="relay"):
        send_guard.check("relay")


def test_every_send_logs_its_origin_even_unarmed(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="wadam.engine.send_guard"):
        send_guard.check("relay", chat_name="Alice", text="hello")
    assert any("origin=relay" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The guard on the real delivery path
# ---------------------------------------------------------------------------


def test_a_relay_message_is_refused_and_never_reaches_whatsapp(repo, monkeypatch):
    monkeypatch.setenv(send_guard.ENV_VAR, "webhook_reply")
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)
    state, sender = FakeChat(), BatchSender(FakeChat())
    sender._chat = state
    message = _queued(repo, chat, "relayed text", "relay")

    asyncio.run(_service(repo, state, sender).deliver_batch([message]))

    assert sender.sent == [], "the relay must not be able to send during the test"
    assert message.status == OutgoingStatus.CANCELLED
    assert "relay" in message.error


def test_an_api_message_is_refused_too(repo, monkeypatch):
    monkeypatch.setenv(send_guard.ENV_VAR, "webhook_reply")
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)
    state = FakeChat()
    sender = BatchSender(state)
    message = _queued(repo, chat, "api text", "api")

    asyncio.run(_service(repo, state, sender).deliver_batch([message]))

    assert sender.sent == []
    assert message.status == OutgoingStatus.CANCELLED


def test_the_pipeline_reply_is_the_one_thing_that_gets_through(repo, monkeypatch):
    monkeypatch.setenv(send_guard.ENV_VAR, "webhook_reply")
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)
    state = FakeChat()
    sender = BatchSender(state)
    message = _queued(repo, chat, "the controlled reply", "webhook_reply")

    asyncio.run(_service(repo, state, sender).deliver_batch([message]))

    assert sender.sent == ["the controlled reply"]
    assert message.status == OutgoingStatus.DELIVERED


def test_a_refused_message_is_cancelled_not_retried(repo, monkeypatch):
    """CANCELLED, because nothing was attempted. Retrying a message the guard
    exists to stop would defeat the guard on the next drain."""
    monkeypatch.setenv(send_guard.ENV_VAR, "webhook_reply")
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)
    state = FakeChat()
    sender = BatchSender(state)
    message = _queued(repo, chat, "relayed", "relay")

    service = _service(repo, state, sender)
    asyncio.run(service.deliver_batch([message]))
    assert message.status in OutgoingStatus.FINAL

    asyncio.run(service.deliver_batch(repo.pending_outgoing()))
    assert sender.sent == []


def test_a_mixed_batch_sends_only_the_permitted_one(repo, monkeypatch):
    monkeypatch.setenv(send_guard.ENV_VAR, "webhook_reply")
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)
    state = FakeChat()
    sender = BatchSender(state)
    messages = [
        _queued(repo, chat, "from relay", "relay"),
        _queued(repo, chat, "from pipeline", "webhook_reply"),
        _queued(repo, chat, "from api", "api"),
    ]

    asyncio.run(_service(repo, state, sender).deliver_batch(messages))

    assert sender.sent == ["from pipeline"]
