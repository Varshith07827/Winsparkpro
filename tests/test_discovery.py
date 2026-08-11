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


def test_a_new_chat_arrives_watched(discovery):
    """A tool for watching chats that watches none of them until every box is
    ticked is a tool nobody has switched on yet."""
    engine_discovery, _repo = discovery
    result = engine_discovery.sync([row("Alice")])

    assert len(result.new) == 1
    chat = result.new[0]
    assert chat.automation_enabled is True


def test_a_new_chat_is_watched_but_not_yet_baselined(discovery):
    """The pair that makes automation-on-by-default safe.

    `seeded` false means the first read stores the whole visible backlog as
    SEEDED and automates none of it. Enable the first without the second and
    installing this would webhook every conversation already on screen."""
    engine_discovery, _repo = discovery
    chat = engine_discovery.sync([row("Alice")]).new[0]

    assert chat.automation_enabled is True
    assert chat.seeded is False, "the backlog must not count as processed yet"


def test_a_new_chat_stores_no_webhook_of_its_own(tmp_path: Path):
    """The global template is the source of truth.

    Chats used to each carry a copy of the default webhook, which then drifted
    out of step the moment the default changed. Now the URL is derived from the
    template and the chat's number every time it is needed."""
    repository, settings = make_repo(tmp_path, default_webhook="https://x.test/hook")
    engine_discovery = ChatDiscovery(repository, settings)
    chat = engine_discovery.sync([row("Alice")]).new[0]
    assert chat.webhook_override == "", "a chat only stores a URL when overriding"
    repository.stop()


def test_a_chat_named_by_its_number_resolves_a_phone_number(discovery):
    engine_discovery, _repo = discovery
    chat = engine_discovery.sync([row("+91 94231 55555")]).new[0]
    assert chat.phone_number == "919423155555"


def test_a_saved_contact_gets_no_invented_number(discovery):
    """WhatsApp shows a saved contact by name and never exposes the number.

    Storing anything here would build a webhook URL pointing at the wrong
    person, so it stays empty until it can genuinely be resolved."""
    engine_discovery, _repo = discovery
    chat = engine_discovery.sync([row("Alice")]).new[0]
    assert chat.phone_number == ""


def test_a_resolved_number_is_never_overwritten_by_a_later_scan(discovery):
    engine_discovery, repo = discovery
    chat = engine_discovery.sync([row("+91 94231 55555")]).new[0]
    chat.phone_number = "919999999999"      # corrected by hand
    repo.save_chat(chat)
    engine_discovery.sync([row("+91 94231 55555", message="new")])
    assert repo.get_chat(chat.chat_id).phone_number == "919999999999"


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
    # A deliberate per-chat destination goes in the OVERRIDE. `webhook_url` is
    # derived from the global template and is rebuilt on every pass.
    chat.webhook_override = "https://x.test/alice"
    chat.seeded = True
    repository.save_chat(chat)

    engine_discovery.sync([row("Alice", message="new message", unread=2)])
    after = repository.get_chat(chat_id_for("Alice"))
    assert after.automation_enabled is True
    assert after.webhook_url == "https://x.test/alice"
    assert after.seeded is True
    assert after.unread_count == 2


def test_changing_the_template_updates_every_chat(discovery_factory=None):
    """The template is the source of truth, so editing it must reach chats that
    have had no new message since."""
    import dataclasses
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as tmp:
        repository, settings = make_repo(_P(tmp))
        one = dataclasses.replace(settings,
                                  webhook_template="https://one.test/?{phone_number}")
        chat = ChatDiscovery(repository, one).sync([row("+91 94231 55555")]).new[0]
        assert chat.webhook_url == "https://one.test/?919423155555"

        two = dataclasses.replace(settings,
                                  webhook_template="https://two.test/?{phone_number}")
        ChatDiscovery(repository, two).sync([row("+91 94231 55555")])
        assert repository.get_chat(chat.chat_id).webhook_url ==             "https://two.test/?919423155555"
        repository.stop()


def test_a_chat_without_a_number_is_addressed_by_name(discovery):
    """A saved contact never exposes a number, so it is addressed by name until
    somebody supplies one. Waiting instead would mean it never forwards."""
    engine_discovery, _repo = discovery
    chat = engine_discovery.sync([row("Alice")]).new[0]
    assert chat.phone_number == ""
    assert chat.webhook_url.endswith("?Alice")


def test_an_override_beats_the_template(discovery):
    engine_discovery, repository = discovery
    chat = engine_discovery.sync([row("+91 94231 55555")]).new[0]
    chat.webhook_override = "https://elsewhere.test/hook"
    repository.save_chat(chat)
    engine_discovery.sync([row("+91 94231 55555", message="new")])
    assert repository.get_chat(chat.chat_id).webhook_url == "https://elsewhere.test/hook"


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
