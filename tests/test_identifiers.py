"""Seven identifiers, none interchangeable.

`POST /wam/ {"id": "2933"}` has to reach one chat and no other, so what each
identifier is — and is not — has to be pinned rather than assumed. The audit
that produced this file is in docs/DATA.md.

    chat_id         sha1 of the chat NAME. Stable while the name is. Routing: yes.
    external_id     last 4 digits. What /wam/ addresses. NOT unique by nature.
    phone_number    full digits. Identity. Empty when unknown, never guessed.
    chat_name       what WhatsApp displays. Changes. Not an identifier.
    message_key     content hash of an incoming bubble. Dedup.
    outgoing_id     uuid4 per queued message. Unique forever.
    correlation_id  one message's thread through the logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.api.resolver import resolve_chat, suggest_external_id
from wadam.config import Settings
from wadam.domain.models import (
    ChatConfig,
    OutgoingMessage,
    chat_id_for,
    contact_id_for,
    message_key_for,
    phone_digits,
)
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.test_storage import FakeMongo


def _chat(name, phone="", external="") -> ChatConfig:
    return ChatConfig(chat_id=chat_id_for(name), chat_name=name,
                      phone_number=phone, external_id=external)


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


def test_external_id_is_the_last_four_digits_not_the_number():
    assert contact_id_for("918106972933") == "2933"
    assert contact_id_for("+91 81069 72933") == "2933"


def test_external_id_and_phone_number_are_not_interchangeable():
    chat = _chat("+91 81069 72933", phone="918106972933", external="2933")
    assert chat.external_id != chat.phone_number
    assert chat.phone_number.endswith(chat.external_id)


def test_phone_digits_normalises_every_spelling():
    for spelling in ("+91 81069 72933", "918106972933", "+91-81069-72933",
                     "(91) 81069 72933"):
        assert phone_digits(spelling) == "918106972933"


def test_an_unknown_number_is_empty_never_invented():
    assert phone_digits("Alice") == ""
    assert contact_id_for("Alice") == ""
    assert suggest_external_id(_chat("Alice")) == ""


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
    chats = [_chat("+91 81069 72933", "918106972933", "2933"), _chat("Alice")]
    result = resolve_chat(chats, "2933")
    assert result.ok and result.chat.chat_name == "+91 81069 72933"
    assert result.matched_by == "external_id"


def test_an_unknown_id_resolves_to_nothing_rather_than_a_guess():
    result = resolve_chat([_chat("Alice"), _chat("Bob")], "9999")
    assert not result.ok
    assert not result.ambiguous


def test_a_duplicate_external_id_is_refused_not_picked():
    """Four digits is 10,000 values. Sending to the wrong person is the one
    failure this must never produce quietly."""
    chats = [_chat("+91 81069 72933", "918106972933", "2933"),
             _chat("+44 7700 902933", "447700902933", "2933")]
    result = resolve_chat(chats, "2933")
    assert not result.ok
    assert result.ambiguous
    assert len(result.candidates) == 2


def test_a_full_number_resolves_even_when_the_short_id_is_ambiguous():
    chats = [_chat("+91 81069 72933", "918106972933", "2933"),
             _chat("+44 7700 902933", "447700902933", "2933")]
    result = resolve_chat(chats, "918106972933")
    assert result.ok and result.matched_by == "phone_number"


def test_a_saved_contact_cannot_be_addressed_by_number(_no_number=None):
    """The documented limitation, stated as a test so it cannot regress into a
    silent guess. A saved contact exposes no number, so it has no external_id
    and `/wam/` cannot reach it by one."""
    chats = [_chat("Varshith")]
    assert suggest_external_id(chats[0]) == ""
    assert not resolve_chat(chats, "9423").ok


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
    repo.save_chat(_chat("+91 81069 72933", "918106972933", "2933"))
    repo.flush_json(force=True)

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    restarted = Repository(settings, repo._mongo, JsonBackupStore(tmp_path, 0))
    restarted.start()
    try:
        chat = restarted.list_chats()[0]
        assert chat.phone_number == "918106972933"
        assert chat.external_id == "2933"
        assert chat.chat_id == chat_id_for("+91 81069 72933")
        assert resolve_chat(restarted.list_chats(), "2933").ok
    finally:
        restarted.stop()


def test_the_ambiguity_response_tells_the_caller_what_to_use_instead(tmp_path: Path):
    """A 409 that said "set a distinct contact ID" pointed at a configuration
    field the simplified product no longer has. The advice has to be something
    the caller can actually act on, so it names the identifiers that do not
    collide."""
    from tests.test_send_api import FakeEngine
    from wadam.api.host import SendApiHost

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    try:
        repository.save_chat(_chat("+91 81069 72933", "918106972933", "2933"))
        repository.save_chat(_chat("+44 7700 902933", "447700902933", "2933"))
        host = SendApiHost(settings, repository, FakeEngine())

        response = host._send("2933", "must not be sent")

        assert response.status == 409
        assert response.payload["code"] == "ambiguous_id"
        assert len(response.payload["candidates"]) == 2
        assert "phone_number" in response.payload["resolves_by"]
        assert "full phone number" in response.payload["error"]
        assert "LAST FOUR DIGITS" in response.payload["error"]
    finally:
        repository.stop()
