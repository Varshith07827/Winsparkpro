"""The simplified product: one template, one number per chat, two settings.

Everything here exists because the specification made something *fixed* that
used to be configurable, and a fixed thing with no test is a thing that quietly
becomes configurable again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam import constants
from wadam.config import Settings, load_settings
from wadam.domain.models import ChatConfig, MessageStatus, StoredMessage, chat_id_for
from wadam.domain.webhook_url import describe_missing, webhook_url_for
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.ui.first_run import env_text, needs_setup, validate, write_env
from tests.test_storage import FakeMongo


# ---------------------------------------------------------------------------
# Webhook generation
# ---------------------------------------------------------------------------


def test_the_url_is_built_from_the_template_and_the_number():
    assert webhook_url_for("https://noteify.org/ntext/whook/?{phone_number}",
                           "15551234567") == \
        "https://noteify.org/ntext/whook/?15551234567"


def test_a_chat_without_a_number_falls_back_to_its_name():
    """Every chat forwards from the moment it is ticked.

    A saved contact never exposes a number, so waiting for one meant those
    chats could never forward at all. The name identifies the chat and the
    receiving end can tell them apart."""
    assert webhook_url_for("https://noteify.org/ntext/whook/?{phone_number}", "",
                           chat_name="Novus Tech Group") ==         "https://noteify.org/ntext/whook/?Novus%20Tech%20Group"


def test_a_name_with_awkward_characters_is_url_encoded():
    """A raw "&" or "#" would truncate the query string."""
    url = webhook_url_for("https://n.test/?{phone_number}", "", chat_name="A & B #1")
    assert url == "https://n.test/?A%20%26%20B%20%231"


def test_a_number_is_preferred_over_the_name():
    assert webhook_url_for("https://n.test/?{phone_number}", "919423155555",
                           chat_name="Varshith") == "https://n.test/?919423155555"


def test_with_neither_a_number_nor_a_name_there_is_still_no_url():
    """The original rule survives: an empty substitution would be a valid-looking
    URL pointing at nobody."""
    assert webhook_url_for("https://n.test/?{phone_number}", "", chat_name="") == ""


def test_an_override_wins_over_the_template():
    assert webhook_url_for("https://noteify.org/?{phone_number}", "15551234567",
                           override="https://elsewhere.test/hook") == \
        "https://elsewhere.test/hook"


def test_a_template_without_a_placeholder_is_used_as_is():
    """Odd but explicit — the same URL for every chat. Warned about at startup,
    not silently turned into something else."""
    assert webhook_url_for("https://one.test/hook", "15551234567") == "https://one.test/hook"


def test_a_missing_number_is_explained_in_words():
    assert "name" in describe_missing("").lower()
    assert describe_missing("15551234567") == ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_the_database_name_is_fixed():
    assert Settings().database_name == "wa_events"
    assert constants.DATABASE_NAME == "wa_events"


def test_the_database_name_cannot_be_set_from_the_environment(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "MONGODB_URI=mongodb://localhost:27017\n"
        "WEBHOOK_URL=https://noteify.org/ntext/whook/?{phone_number}\n"
        "DATABASE_NAME=something_else\n",
        encoding="utf-8",
    )
    settings = load_settings(env)
    assert settings.database_name == "wa_events"
    assert any("DATABASE_NAME is ignored" in w for w in settings.warnings)


def test_the_env_file_needs_only_two_settings(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "MONGODB_URI=mongodb://localhost:27017\n"
        "WEBHOOK_URL=https://noteify.org/ntext/whook/?{phone_number}\n",
        encoding="utf-8",
    )
    settings = load_settings(env)
    assert settings.mongodb_uri == "mongodb://localhost:27017"
    assert settings.webhook_template == "https://noteify.org/ntext/whook/?{phone_number}"
    assert settings.poll_interval_seconds == 3, "polling is fixed, not configured"


def test_a_template_with_no_placeholder_warns_rather_than_failing(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("MONGODB_URI=mongodb://localhost:27017\n"
                   "WEBHOOK_URL=https://noteify.org/hook\n", encoding="utf-8")
    settings = load_settings(env)
    assert any("placeholder" in w for w in settings.warnings)


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------


def test_the_written_env_holds_exactly_the_two_keys(tmp_path: Path):
    text = env_text("mongodb://localhost:27017",
                    "https://noteify.org/ntext/whook/?{phone_number}")
    keys = [line.split("=", 1)[0] for line in text.splitlines()
            if line and not line.startswith("#")]
    assert keys == ["MONGODB_URI", "WEBHOOK_URL"]


def test_the_written_env_can_be_loaded_back(tmp_path: Path):
    """The setup screen and the loader must agree, or first run configures the
    application and the second launch rejects it."""
    env = tmp_path / ".env"
    write_env(env, "mongodb://localhost:27017",
              "https://noteify.org/ntext/whook/?{phone_number}")
    settings = load_settings(env)
    assert settings.mongodb_uri == "mongodb://localhost:27017"
    assert settings.warnings == ()


@pytest.mark.parametrize("uri, template, fragment", [
    ("", "https://x.test/?{phone_number}", "MongoDB connection string"),
    ("localhost:27017", "https://x.test/?{phone_number}", "mongodb://"),
    ("mongodb://localhost", "", "webhook URL"),
    ("mongodb://localhost", "noteify.org/?{phone_number}", "http"),
    ("mongodb://localhost", "https://x.test/hook", "{phone_number}"),
])
def test_setup_rejects_what_cannot_work(uri, template, fragment):
    problem = validate(uri, template)
    assert problem, f"expected {uri!r}/{template!r} to be rejected"
    assert fragment in problem


def test_setup_accepts_a_usable_pair():
    assert validate("mongodb://localhost:27017",
                    "https://noteify.org/ntext/whook/?{phone_number}") == ""


def test_setup_is_asked_for_only_when_there_is_nothing_usable(tmp_path: Path):
    missing = tmp_path / "absent.env"
    assert needs_setup(missing) is True

    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    assert needs_setup(empty) is True

    no_uri = tmp_path / "partial.env"
    no_uri.write_text("WEBHOOK_URL=https://x.test/?{phone_number}\n", encoding="utf-8")
    assert needs_setup(no_uri) is True

    complete = tmp_path / "good.env"
    write_env(complete, "mongodb://localhost:27017", "https://x.test/?{phone_number}")
    assert needs_setup(complete) is False, "a configured app must never ask again"


# ---------------------------------------------------------------------------
# Pending count
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


def _incoming(chat_id: str, text: str, status: str) -> StoredMessage:
    return StoredMessage(message_key=f"{chat_id}:{text}", chat_id=chat_id,
                         chat_name="Alice", sender="Alice", text=text,
                         direction="in", status=status)


def test_pending_counts_messages_still_in_flight(repo):
    cid = chat_id_for("Alice")
    repo.save_chat(ChatConfig(chat_id=cid, chat_name="Alice"))
    repo.save_message(_incoming(cid, "one", MessageStatus.PENDING))
    repo.save_message(_incoming(cid, "two", MessageStatus.AWAITING_SEND))

    assert repo.pending_counts().get(cid) == 2


def test_a_finished_message_stops_counting(repo):
    """The badge has to disappear on its own, or it means nothing."""
    cid = chat_id_for("Alice")
    repo.save_chat(ChatConfig(chat_id=cid, chat_name="Alice"))
    repo.save_message(_incoming(cid, "one", MessageStatus.REPLIED))
    repo.save_message(_incoming(cid, "two", MessageStatus.WEBHOOK_OK))
    repo.save_message(_incoming(cid, "three", MessageStatus.IGNORED))

    assert repo.pending_counts().get(cid) is None


def test_a_seeded_backlog_is_not_pending(repo):
    """Messages that existed before the chat was switched on are not work."""
    cid = chat_id_for("Alice")
    repo.save_chat(ChatConfig(chat_id=cid, chat_name="Alice"))
    repo.save_message(_incoming(cid, "old", MessageStatus.SEEDED))

    assert repo.pending_counts().get(cid) is None


# ---------------------------------------------------------------------------
# Backfilling a number onto history
# ---------------------------------------------------------------------------


def test_a_new_number_is_stamped_onto_messages_already_stored(repo):
    """A chat can run for days before anyone supplies its number.

    Without backfilling, its history splits into rows that carry the number and
    rows that do not — invisible until someone queries the collection and
    quietly gets half of it."""
    cid = chat_id_for("Alice")
    repo.save_chat(ChatConfig(chat_id=cid, chat_name="Alice"))
    repo.save_message(_incoming(cid, "before one", MessageStatus.REPLIED))
    repo.save_message(_incoming(cid, "before two", MessageStatus.REPLIED))

    filled = repo.backfill_phone_number(cid, "919423155555")

    assert filled == 2
    for message in repo.messages_for(cid):
        assert message.phone_number == "919423155555"


def test_backfilling_leaves_other_chats_alone(repo):
    alice, bob = chat_id_for("Alice"), chat_id_for("Bob")
    repo.save_chat(ChatConfig(chat_id=alice, chat_name="Alice"))
    repo.save_chat(ChatConfig(chat_id=bob, chat_name="Bob"))
    repo.save_message(_incoming(alice, "hers", MessageStatus.REPLIED))
    repo.save_message(_incoming(bob, "his", MessageStatus.REPLIED))

    repo.backfill_phone_number(alice, "919423155555")

    by_chat = {cid: repo.messages_for(cid)[0].phone_number
               for cid in (alice, bob)}
    assert by_chat[alice] == "919423155555"
    assert by_chat[bob] == ""


def test_backfilling_the_same_number_twice_changes_nothing(repo):
    cid = chat_id_for("Alice")
    repo.save_chat(ChatConfig(chat_id=cid, chat_name="Alice"))
    repo.save_message(_incoming(cid, "one", MessageStatus.REPLIED))

    assert repo.backfill_phone_number(cid, "919423155555") == 1
    assert repo.backfill_phone_number(cid, "919423155555") == 0


def test_clearing_a_number_clears_it_from_history_too(repo):
    """A number entered by mistake has to be removable everywhere, or the
    wrong one lives on in the messages after being fixed on the chat."""
    cid = chat_id_for("Alice")
    repo.save_chat(ChatConfig(chat_id=cid, chat_name="Alice"))
    repo.save_message(_incoming(cid, "one", MessageStatus.REPLIED))
    repo.backfill_phone_number(cid, "919999999999")

    assert repo.backfill_phone_number(cid, "") == 1
    assert all(m.phone_number == "" for m in repo.messages_for(cid))


# ---------------------------------------------------------------------------
# Addressing a chat by its number
# ---------------------------------------------------------------------------


def _chat(name: str, phone: str = "", external: str = "") -> ChatConfig:
    return ChatConfig(chat_id=chat_id_for(name), chat_name=name,
                      phone_number=phone, external_id=external)


def test_a_chat_can_be_addressed_by_its_stored_number():
    """The number is typed in by hand, so the API has to accept it.

    Before this the resolver only knew numbers it could derive from a chat's
    NAME — which is exactly the case that does not apply to a saved contact."""
    from wadam.api.resolver import resolve_chat

    chats = [_chat("Varshith", phone="919423155555"), _chat("Alice")]
    result = resolve_chat(chats, "919423155555")
    assert result.ok and result.chat.chat_name == "Varshith"
    assert result.matched_by == "phone_number"


def test_a_number_matches_however_it_is_punctuated():
    """"+91 94231 55555" and "919423155555" are the same contact; a caller
    should not have to know which spelling is stored."""
    from wadam.api.resolver import resolve_chat

    chats = [_chat("Varshith", phone="919423155555")]
    for spelling in ("+91 94231 55555", "+919423155555", "91-9423-155555"):
        result = resolve_chat(chats, spelling)
        assert result.ok, f"{spelling!r} should resolve"
        assert result.chat.chat_name == "Varshith"


def test_the_last_four_digits_come_from_the_stored_number_too():
    from wadam.api.resolver import resolve_chat

    chats = [_chat("Varshith", phone="919423155555"), _chat("Alice")]
    result = resolve_chat(chats, "5555")
    assert result.ok and result.chat.chat_name == "Varshith"
    assert result.matched_by == "contact_last4"


def test_two_chats_sharing_the_last_four_digits_are_refused_not_guessed():
    from wadam.api.resolver import resolve_chat

    chats = [_chat("Varshith", phone="919423155555"),
             _chat("Nagen", phone="447700155555")]
    result = resolve_chat(chats, "5555")
    assert not result.ok
    assert result.ambiguous
    assert result.candidates == ("Nagen", "Varshith")


def test_a_full_number_still_resolves_when_the_last_four_are_ambiguous():
    """Ambiguity is about the abbreviation, not the number itself."""
    from wadam.api.resolver import resolve_chat

    chats = [_chat("Varshith", phone="919423155555"),
             _chat("Nagen", phone="447700155555")]
    result = resolve_chat(chats, "919423155555")
    assert result.ok and result.chat.chat_name == "Varshith"


def test_a_chat_name_still_works_when_there_is_no_number():
    from wadam.api.resolver import resolve_chat

    result = resolve_chat([_chat("Novus Tech Group")], "Novus Tech Group")
    assert result.ok and result.matched_by == "chat_name"
