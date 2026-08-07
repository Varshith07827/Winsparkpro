"""Shared fixtures — notably, running the same tests against both stores.

Two test doubles have now been caught lying: `FakeCollection.find_one` matched
only the query's first key, and `FakeMongo` was missing the `outgoing`
collection entirely. Both times the production code was correct and the double
was not, which is the failure mode that makes doubles worse than useless — they
produce confidence in proportion to how wrong they are.

So the storage-dependent suites run **twice**: once against the dict-backed
fake (fast, always available) and once against a real `mongod`. Anything that
passes on the fake and fails on the real one is a bug in the fake; anything
that fails on both is a bug in the code. The real pass is skipped, not failed,
when no server is reachable, so the suite still runs on a machine without one.

Point it elsewhere with `WADAM_TEST_MONGODB_URI`.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.mongo import MongoStore
from wadam.storage.repository import Repository

TEST_URI = os.environ.get("WADAM_TEST_MONGODB_URI", "mongodb://localhost:27017")

_mongo_available: bool | None = None


def mongo_available() -> bool:
    """Probed once per session — a connect attempt per test would dominate."""
    global _mongo_available
    if _mongo_available is None:
        store = MongoStore(TEST_URI, "wadam_probe")
        try:
            store.connect()
            store.close()
            _mongo_available = True
        except Exception:  # noqa: BLE001
            _mongo_available = False
    return _mongo_available


@pytest.fixture(params=["fake", "mongo"])
def storage(request, tmp_path: Path):
    """A started Repository backed by either store.

    Yields `(repository, settings)`. The real-MongoDB parameter uses a fresh
    throwaway database per test and drops it afterwards, so tests never see
    each other's data and nothing survives the run."""
    if request.param == "mongo" and not mongo_available():
        pytest.skip(f"no MongoDB at {TEST_URI}")

    settings = Settings(
        mongodb_uri=TEST_URI,
        database_name=f"wadam_test_{uuid.uuid4().hex[:8]}",
        json_backup_folder=tmp_path,
        json_autosave_interval=0,
    )
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()

    if request.param == "fake":
        from tests.test_storage import FakeMongo

        store = FakeMongo()
    else:
        store = MongoStore(settings.mongodb_uri, settings.database_name)
        store.connect()

    repository = Repository(settings, store, backup)
    repository.start()
    try:
        yield repository, settings
    finally:
        repository.stop()
        if request.param == "mongo":
            try:
                store._client.drop_database(settings.database_name)
            except Exception:  # noqa: BLE001
                pass
            store.close()


@pytest.fixture()
def reopen(tmp_path: Path):
    """Simulate a restart: a NEW Repository over the SAME stores.

    The whole point of a durable queue is that it survives the process, so
    recovery tests must reconstruct the repository rather than reusing the
    instance that already has everything in memory."""
    def _reopen(repository: Repository, settings: Settings) -> Repository:
        restarted = Repository(settings, repository._mongo,
                               JsonBackupStore(settings.json_backup_folder, 0))
        restarted.start()
        return restarted

    return _reopen
