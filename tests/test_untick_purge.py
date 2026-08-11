"""Unticking a chat deletes what it stored.

The checkbox now means two things, and they are not symmetrical. Ticking starts
watching a chat and is instant and silent. Unticking stops watching it AND
destroys everything it put in `wa_events` — messages, webhook calls and queued
sends — because leaving them behind would keep a record for a chat nobody is
watching any more.

Deletion is irreversible, so what it must and must not reach is pinned here
rather than left to a reading of the code:

    the chat's own records          deleted
    every other chat's records      untouched
    the ChatConfig row itself       KEPT
    the baseline (`seeded`)         cleared

Keeping the row is the one that is easy to get wrong. A discovered chat now
arrives with automation ON, so deleting the config would have the chat
rediscovered on the next poll and switched straight back on — unticking a box
would turn it into a tick.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import (
    ChatConfig,
    MessageStatus,
    OutgoingMessage,
    OutgoingStatus,
    StoredMessage,
    WebhookRecord,
    chat_id_for,
    message_key_for,
)
from wadam.engine.discovery import ChatDiscovery
from wadam.engine.engine import AutomationEngine
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.row_parser import parse_chat_row
from wadam.whatsapp.reader import ChatRow

from tests.test_storage import FakeMongo


@pytest.fixture()
def engine(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="wa_events",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    instance = AutomationEngine(settings, repository, lambda _s: None)
    yield instance, repository
    instance._sta.dispose()
    repository.stop()


def populate(repo: Repository, name: str, *, messages: int = 3,
             webhooks: int = 2, outgoing: int = 1) -> ChatConfig:
    """A chat with a history: stored messages, webhook calls, a queued send."""
    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name,
                      automation_enabled=True, seeded=True,
                      webhook_url=f"https://x.test/?{name}")
    repo.save_chat(chat)
    for index in range(messages):
        repo.save_message(StoredMessage(
            message_key=message_key_for(chat.chat_id, name, f"m{index}", "12:00 pm", "in"),
            chat_id=chat.chat_id, chat_name=name, sender=name,
            text=f"m{index}", direction="in", status=MessageStatus.PENDING))
    for index in range(webhooks):
        repo.save_webhook(WebhookRecord(
            chat_id=chat.chat_id, chat_name=name, url=chat.webhook_url,
            request={"text": f"w{index}"}, status_code=200, ok=True))
    for index in range(outgoing):
        repo.enqueue_outgoing(OutgoingMessage(
            chat_id=chat.chat_id, chat_name=name, text=f"o{index}", origin="api"))
    return chat


def untick(instance: AutomationEngine, chat: ChatConfig) -> None:
    asyncio.run(instance.set_chat_automation(chat.chat_id, False))


# ---------------------------------------------------------------------------
# What is destroyed
# ---------------------------------------------------------------------------


def test_unticking_deletes_the_chats_messages(engine):
    instance, repo = engine
    chat = populate(repo, "Alice")
    assert repo.message_count(chat.chat_id) == 3

    untick(instance, chat)

    assert repo.message_count(chat.chat_id) == 0
    assert repo._mongo.messages.count_documents({"chat_id": chat.chat_id}) == 0


def test_unticking_deletes_the_chats_webhook_calls_and_queued_sends(engine):
    instance, repo = engine
    chat = populate(repo, "Alice")

    untick(instance, chat)

    counts = repo.chat_record_counts(chat.chat_id)
    assert counts == {"messages": 0, "webhooks": 0, "outgoing": 0}


def test_a_queued_send_is_cancelled_before_it_is_deleted(engine):
    """Not silently dropped. A send that was promised and then discarded should
    end in a state that says so, in case anything is still watching it."""
    instance, repo = engine
    chat = populate(repo, "Alice", outgoing=2)
    queued = [m for m in repo.all_outgoing() if m.chat_id == chat.chat_id]
    assert len(queued) == 2

    untick(instance, chat)

    assert all(m.status == OutgoingStatus.CANCELLED for m in queued)
    assert all("deleted" in m.error for m in queued)


def test_the_badge_goes_to_zero(engine):
    """The pending count is derived from exactly the records that were deleted,
    so a purged chat cannot be left showing work it no longer has."""
    instance, repo = engine
    chat = populate(repo, "Alice")
    assert repo.pending_counts().get(chat.chat_id, 0) > 0

    untick(instance, chat)

    assert repo.pending_counts().get(chat.chat_id, 0) == 0


# ---------------------------------------------------------------------------
# What is NOT destroyed
# ---------------------------------------------------------------------------


def test_another_chats_records_are_untouched(engine):
    instance, repo = engine
    alice = populate(repo, "Alice")
    bob = populate(repo, "Bob", messages=4, webhooks=1, outgoing=2)

    untick(instance, alice)

    assert repo.chat_record_counts(bob.chat_id) == {
        "messages": 4, "webhooks": 1, "outgoing": 2}


def test_the_chat_itself_survives_and_is_simply_off(engine):
    instance, repo = engine
    chat = populate(repo, "Alice")

    untick(instance, chat)

    after = repo.get_chat(chat.chat_id)
    assert after is not None, "the row stays in the list — only its history goes"
    assert after.automation_enabled is False
    assert after.chat_name == "Alice"


def test_the_configured_number_and_webhook_survive(engine):
    """Discovering a saved contact's number costs a panel open and about eight
    seconds. Unticking a chat is not a reason to make it happen again."""
    instance, repo = engine
    chat = populate(repo, "Alice")
    chat.phone_number = "917981149423"
    chat.external_id = "9423"
    chat.webhook_override = "https://custom.test/hook"
    repo.save_chat(chat)

    untick(instance, chat)

    after = repo.get_chat(chat.chat_id)
    assert after.phone_number == "917981149423"
    assert after.external_id == "9423"
    assert after.webhook_override == "https://custom.test/hook"


def test_the_dedup_keys_of_other_chats_still_work(engine):
    """The in-memory key set is one set for every chat. Rebuilding it wrongly
    would let another chat's message be stored a second time."""
    instance, repo = engine
    alice = populate(repo, "Alice")
    bob = populate(repo, "Bob")
    bob_key = message_key_for(bob.chat_id, "Bob", "m0", "12:00 pm", "in")

    untick(instance, alice)

    assert repo.has_message(bob_key)
    assert repo.save_message(StoredMessage(
        message_key=bob_key, chat_id=bob.chat_id, chat_name="Bob",
        sender="Bob", text="m0", direction="in")) is False


