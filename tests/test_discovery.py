"""Automatic chat discovery: what a newly seen chat is allowed to arrive as,
and what counts as "this chat changed"."""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import chat_id_for
from wadam.engine.discovery import ChatDiscovery, row_signature
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.reader import ChatRow
from wadam.whatsapp.row_parser import parse_chat_row

from tests.test_storage import FakeMongo


def make_repo(tmp_path: Path, default_webhook: str = "") -> tuple[Repository, Settings]:
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0,
                        default_webhook=default_webhook)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    return repository, settings


@pytest.fixture()
def discovery(tmp_path: Path):
    repository, settings = make_repo(tmp_path)
    yield ChatDiscovery(repository, settings), repository
    repository.stop()


def row(name: str, message: str = "hi", unread: int = 0, timestamp: str = "12:00 pm") -> ChatRow:
    """Build a row the way the reader does — from a flattened accessible Name
    through the real parser — so these tests exercise the same fields the
    engine actually receives rather than a hand-filled dataclass."""
    prefix = f"{unread} unread messages " if unread else ""
    return ChatRow(**parse_chat_row(f"{prefix}{name} {timestamp} {message}"))


def test_a_new_chat_arrives_inert(discovery):
    engine_discovery, _repo = discovery
    result = engine_discovery.sync([row("Alice")])

    assert len(result.new) == 1
    chat = result.new[0]
    assert chat.automation_enabled is False, "discovery must never enable automation"
    assert chat.webhook_url == ""
    assert chat.seeded is False, "the backlog must not count as processed yet"


def test_default_webhook_is_applied_to_new_chats_only(tmp_path: Path):
    repository, settings = make_repo(tmp_path, default_webhook="https://x.test/hook")
    engine_discovery = ChatDiscovery(repository, settings)
    chat = engine_discovery.sync([row("Alice")]).new[0]
    assert chat.webhook_url == "https://x.test/hook"
    # Still off: a webhook is a destination, not permission to use it.
    assert chat.automation_enabled is False
    repository.stop()


def test_an_unchanged_row_is_not_reported_as_changed(discovery):
    engine_discovery, _repo = discovery
    engine_discovery.sync([row("Alice")])
    second = engine_discovery.sync([row("Alice")])
    assert second.changed == []
    assert len(second.seen) == 1


def test_a_new_message_makes_the_row_change(discovery):
    engine_discovery, _repo = discovery
    engine_discovery.sync([row("Alice", message="hi")])
    second = engine_discovery.sync([row("Alice", message="are you there?", unread=1)])
    assert [c.chat_name for c in second.changed] == ["Alice"]
    assert second.changed[0].unread_count == 1


def test_navigation_titles_are_not_chats(discovery):
    engine_discovery, _repo = discovery
    result = engine_discovery.sync([row("Archived"), row("Starred messages"), row("Alice")])
    assert [c.chat_name for c in result.seen] == ["Alice"]


def test_a_users_settings_survive_rediscovery(discovery):
    engine_discovery, repository = discovery
    engine_discovery.sync([row("Alice")])
    chat = repository.get_chat(chat_id_for("Alice"))
    chat.automation_enabled = True
    chat.webhook_url = "https://x.test/alice"
    chat.seeded = True
    repository.save_chat(chat)

    engine_discovery.sync([row("Alice", message="new message", unread=2)])
    after = repository.get_chat(chat_id_for("Alice"))
    assert after.automation_enabled is True
    assert after.webhook_url == "https://x.test/alice"
    assert after.seeded is True
    assert after.unread_count == 2


def test_group_hint_is_sticky(discovery):
    engine_discovery, repository = discovery
    engine_discovery.sync([row("Team", message="Chaitu: hello")])
    assert repository.get_chat(chat_id_for("Team")).is_group is True
    # Our own message next — the row no longer looks like a group, but it is one.
    engine_discovery.sync([row("Team", message="You: ok")])
    assert repository.get_chat(chat_id_for("Team")).is_group is True


def test_row_signature_covers_everything_the_sidebar_shows():
    base = row("Alice")
    assert row_signature(base) == row_signature(row("Alice"))
    assert row_signature(base) != row_signature(row("Alice", unread=1))
    assert row_signature(base) != row_signature(row("Alice", message="different"))
    assert row_signature(base) != row_signature(row("Alice", timestamp="12:01 pm"))


def test_chat_id_is_stable_across_casing_and_spacing():
    assert chat_id_for("Alice Smith") == chat_id_for("  alice   smith ")
    assert chat_id_for("Alice") != chat_id_for("Alicia")
