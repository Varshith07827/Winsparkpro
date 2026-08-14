"""A query left in WhatsApp's search box used to stop the application dead.

The chain, which is why this is worth its own file:

    a search is active
      -> WhatsApp HIDES the recents grid entirely (`read_chat_rows_sync`)
      -> the poll reads the SEARCH RESULTS as if they were the chat list
      -> no real chat registers as changed, so no scan job is queued
      -> and the outgoing queue is drained *after each job*
      -> so nothing is ever sent again, until someone clears the box by hand

Three separate defects, fixed together because any one of them alone leaves the
application stoppable:

  1. `open_chat_sync` typed into the search box and had six ways out of that
     block, none of which cleared up. The success path relied on WhatsApp
     clearing the box itself.
  2. The poll reconciled the stored chat list against a filtered view without
     knowing it was filtered.
  3. Delivery only happened as a side effect of some other job running.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, OutgoingMessage, OutgoingStatus, chat_id_for
from wadam.engine.engine import AutomationEngine
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp import sender as S
from wadam.whatsapp.reader import SidebarReading

from tests.test_engine_integration import FakeReader, FakeSender, NoopPipeline, row
from tests.test_storage import FakeMongo


@pytest.fixture()
def engine(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0,
                        webhook_template="https://x.test/?{phone_number}")
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    instance = AutomationEngine(settings, repository, lambda _s: None)
    reader = FakeReader()
    instance._reader = reader
    instance._sender = FakeSender(reader)
    instance._pipeline = NoopPipeline()
    instance._hwnd = 1001
    yield instance, reader, repository
    instance._sta.dispose()
    repository.stop()


def kinds(instance) -> list[str]:
    out = []
    while not instance._queue.empty():
        out.append(instance._queue.get_nowait().kind)
    return out


# ---------------------------------------------------------------------------
# 1. The search is always cleaned up by whoever typed it
# ---------------------------------------------------------------------------


def test_every_exit_from_the_search_fallback_clears_up():
    """Read from the source, because the failure is a path NOT taken: six
    returns, and a leak on any one of them stops the application."""
    source = inspect.getsource(S.open_chat_sync)
    fallback = source[source.index("search_and_read_rows_sync"):]
    assert "finally:" in fallback, (
        "the search box must be emptied however the open ends, not only when "
        "it succeeds"
    )
    assert "clear_search_sync" in fallback


def test_the_success_path_does_not_rely_on_whatsapp_tidying_up():
    """The old comment said 'opening a chat clears the search by itself'. That
    is a bet on someone else's UI, and losing it stops all sending."""
    source = inspect.getsource(S.open_chat_sync)
    finally_block = source[source.index("finally:"):]
    assert "clear_search_sync" in finally_block


def test_reading_the_query_presses_nothing():
    """The poll needs to know a search is active, and the poll is not allowed
    to type. Reading a ValuePattern is not input."""
    source = inspect.getsource(S.search_query_sync)
    for key in ("SendKeys", "{Esc}", "{Ctrl}a", "{Delete}", "Click"):
        assert key not in source, f"{key} would make a passive read interactive"


# ---------------------------------------------------------------------------
# 2. A filtered sidebar is not the chat list
# ---------------------------------------------------------------------------


def test_a_filtered_sidebar_is_not_reconciled_against_the_chat_list(engine):
    """The rows behind a search are an arbitrary subset. Treating them as the
    whole list means every other chat looks like it went quiet."""
    instance, reader, repo = engine
    reader.rows = [row("Alice"), row("Bob")]
    asyncio.run(instance._cycle())
    assert len(repo.list_chats()) == 2

    reader.searching = True
    reader.rows = [row("Alice")]           # only the search result survives
    asyncio.run(instance._cycle())

    assert {c.chat_name for c in repo.list_chats()} == {"Alice", "Bob"}, (
        "Bob must not vanish because a search was open"
    )


def test_a_filtered_sidebar_queues_a_repair(engine):
    instance, reader, repo = engine
    reader.searching = True
    reader.rows = [row("Alice")]

    asyncio.run(instance._cycle())

    assert "clear_search" in kinds(instance)


def test_the_repair_actually_clears_it(engine):
    instance, reader, repo = engine
    reader.searching = True

    asyncio.run(instance._clear_search())

    assert reader.searching is False
    assert instance._sender.searches_cleared == 1


def test_the_repair_is_recorded(engine):
    """A silent self-heal hides how often it is happening."""
    instance, reader, repo = engine
    reader.searching = True

    asyncio.run(instance._clear_search())

    assert any(e.event == "search.cleared" for e in repo.recent_logs())


def test_clearing_nothing_presses_nothing(engine):
    """No query means no keys — otherwise the repair itself would be an
    interruption, pressing Escape into whatever the user was doing."""
    instance, reader, repo = engine
    reader.searching = False

    asyncio.run(instance._clear_search())

    assert instance._sender.searches_cleared == 0


def test_normal_cycles_queue_no_repair(engine):
    instance, reader, repo = engine
    reader.rows = [row("Alice")]

    asyncio.run(instance._cycle())

    assert "clear_search" not in kinds(instance)


# ---------------------------------------------------------------------------
# 3. Delivery does not depend on unrelated work happening
# ---------------------------------------------------------------------------


