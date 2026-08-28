"""Persistence: MongoDB as the primary, the JSON mirror alongside it.

These run twice — once against the dict-backed fake and once against a real
`mongod` when one is reachable. See `conftest.py` for why.
"""

from __future__ import annotations

import json
from pathlib import Path

from wadam.domain.models import ChatConfig, MessageStatus, StoredMessage

CHAT_ID = "111111111111111@lid"


def read_json(folder: Path, name: str):
    return json.loads((folder / name).read_text(encoding="utf-8"))


def a_chat(chat_id: str = CHAT_ID, name: str = "Alice", **kwargs) -> ChatConfig:
    return ChatConfig(chat_id=chat_id, chat_name=name, **kwargs)


def a_message(key: str = "m1", chat_id: str = CHAT_ID, text: str = "hi",
              **kwargs) -> StoredMessage:
    return StoredMessage(message_key=key, chat_id=chat_id, chat_name="Alice",
                         text=text, **kwargs)


# ── chats ─────────────────────────────────────────────────────────────


def test_a_chat_write_reaches_both_stores(storage, tmp_path: Path):
    repo, settings = storage
    repo.save_chat(a_chat())
    repo.flush_json(force=True)

    assert repo.get_chat(CHAT_ID).chat_name == "Alice"
    mirrored = read_json(settings.json_backup_folder, "chats.json")
    assert any(c["chat_id"] == CHAT_ID for c in mirrored)


def test_automation_survives_a_restart(storage, reopen):
    repo, settings = storage
    repo.save_chat(a_chat(automation_enabled=True))

    restarted = reopen(repo, settings)
    try:
        assert restarted.get_chat(CHAT_ID).automation_enabled is True
    finally:
        restarted.stop()


def test_deleting_a_chat_takes_its_messages(storage):
    repo, _ = storage
    repo.save_chat(a_chat())
    repo.save_message(a_message())

    repo.delete_chat(CHAT_ID)

    assert repo.get_chat(CHAT_ID) is None
    assert repo.messages_for(CHAT_ID) == []


# ── messages and deduplication ────────────────────────────────────────


def test_a_message_is_stored_once(storage):
    repo, _ = storage

    assert repo.save_message(a_message(key="m1")) is True
    assert repo.save_message(a_message(key="m1")) is False
    assert len(repo.messages_for(CHAT_ID)) == 1


def test_two_different_keys_are_two_messages(storage):
    repo, _ = storage
    repo.save_message(a_message(key="m1", text="ok"))
    repo.save_message(a_message(key="m2", text="ok"))

    assert len(repo.messages_for(CHAT_ID)) == 2


def test_deduplication_survives_a_restart(storage, reopen):
    """A webhook delivery retried across a restart must still be recognised.
    The old in-memory set could not do this; the unique index can."""
    repo, settings = storage
    repo.save_message(a_message(key="m1"))

    restarted = reopen(repo, settings)
    try:
        assert restarted.save_message(a_message(key="m1")) is False
    finally:
        restarted.stop()


def test_a_message_status_can_be_updated(storage):
    repo, _ = storage
    message = a_message()
    repo.save_message(message)

    message.status = MessageStatus.REPLIED
    message.reply_text = "pong"
    repo.update_message(message)

    stored = repo.messages_for(CHAT_ID)[0]
    assert stored.status == MessageStatus.REPLIED
    assert stored.reply_text == "pong"


def test_messages_are_scoped_to_their_chat(storage):
    repo, _ = storage
    repo.save_message(a_message(key="m1", chat_id="a@c.us"))
    repo.save_message(a_message(key="m2", chat_id="b@c.us"))

    assert len(repo.messages_for("a@c.us")) == 1
    assert repo.message_count("b@c.us") == 1


# ── the mirror ────────────────────────────────────────────────────────


def test_messages_reach_the_json_mirror(storage, tmp_path: Path):
    repo, settings = storage
    repo.save_message(a_message(text="hello"))
    repo.flush_json(force=True)

    mirrored = read_json(settings.json_backup_folder, "messages.json")
    assert any(m["text"] == "hello" for m in mirrored)


def test_the_activity_log_is_mirrored_but_not_stored_in_mongo(storage):
    """A diagnostic trail is worth keeping and is not worth paying a cloud
    provider to keep for you."""
    repo, settings = storage
    repo.log("INFO", "automation.toggled", chat_id=CHAT_ID, message="on")
    repo.flush_json(force=True)

    mirrored = read_json(settings.json_backup_folder, "logs.json")
    assert any(entry["event"] == "automation.toggled" for entry in mirrored)
    assert "automation.toggled" in {entry.event for entry in repo.recent_logs()}


def test_status_reports_both_stores(storage):
    repo, _ = storage
    status = repo.status()

    assert status["mongodb_ok"] == "yes"
    assert status["json_ok"] == "yes"
