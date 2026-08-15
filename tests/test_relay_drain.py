"""Two things a poll loop has to do that this one wasn't doing.

**Draining.** A webhook holds a queue and hands over one message per request.
A tick that fetched exactly once delivered one message per interval, so four
messages posted at the same moment took four intervals to arrive — an
application that looks slow rather than polite. winSpark's fetch-webhook relay
drains: it asks again immediately after each accepted message, bounded, and
stops on blank/error/duplicate. This ports that.

**Re-reading configuration.** The chats were loaded from MongoDB once, at
startup. Editing a webhook or an automation flag in the database did nothing
until a restart, and the next `save_chat` wrote the stale value back over the
edit — so nothing except the running window could configure the application.
Found by pointing a chat's webhook at a stand-in and watching the app poll the
old URL for 150 seconds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wadam import constants
from wadam.config import Settings
from wadam.domain.models import ChatConfig, chat_id_for
from wadam.engine.engine import AutomationEngine
from wadam.engine.webhook import RelayMessage
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository

from tests.test_storage import FakeMongo


class Poll:
    """What RelayService.poll returns."""

    def __init__(self, messages=(), ok=True, error="", status="200 OK"):
        self.chat_id = ""
        self.messages = tuple(messages)
        self.ok = ok
        self.error = error
        self.status = status


class FakeRelay:
    """An endpoint with a queue, handing over one message per request — which
    is the behaviour that makes draining necessary."""

    def __init__(self, queued: list[str], refuse: set[str] | None = None):
        self.queued = list(queued)
        self.refuse = refuse or set()
        self.polls = 0
        self.accepted: list[str] = []
        self.skipped: list[str] = []
        self.recorded = 0

    async def poll(self, _chat):
        self.polls += 1
        if not self.queued:
            return Poll()
        return Poll([RelayMessage(text=self.queued.pop(0))])

    def should_send(self, _chat, message):
        if message.text in self.refuse:
            return False, "duplicate"
        return True, ""

    def note_skipped(self, _chat, message, _reason):
        self.skipped.append(message.text)

    async def record_poll(self, _chat, _poll):
        self.recorded += 1

    def is_eligible(self, chat):
        return bool(chat.automation_enabled and chat.webhook_url)


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


def chat_on(repo, name="Alice", url="https://x.test/hook") -> ChatConfig:
    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name, seeded=True,
                      automation_enabled=True, webhook_url=url)
    repo.save_chat(chat)
    return chat


def queued_texts(instance) -> list[str]:
    out = []
    while not instance._queue.empty():
        job = instance._queue.get_nowait()
        if job.relay is not None:
            out.append(job.relay.text)
    return out


# ---------------------------------------------------------------------------
# Draining
# ---------------------------------------------------------------------------


def test_a_burst_clears_in_one_tick_not_one_per_interval(engine):
    """The whole point. Four messages posted at the same moment used to take
    four polling intervals to arrive."""
    instance, repo = engine
    chat = chat_on(repo)
    relay = FakeRelay(["one", "two", "three", "four"])
    instance._relay = relay

    asyncio.run(instance._drain_relay(chat, asyncio.run(relay.poll(chat))))

    assert queued_texts(instance) == ["one", "two", "three", "four"]


def test_an_idle_endpoint_is_asked_exactly_once(engine):
    """Draining must not turn a quiet endpoint into a hammered one."""
    instance, repo = engine
    chat = chat_on(repo)
    relay = FakeRelay([])
    instance._relay = relay

    asyncio.run(instance._drain_relay(chat, asyncio.run(relay.poll(chat))))

    assert relay.polls == 1
    assert queued_texts(instance) == []


def test_a_blank_stops_the_drain(engine):
    instance, repo = engine
    chat = chat_on(repo)
    relay = FakeRelay(["one"])
    instance._relay = relay

    asyncio.run(instance._drain_relay(chat, asyncio.run(relay.poll(chat))))

    # one served, one blank that ended it — and no further asking
    assert relay.polls == 2
    assert queued_texts(instance) == ["one"]


def test_a_duplicate_stops_the_drain(engine):
    """An endpoint that keeps returning the same thing is asked twice, not
    MAX_RELAY_DRAIN times."""
    instance, repo = engine
    chat = chat_on(repo)
    relay = FakeRelay(["same", "same", "same", "same"], refuse={"same"})
    instance._relay = relay

    asyncio.run(instance._drain_relay(chat, asyncio.run(relay.poll(chat))))

    assert relay.polls == 1, "the refusal ended it without another request"
    assert queued_texts(instance) == []
    assert relay.skipped == ["same"]


def test_an_error_stops_the_drain(engine):
    instance, repo = engine
    chat = chat_on(repo)
    relay = FakeRelay(["one"])
    instance._relay = relay

    asyncio.run(instance._drain_relay(chat, Poll(ok=False, error="HTTP 500")))

    assert relay.polls == 0
    assert queued_texts(instance) == []


def test_an_endless_endpoint_cannot_hold_the_tick_open(engine):
    """A source that returns something new on every single request is bounded,
    so one chat can never starve the others."""
    instance, repo = engine
    chat = chat_on(repo)
    relay = FakeRelay([f"m{i}" for i in range(100)])
    instance._relay = relay

    asyncio.run(instance._drain_relay(chat, asyncio.run(relay.poll(chat))))

    assert len(queued_texts(instance)) == constants.MAX_RELAY_DRAIN
    assert relay.polls == constants.MAX_RELAY_DRAIN, (
        "exactly the bound, with no extra fetch whose answer is discarded — "
        "against a dequeuing endpoint that would destroy a message, because it "
        "has already removed it from its queue to hand it over"
    )


# ---------------------------------------------------------------------------
# Re-reading configuration
# ---------------------------------------------------------------------------


def test_a_webhook_edited_in_the_database_is_picked_up(engine):
    """The bug: the app polled the old URL for 150 seconds because chats were
    read once, at startup."""
    instance, repo = engine
    chat = chat_on(repo, url="https://old.test/hook")
    repo._mongo.chat_configs.update_one(
        {"chat_id": chat.chat_id},
        {"$set": {"webhook_url": "https://new.test/hook",
                  "webhook_override": "https://new.test/hook"}})

    assert repo.get_chat(chat.chat_id).webhook_url == "https://old.test/hook"
    changed = repo.reload_chat_config()

    assert changed == [chat.chat_id]
    assert repo.get_chat(chat.chat_id).webhook_url == "https://new.test/hook"


def test_automation_switched_off_elsewhere_is_obeyed(engine):
    instance, repo = engine
    chat = chat_on(repo)
    repo._mongo.chat_configs.update_one({"chat_id": chat.chat_id},
                                        {"$set": {"automation_enabled": False}})

    repo.reload_chat_config()

    assert repo.get_chat(chat.chat_id).automation_enabled is False


def test_runtime_state_is_not_clobbered_by_the_reload(engine):
    """Only configuration comes back. Copying `seeded` or the last_* fields over
    a live object would undo whatever happened since the last save — and
    re-seeding a chat means answering its backlog."""
    instance, repo = engine
    chat = chat_on(repo)
    repo._mongo.chat_configs.update_one(
        {"chat_id": chat.chat_id},
        {"$set": {"seeded": False, "last_relay_text": "stale", "messages_stored": 0}})
    live = repo.get_chat(chat.chat_id)
    live.last_relay_text = "current"
    live.messages_stored = 42

    repo.reload_chat_config()

    after = repo.get_chat(chat.chat_id)
    assert after.seeded is True, "re-seeding would answer the whole backlog"
    assert after.last_relay_text == "current"
    assert after.messages_stored == 42


def test_a_quiet_reload_reports_nothing(engine):
    instance, repo = engine
    chat_on(repo)
    assert repo.reload_chat_config() == []


def test_the_reload_does_not_invent_chats(engine):
    """Discovery owns creation. A row appearing in MongoDB from somewhere else
    must not become a chat this application polls."""
    instance, repo = engine
    chat_on(repo)
    repo._mongo.chat_configs.insert_one(
        {"chat_id": "elsewhere", "chat_name": "Injected",
         "automation_enabled": True, "webhook_url": "https://x.test/"})

    repo.reload_chat_config()

    assert repo.get_chat("elsewhere") is None
    assert [c.chat_name for c in repo.list_chats()] == ["Alice"]


def test_the_reload_is_not_run_every_cycle(engine):
    """It used to be, and had to be: every save wrote the whole chat, so a
    routine write stamped stale config over an external edit and only a reload
    faster than the writes could win.

    `Repository._writable` fixed that at the source, so there is no race left to
    win — and a read every three seconds against a paid cluster is a standing
    charge for a value that changes when a person changes it. Measured on an
    idle five-chat install: this was half of 1.7 million operations a month."""
    instance, repo = engine
    chat_on(repo)
    calls = []
    repo.reload_chat_config = lambda: calls.append(1) or []

    for _ in range(3):
        asyncio.run(instance._reload_chat_config())

    assert len(calls) == 1
    assert constants.CHAT_CONFIG_RELOAD_INTERVAL > constants.POLL_INTERVAL_SECONDS


def test_an_external_edit_survives_the_save_that_follows_it(engine):
    """The failure the interval caused, as a test. Reload, then let the app
    write the chat back: the edit must be what gets written, not the value the
    process was holding."""
    instance, repo = engine
    chat = chat_on(repo, url="https://old.test/hook")
    repo._mongo.chat_configs.update_one(
        {"chat_id": chat.chat_id}, {"$set": {"webhook_url": "https://new.test/hook"}})

    asyncio.run(instance._reload_chat_config())
    repo.save_chat(repo.get_chat(chat.chat_id))

    stored = repo._mongo.chat_configs.find_one({"chat_id": chat.chat_id})
    assert stored["webhook_url"] == "https://new.test/hook"


def test_a_routine_save_does_not_stamp_config_over_an_external_edit(engine):
    """The failure that survived reloading every cycle.

    Every save wrote the whole chat, so a relay poll recording its status
    carried a full copy of the configuration and stamped it over the database.
    Reloading faster only narrows that window; not writing back a field you did
    not change closes it."""
    instance, repo = engine
    chat = chat_on(repo, url="https://old.test/hook")

    # somebody edits the database
    repo._mongo.chat_configs.update_one(
        {"chat_id": chat.chat_id}, {"$set": {"webhook_url": "https://edited.test/hook"}})
    # and the app does something routine that saves the chat
    live = repo.get_chat(chat.chat_id)
    live.last_relay_status = "200 OK"
    repo.save_chat(live)

    stored = repo._mongo.chat_configs.find_one({"chat_id": chat.chat_id})
    assert stored["webhook_url"] == "https://edited.test/hook", "the edit survives"
    assert stored["last_relay_status"] == "200 OK", "and the runtime write landed"


def test_a_genuine_local_change_is_still_written(engine):
    """The other half. Leaving config alone must not mean the UI cannot change
    it — a field that differs from what this process last read is a real edit."""
    instance, repo = engine
    chat = chat_on(repo, url="https://old.test/hook")

    live = repo.get_chat(chat.chat_id)
    live.webhook_url = "https://set-in-the-app.test/hook"
    live.automation_enabled = False
    repo.save_chat(live)

    stored = repo._mongo.chat_configs.find_one({"chat_id": chat.chat_id})
    assert stored["webhook_url"] == "https://set-in-the-app.test/hook"
    assert stored["automation_enabled"] is False


def test_the_two_together_survive_a_reload_cycle(engine):
    """Edit outside, reload, routine save, reload again — the value has to be
    the edit at every step, not oscillate."""
    instance, repo = engine
    chat = chat_on(repo, url="https://old.test/hook")
    repo._mongo.chat_configs.update_one(
        {"chat_id": chat.chat_id}, {"$set": {"webhook_url": "https://edited.test/hook"}})

    for _ in range(3):
        repo.reload_chat_config()
        repo.save_chat(repo.get_chat(chat.chat_id))

    assert repo.get_chat(chat.chat_id).webhook_url == "https://edited.test/hook"
    stored = repo._mongo.chat_configs.find_one({"chat_id": chat.chat_id})
    assert stored["webhook_url"] == "https://edited.test/hook"


def test_the_last_poll_stamp_is_not_written_every_cycle(engine):
    """"When did we last look at this chat" is telemetry nobody decides
    anything from, and it was an UPDATE on a three-second timer — measured as
    half of the entire idle cost. Kept live in memory and in the JSON mirror;
    written to MongoDB rarely."""
    instance, repo = engine
    chat = chat_on(repo)
    from wadam.domain.models import utcnow

    before = repo._mongo.chat_configs.updates if hasattr(
        repo._mongo.chat_configs, "updates") else None
    calls = []
    original = repo._mongo.chat_configs.update_many
    repo._mongo.chat_configs.update_many = lambda *a, **k: calls.append(1) or original(*a, **k)

    for _ in range(5):
        repo.touch_last_poll([chat.chat_id], utcnow())

    assert len(calls) == 1, "the first write lands; the rest are held in memory"
    assert repo.get_chat(chat.chat_id).last_poll_utc is not None, (
        "and the value on screen is as live as it ever was"
    )