def test_a_pending_send_is_drained_even_when_nothing_else_runs(engine):
    """The queue was drained after each job, so a cycle that produced no jobs
    sent nothing — and a filtered sidebar produces no jobs indefinitely."""
    instance, reader, repo = engine
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      automation_enabled=True, seeded=True)
    repo.save_chat(chat)
    repo.enqueue_outgoing(OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice",
                                          text="owed", origin="api"))

    asyncio.run(instance._wake_drainer_if_owed())

    assert "drain" in kinds(instance)


def test_a_filtered_cycle_leaves_a_job_that_will_drain(engine):
    """The two halves together: the sidebar is unusable, and the message that
    was already queued must still go.

    The cycle does not add a separate `drain` — the repair job it queued is
    itself a job, and the worker drains after every one. Queueing both would be
    two wake-ups for one drain."""
    instance, reader, repo = engine
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      automation_enabled=True, seeded=True)
    repo.save_chat(chat)
    repo.enqueue_outgoing(OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice",
                                          text="owed", origin="api"))
    reader.searching = True
    reader.rows = [row("Alice")]

    asyncio.run(instance._cycle())

    assert kinds(instance) == ["clear_search"]


def test_the_repair_job_is_the_one_that_drains(engine):
    """A `clear_search` job carries no chat, and the worker used to drop any
    job whose chat it could not find — which would have skipped both the repair
    and the drain that follows every job."""
    source = inspect.getsource(AutomationEngine._worker)
    handled = source.index("clear_search")
    looked_up = source.index('if chat is None:')
    assert handled < looked_up, (
        "a chat-less job must be handled before the lookup that would discard it"
    )


def test_an_empty_queue_is_not_woken(engine):
    """Nothing owed, nothing queued — otherwise every idle cycle would add a
    job and the worker would spin."""
    instance, reader, repo = engine

    asyncio.run(instance._wake_drainer_if_owed())

    assert kinds(instance) == []


def test_a_busy_queue_is_not_piled_onto(engine):
    """A drain is already going to happen after the job in flight."""
    instance, reader, repo = engine
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      automation_enabled=True, seeded=True)
    repo.save_chat(chat)
    repo.enqueue_outgoing(OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice",
                                          text="owed", origin="api"))
    from wadam.engine.engine import _Job

    instance._enqueue(_Job("scan", chat.chat_id))

    asyncio.run(instance._wake_drainer_if_owed())

    assert kinds(instance) == ["scan"], "no second job while one is waiting"


def test_a_finished_message_does_not_keep_waking_the_drainer(engine):
    instance, reader, repo = engine
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      automation_enabled=True, seeded=True)
    repo.save_chat(chat)
    message = OutgoingMessage(chat_id=chat.chat_id, chat_name="Alice",
                              text="done", origin="api")
    repo.enqueue_outgoing(message)
    message.status = OutgoingStatus.DELIVERED
    repo.update_outgoing(message)

    asyncio.run(instance._wake_drainer_if_owed())

    assert kinds(instance) == []


# ---------------------------------------------------------------------------
# What an active search actually does to the sidebar (measured, not assumed)
# ---------------------------------------------------------------------------


def _sidebar(monkeypatch, rows, query):
    from wadam.whatsapp import reader as RD

    monkeypatch.setattr(RD, "_require_win32", lambda: None)
    monkeypatch.setattr(RD, "read_chat_rows_sync", lambda *_a, **_k: list(rows))
    monkeypatch.setattr(S, "search_query_sync", lambda *_a, **_k: query)
    return RD.read_sidebar_sync(0)


def test_an_empty_sidebar_with_a_query_in_the_box_is_a_stuck_search(monkeypatch):
    """The first version of this keyed off the search-results grid standing in
    for the recents grid. Measured on the real build, a query removes BOTH
    grids and the sidebar reads as completely empty:

        search='zzz'  recents=False  results=False  rows=0
        search=''     recents=True   results=False  rows=5

    So the detection saw nothing and the stall was not repaired."""
    assert _sidebar(monkeypatch, [], "zzz").filtered is True


def test_an_empty_sidebar_with_an_empty_box_is_not_a_search(monkeypatch):
    """WhatsApp still starting up reads exactly the same way, and pressing
    Escape into a window that is merely slow is an interruption, not a fix."""
    reading = _sidebar(monkeypatch, [], "")
    assert reading.filtered is False
    assert reading.rows == []


def test_rows_present_is_never_treated_as_filtered(monkeypatch):
    """The normal case must not cost a search-box probe or a repair job."""
    assert _sidebar(monkeypatch, [row("Alice")], "").filtered is False


def test_a_failed_probe_is_not_evidence_of_a_search(monkeypatch):
    """UIA raises transiently whenever WhatsApp re-renders. Treating that as
    'a search is active' would press Escape at random."""
    from wadam.whatsapp import reader as RD

    monkeypatch.setattr(RD, "_require_win32", lambda: None)
    monkeypatch.setattr(RD, "read_chat_rows_sync", lambda *_a, **_k: [])

    def boom(*_a, **_k):
        raise RuntimeError("transient COMError")

    monkeypatch.setattr(S, "search_query_sync", boom)
    assert RD.read_sidebar_sync(0).filtered is False
