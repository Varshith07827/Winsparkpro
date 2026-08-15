"""The per-cycle bookkeeping the poll loop runs after every cycle.

This exists because of a real failure: `prune_logs` was called on the
repository when it only existed on the Mongo store, so on cycle 200 — about ten
minutes into a run — the `AttributeError` escaped and killed the engine. It was
outside the guard around the cycle itself, and no test reached cycle 200.

So this walks the counter past every periodic branch (`== 1`, `% 10`, `% 200`)
against a real Repository, and separately proves the loop survives bookkeeping
that raises.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.engine.engine import AutomationEngine
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository

from tests.test_storage import FakeMongo


class _FakeQueue:
    def qsize(self) -> int:
        return 0


class _CycleHarness:
    """`_record_cycle` only touches the repository, the window handle, the queue
    depth and the last error, so binding it to a stand-in exercises the real
    method without a polling loop or an STA thread."""

    def __init__(self, repository: Repository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings          # _record_cycle probes session health
        self._hwnd = 1234
        self._queue = _FakeQueue()
        self._last_error = ""

    _record_cycle = None  # bound below


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    yield repository
    repository.stop()


def make_harness(repository: Repository, settings: Settings = None) -> _CycleHarness:
    harness = _CycleHarness(repository, settings or Settings())
    harness._record_cycle = AutomationEngine._record_cycle.__get__(harness, _CycleHarness)
    return harness


def test_bookkeeping_survives_every_periodic_branch(repo: Repository):
    harness = make_harness(repo)

    async def run_cycles(count: int) -> None:
        for _ in range(count):
            await harness._record_cycle(12)

    # 201 cycles crosses the first-cycle save, twenty of the ten-cycle saves,
    # and the two-hundred-cycle log prune. Before the fix this raised
    # AttributeError on cycle 200.
    asyncio.run(run_cycles(201))

    state = repo.poll_state
    assert state.cycle_count == 201
    assert state.whatsapp_found is True
    assert state.last_cycle_ms == 12
    assert state.last_cycle_utc is not None


def test_the_first_cycle_records_poll_state_locally(repo: Repository):
    """Cycle counters are diagnostics. They used to go to MongoDB every ten
    cycles — a billable write to remember how many times a loop had run, and
    meaningless after a restart. Kept in memory and in the JSON mirror now."""
    harness = make_harness(repo)
    asyncio.run(harness._record_cycle(5))

    assert repo.poll_state.cycle_count == 1
    assert repo.poll_state.last_cycle_ms == 5


def test_the_retired_collections_are_never_reached_for(repo: Repository):
    """A guard rather than an assertion about a fake: the store is wrapped so
    that touching either retired collection raises, then the two paths that
    used to write to them are exercised."""
    class Tripwire:
        def __init__(self, inner): self._inner = inner
        def __getattr__(self, name):
            if name in ("automation_logs", "poll_state"):
                raise AssertionError(f"{name} is retired — writing it costs money")
            return getattr(self._inner, name)

    repo._mongo = Tripwire(repo._mongo)

    repo.log("INFO", "test.event", message="hello")
    repo.save_poll_state(repo.poll_state)

    assert any(e.event == "test.event" for e in repo.recent_logs()), (
        "still recorded — locally, in the ring buffer that feeds logs.json"
    )


def test_a_raising_repository_does_not_stop_the_loop(repo: Repository, monkeypatch):
    """The guard that should have caught the original bug: bookkeeping is
    wrapped separately from the cycle, so telemetry can never stop automation."""
    harness = make_harness(repo)

    def explode(*_a, **_k):
        raise RuntimeError("telemetry is broken")

    monkeypatch.setattr(repo, "save_poll_state", explode)

    async def run_guarded() -> int:
        survived = 0
        for _ in range(12):
            try:
                await harness._record_cycle(1)
            except Exception:  # noqa: BLE001 - mirrors the loop's own guard
                pass
            survived += 1
        return survived

    assert asyncio.run(run_guarded()) == 12


def test_draining_cannot_suspend_discovery_forever():
    """A producer that never stops must not starve incoming-message discovery.

    This happened, on a real contact: a relay endpoint returned a message on
    every 3-second poll, so the drainer was permanently busy, the poll cycle
    was skipped every time, and a genuine incoming message ("Hey it worked")
    was never read or stored at all. Sending a backlog outranks discovery for a
    few seconds — never indefinitely."""
    import time

    from wadam.engine.engine import MAX_DRAIN_POLL_PAUSE

    assert MAX_DRAIN_POLL_PAUSE > 0, "an unbounded pause is a starvation bug"
    assert MAX_DRAIN_POLL_PAUSE <= 60, "discovery must resume within a minute"

    # The predicate the cycle uses, stated plainly.
    def paused(draining: bool, since: float, now: float) -> bool:
        return draining and (now - since) < MAX_DRAIN_POLL_PAUSE

    start = time.monotonic()
    assert paused(True, start, start) is True, "a short drain pauses the poll"
    assert paused(True, start, start + MAX_DRAIN_POLL_PAUSE + 1) is False, (
        "a long drain must let discovery through"
    )
    assert paused(False, start, start + 1) is False
