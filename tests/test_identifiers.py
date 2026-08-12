"""Six identifiers, none interchangeable.

`POST /wam/ {"id": "918106972933"}` has to reach one chat and no other, so what
each identifier is — and is not — has to be pinned rather than assumed. The
audit that produced this file is in docs/DATA.md.

    chat_id         sha1 of the chat NAME. Stable while the name is. Routing: yes.
    phone_number    full digits. Identity, and how a 1:1 chat is addressed.
                    Empty when unknown, never guessed. A GROUP never has one.
    chat_name       what WhatsApp displays. Changes — but it is how a group is
                    addressed, because a group has no number.
    message_key     content hash of an incoming bubble. Dedup.
    outgoing_id     uuid4 per queued message. Unique forever.
    correlation_id  one message's thread through the logs.

**The four-digit `external_id` is gone.** It was the primary way `/wam/`
addressed a chat, derived from the last four digits of a number. Four digits is
10,000 values, so with a few hundred chats a collision is likely rather than
exotic (the birthday bound puts it above even odds at ~118 chats) — and the
failure it produces is the worst one available here: a message delivered
quietly to the wrong person. It bought nothing the full number does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.api.resolver import resolve_chat
from wadam.config import Settings
from wadam.domain.models import (
    ChatConfig,
    OutgoingMessage,
    chat_id_for,
    message_key_for,
    phone_digits,
)
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.test_storage import FakeMongo


def _chat(name, phone="", is_group=False) -> ChatConfig:
    return ChatConfig(chat_id=chat_id_for(name), chat_name=name,
                      phone_number=phone, is_group=is_group)


# ---------------------------------------------------------------------------
# What each one IS
# ---------------------------------------------------------------------------


def test_chat_id_is_derived_from_the_name_and_is_stable():
    assert chat_id_for("Alice") == chat_id_for("Alice")
    assert chat_id_for("Alice") != chat_id_for("Bob")


def test_renaming_a_contact_produces_a_different_chat():
    """A documented consequence, not an accident: identity is hashed from the
    display name because WhatsApp exposes no durable chat identifier."""
    assert chat_id_for("Alice") != chat_id_for("Alice Smith")


def test_a_group_is_addressed_by_name_because_it_has_no_number():
    """Not a limitation to work around. A group has no single contact, so any
    number attached to one would be somebody else's."""
    group = _chat("Novus Tech Group", is_group=True)
    assert group.phone_number == ""
    result = resolve_chat([group], "Novus Tech Group")
    assert result.ok and result.matched_by == "chat_name"


def test_phone_digits_normalises_every_spelling():
    for spelling in ("+91 81069 72933", "918106972933", "+91-81069-72933",
                     "(91) 81069 72933"):
        assert phone_digits(spelling) == "918106972933"


def test_an_unknown_number_is_empty_never_invented():
    assert phone_digits("Alice") == ""


def test_message_key_distinguishes_direction():
    args = ("c1", "Alice", "hello", "9:21 pm")
    assert message_key_for(*args, "in") != message_key_for(*args, "out")


def test_outgoing_id_is_unique_per_queued_message():
    first, second = OutgoingMessage(text="OK"), OutgoingMessage(text="OK")
    assert first.outgoing_id != second.outgoing_id, (
        "two identical texts are two messages"
    )


# ---------------------------------------------------------------------------
# Routing: POST /wam/ {"id": ...}
# ---------------------------------------------------------------------------


def test_the_api_id_resolves_to_exactly_one_chat():
    chats = [_chat("+91 81069 72933", "918106972933"), _chat("Alice")]
    result = resolve_chat(chats, "918106972933")
    assert result.ok and result.chat.chat_name == "+91 81069 72933"
    assert result.matched_by == "phone_number"


def test_an_unknown_id_resolves_to_nothing_rather_than_a_guess():
    result = resolve_chat([_chat("Alice"), _chat("Bob")], "9999")
    assert not result.ok
    assert not result.ambiguous


def test_the_last_four_digits_no_longer_address_anything():
    """The removal, stated as a test so it cannot creep back.

    These two contacts shared a four-digit id and used to be an `ambiguous_id`
    409. Now "2933" is simply not an identifier, and an identifier nothing
    recognises sends nothing."""
    chats = [_chat("+91 81069 72933", "918106972933"),
             _chat("+44 7700 902933", "447700902933")]
    result = resolve_chat(chats, "2933")
    assert not result.ok
    assert not result.ambiguous


def test_two_contacts_sharing_a_suffix_are_told_apart_by_their_numbers():
    """What replaces it: the thing that used to collide now simply resolves."""
    chats = [_chat("+91 81069 72933", "918106972933"),
             _chat("+44 7700 902933", "447700902933")]
    assert resolve_chat(chats, "918106972933").chat.chat_name == "+91 81069 72933"
    assert resolve_chat(chats, "447700902933").chat.chat_name == "+44 7700 902933"


def test_a_chat_with_no_number_cannot_be_addressed_by_one():
    """No guessing. A chat whose number is unknown is not reachable by any
    number, rather than by one inferred from its name."""
    chats = [_chat("Varshith")]
    assert not resolve_chat(chats, "9423").ok
    assert not resolve_chat(chats, "917981149423").ok


def test_a_saved_contact_is_still_addressable_by_name():
    chats = [_chat("Varshith")]
    result = resolve_chat(chats, "Varshith")
    assert result.ok and result.matched_by == "chat_name"


# ---------------------------------------------------------------------------
# Persistence
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


def test_the_identifiers_survive_a_restart(repo, tmp_path: Path):
    repo.save_chat(_chat("+91 81069 72933", "918106972933"))
    repo.flush_json(force=True)

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    restarted = Repository(settings, repo._mongo, JsonBackupStore(tmp_path, 0))
    restarted.start()
    try:
        chat = restarted.list_chats()[0]
        assert chat.phone_number == "918106972933"
        assert chat.chat_id == chat_id_for("+91 81069 72933")
        assert resolve_chat(restarted.list_chats(), "918106972933").ok
    finally:
        restarted.stop()


def test_the_ambiguity_response_tells_the_caller_what_to_use_instead(tmp_path: Path):
    """Two chats with the same exact name is the only way to be ambiguous now.
    The advice has to be something the caller can act on, so it names the
    identifiers that do not collide."""
    from tests.test_send_api import FakeEngine
    from wadam.api.host import SendApiHost

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    try:
        first = _chat("Project")
        second = _chat("Project")
        second.chat_id = "a-second-chat-of-the-same-name"
        repository.save_chat(first)
        repository.save_chat(second)
        host = SendApiHost(settings, repository, FakeEngine())

        response = host._send("Project", "must not be sent")

        assert response.status == 409
        assert response.payload["code"] == "ambiguous_id"
        assert len(response.payload["candidates"]) == 2
        assert "phone_number" in response.payload["resolves_by"]
        assert "external_id" not in response.payload["resolves_by"]
        assert "full phone number" in response.payload["error"]
    finally:
        repository.stop()