# ---------------------------------------------------------------------------
# Turning it back on
# ---------------------------------------------------------------------------


def test_unticking_clears_the_baseline(engine):
    instance, repo = engine
    chat = populate(repo, "Alice")
    assert chat.seeded is True

    untick(instance, chat)

    assert repo.get_chat(chat.chat_id).seeded is False


def test_re_ticking_re_baselines_instead_of_answering_the_backlog(engine):
    """The consequence of the line above, and the reason it is there.

    With the records gone but `seeded` still true, ticking the box again would
    read the conversation, find nothing on record, and treat every message on
    screen as newly arrived — webhooking a backlog that accumulated while the
    automation was deliberately off."""
    instance, repo = engine
    chat = populate(repo, "Alice")
    untick(instance, chat)

    asyncio.run(instance.set_chat_automation(chat.chat_id, True))

    after = repo.get_chat(chat.chat_id)
    assert after.automation_enabled is True
    assert after.seeded is False, "the next read must be a baseline, not a backlog"


def test_ticking_a_chat_on_never_deletes_anything(engine):
    instance, repo = engine
    chat = populate(repo, "Alice")
    chat.automation_enabled = False
    repo.save_chat(chat)

    asyncio.run(instance.set_chat_automation(chat.chat_id, True))

    assert repo.chat_record_counts(chat.chat_id) == {
        "messages": 3, "webhooks": 2, "outgoing": 1}


def test_a_purged_chat_is_not_switched_back_on_by_discovery(engine, tmp_path: Path):
    """The interaction between the two changes in this release, and the reason
    the ChatConfig is kept rather than deleted. A new chat arrives with
    automation ON; if unticking removed the row, the next sidebar reading would
    discover the chat again and turn it on."""
    instance, repo = engine
    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    discovery = ChatDiscovery(repo, settings)
    row = ChatRow(**parse_chat_row("Alice 12:00 pm hi"))

    fresh = discovery.sync([row]).new[0]
    assert fresh.automation_enabled is True, "a newly discovered chat is watched"

    untick(instance, fresh)
    for _ in range(3):
        discovery.sync([row])

    after = repo.get_chat(fresh.chat_id)
    assert after.automation_enabled is False, "unticking must stay unticked"


# ---------------------------------------------------------------------------
# Counting before deleting
# ---------------------------------------------------------------------------


def test_the_counts_are_readable_without_deleting_anything(engine):
    """The UI asks for these to name a number in its confirmation, before the
    user has agreed to anything."""
    instance, repo = engine
    chat = populate(repo, "Alice", messages=5, webhooks=3, outgoing=2)

    counts = repo.chat_record_counts(chat.chat_id)

    assert counts == {"messages": 5, "webhooks": 3, "outgoing": 2}
    assert repo.chat_record_counts(chat.chat_id) == counts, "reading changed nothing"


def test_the_purge_reports_what_it_destroyed(engine):
    instance, repo = engine
    chat = populate(repo, "Alice", messages=5, webhooks=3, outgoing=2)

    destroyed = repo.purge_chat_records(chat.chat_id)

    assert destroyed == {"messages": 5, "webhooks": 3, "outgoing": 2}


def test_an_unknown_chat_is_a_no_op(engine):
    instance, repo = engine
    populate(repo, "Alice")

    asyncio.run(instance.set_chat_automation("nonexistent", False))

    assert repo.chat_record_counts(chat_id_for("Alice")) == {
        "messages": 3, "webhooks": 2, "outgoing": 1}
