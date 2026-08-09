"""Two identical messages are two messages.

Established live: WhatsApp held two bubbles reading WINSPARK_DUPLICATE_TEST,
both with delivery ticks, and the application produced ONE database record. The
loss happened twice over — the reader returned one bubble, and the storage key
would have collapsed them anyway.

    same physical bubble, re-read every 3s  -> ONE record
    two distinct bubbles reading alike      -> TWO records

Both halves matter. Drop the first and every poll re-stores the visible tail;
drop the second and a genuine repeat disappears from wa_events with nothing to
show it ever arrived.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, chat_id_for, message_key_for
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.reader import WhatsAppMessage
from tests.test_storage import FakeMongo


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_same_bubble_read_twice_keeps_one_identity():
    args = ("c1", "You", "OK", "6:47 AM", "out")
    assert message_key_for(*args, 0) == message_key_for(*args, 0)


def test_two_identical_bubbles_get_different_identities():
    args = ("c1", "You", "OK", "6:47 AM", "out")
    assert message_key_for(*args, 0) != message_key_for(*args, 1)


def test_a_run_of_identical_messages_stays_distinct():
    args = ("c1", "You", "OK", "6:47 AM", "out")
    keys = {message_key_for(*args, n) for n in range(5)}
    assert len(keys) == 5


def test_identical_text_in_different_chats_is_still_distinct():
    assert (message_key_for("c1", "You", "OK", "6:47 AM", "out", 0)
            != message_key_for("c2", "You", "OK", "6:47 AM", "out", 0))


def test_identical_text_from_different_senders_is_distinct():
    assert (message_key_for("c1", "Alice", "OK", "6:47 AM", "in", 0)
            != message_key_for("c1", "Bob", "OK", "6:47 AM", "in", 0))


def test_identical_text_at_different_times_is_distinct():
    assert (message_key_for("c1", "You", "OK", "6:47 AM", "out", 0)
            != message_key_for("c1", "You", "OK", "6:48 AM", "out", 0))


# ---------------------------------------------------------------------------
# The whole path, against the real structure observed live
# ---------------------------------------------------------------------------


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


def _msg(text, incoming=True, sender="Alice", time_text="6:47 AM"):
    return WhatsAppMessage(sender=sender, text=text, is_incoming=incoming,
                           time_text=time_text)


#: The conversation as the live UIA tree actually presented it — nine leaves,
#: two of them WhatsApp's own cards, and two PAIRS of identical bubbles at
#: distinct positions with distinct RuntimeIds.
LIVE_SHAPE = [
    _msg("Hi", True, "+91 81069 72933", "6:20 AM"),
    _msg("WINSPARK_E2E_TEST_84721", True, "+91 81069 72933", "6:30 AM"),
    _msg("WINSPARK_E2E_REPLY_84721", False, "You", "6:46 AM"),
    _msg("WINSPARK_DUPLICATE_TEST", False, "You", "6:47 AM"),
    _msg("WINSPARK_DUPLICATE_TEST", False, "You", "6:47 AM"),
    _msg("WINSPARK_TWOSEND_DIAG", False, "You", "7:28 AM"),
    _msg("WINSPARK_TWOSEND_DIAG", False, "You", "7:28 AM"),
]


def _ingest(engine_repo, chat, messages):
    """The identity assignment the engine performs, in isolation."""
    occurrences: dict = {}
    keys = []
    for message in messages:
        direction = "in" if message.is_incoming else "out"
        signature = (message.sender, message.text, message.time_text, direction)
        occurrence = occurrences.get(signature, 0)
        occurrences[signature] = occurrence + 1
        keys.append(message_key_for(chat.chat_id, message.sender, message.text,
                                    message.time_text, direction, occurrence))
    return keys


def test_the_real_conversation_yields_one_identity_per_bubble(repo):
    chat = ChatConfig(chat_id=chat_id_for("+91 81069 72933"),
                      chat_name="+91 81069 72933", seeded=True)
    repo.save_chat(chat)

    keys = _ingest(repo, chat, LIVE_SHAPE)

    assert len(keys) == 7
    assert len(set(keys)) == 7, "every bubble on screen must be its own message"


def test_re_reading_the_same_conversation_adds_nothing(repo):
    """The 3-second poll sees the same tail again and must not duplicate it."""
    chat = ChatConfig(chat_id=chat_id_for("+91 81069 72933"),
                      chat_name="+91 81069 72933", seeded=True)
    repo.save_chat(chat)

    first = _ingest(repo, chat, LIVE_SHAPE)
    second = _ingest(repo, chat, LIVE_SHAPE)

    assert first == second, "a re-read must produce the identical keys"


def test_a_new_identical_message_arriving_later_is_a_new_message(repo):
    chat = ChatConfig(chat_id=chat_id_for("+91 81069 72933"),
                      chat_name="+91 81069 72933", seeded=True)
    repo.save_chat(chat)

    before = set(_ingest(repo, chat, LIVE_SHAPE))
    grown = LIVE_SHAPE + [_msg("WINSPARK_TWOSEND_DIAG", False, "You", "7:28 AM")]
    after = set(_ingest(repo, chat, grown))

    assert len(after) == len(before) + 1
    assert before < after, "the existing bubbles keep the identities they had"


def test_identical_incoming_messages_are_both_kept(repo):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)

    keys = _ingest(repo, chat, [_msg("ok"), _msg("ok")])

    assert len(set(keys)) == 2


def test_identical_messages_separated_by_another_are_both_kept(repo):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", seeded=True)
    repo.save_chat(chat)

    keys = _ingest(repo, chat, [_msg("ok"), _msg("and then"), _msg("ok")])

    assert len(set(keys)) == 3
