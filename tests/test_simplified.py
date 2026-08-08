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


def test_a_chat_without_a_number_gets_no_url():
    """The rule the whole module exists for.

    Substituting an empty string yields a valid-looking URL pointing at nobody;
    messages would post to it forever and look fine."""
    assert webhook_url_for("https://noteify.org/ntext/whook/?{phone_number}", "") == ""


def test_an_override_wins_over_the_template():
    assert webhook_url_for("https://noteify.org/?{phone_number}", "15551234567",
                           override="https://elsewhere.test/hook") == \
        "https://elsewhere.test/hook"


def test_a_template_without_a_placeholder_is_used_as_is():
    """Odd but explicit — the same URL for every chat. Warned about at startup,
    not silently turned into something else."""
    assert webhook_url_for("https://one.test/hook", "15551234567") == "https://one.test/hook"


def test_a_missing_number_is_explained_in_words():
    assert "phone number" in describe_missing("").lower()
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
