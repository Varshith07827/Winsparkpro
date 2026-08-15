"""Integration tests against a real MongoDB.

Everything else in the suite uses a dict-backed fake, which proves the
repository's *contract* but not that pymongo agrees with it. These run against
an actual server, in a throwaway database that is dropped afterwards, and are
skipped when no server is reachable so the suite still passes on a machine
without one.

Point them somewhere else with `WADAM_TEST_MONGODB_URI`.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from wadam import constants
from wadam.config import Settings
from wadam.domain.models import (
    ChatConfig,
    MessageStatus,
    StoredMessage,
    WebhookRecord,
    chat_id_for,
    message_key_for,
    utcnow,
)
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.mongo import MongoStore, MongoUnavailableError, is_local_uri, timeout_for
from wadam.storage.repository import Repository

URI = os.environ.get("WADAM_TEST_MONGODB_URI", "mongodb://localhost:27017")


def _server_available() -> bool:
    store = MongoStore(URI, "wadam_probe")
    try:
        store.connect()
        store.close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _server_available(), reason=f"no MongoDB reachable at {URI}"
)


@pytest.fixture()
def store():
    name = f"wadam_test_{uuid.uuid4().hex[:8]}"
    mongo = MongoStore(URI, name)
    mongo.connect()
    yield mongo
    mongo._client.drop_database(name)
    mongo.close()


@pytest.fixture()
def repo(store, tmp_path: Path) -> Repository:
    settings = Settings(mongodb_uri=URI, database_name=store._database_name,
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, store, backup)
    repository.start()
    yield repository
    repository.stop()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_connecting_verifies_with_a_real_round_trip():
    mongo = MongoStore(URI, "wadam_probe")
    mongo.connect()
    assert mongo.connected is True
    assert "connected" in mongo.status_text
    mongo.close()


def test_an_unreachable_server_fails_at_startup_with_a_readable_message():
    mongo = MongoStore("mongodb://127.0.0.1:27099", "wadam_probe")
    with pytest.raises(MongoUnavailableError) as raised:
        mongo.connect()
    # pymongo connects lazily, so without the explicit ping this would have
    # looked fine here and failed later, long after the startup screen closed.
    assert "Could not reach MongoDB" in str(raised.value)


def test_local_and_remote_get_different_timeouts():
    assert is_local_uri("mongodb://localhost:27017") is True
    assert is_local_uri("mongodb+srv://c.example.mongodb.net") is False
    # A local server answers instantly or not at all; Atlas has to resolve SRV
    # and finish a TLS handshake first, so a short timeout there is a false
    # negative waiting to happen.
    assert timeout_for("mongodb://localhost:27017") < timeout_for("mongodb+srv://c.x.net")


def test_the_database_comes_from_configuration_not_the_uri_path(store):
    # `mongodb://host/admin` is the shape you get by habit — the auth database
    # belongs in a connection string, but as ?authSource=admin, not as the path.
    mongo = MongoStore(f"{URI}/admin", store._database_name)
    mongo.connect()
    assert mongo._db.name == store._database_name
    mongo.close()


# ---------------------------------------------------------------------------
# Collections and indexes
# ---------------------------------------------------------------------------


def test_all_six_collections_and_their_indexes_exist(repo: Repository, store: MongoStore):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice")
    repo.save_chat(chat)
    repo.save_message(StoredMessage(
        message_key=message_key_for(chat.chat_id, "Alice", "hi", "", "in"),
        chat_id=chat.chat_id, text="hi",
    ))
    repo.save_webhook(WebhookRecord(chat_id=chat.chat_id, url="https://x.test/h"))
    repo.log("INFO", "test.event", chat_id=chat.chat_id, message="hello")
    repo.save_poll_state(repo.poll_state)

    names = set(store._db.list_collection_names())
    assert {
        constants.COLLECTION_CHAT_CONFIGS, constants.COLLECTION_MESSAGES,
        constants.COLLECTION_WEBHOOKS, constants.COLLECTION_APPLICATION_STATE,
    } <= names
    assert not set(constants.RETIRED_COLLECTIONS) & names, (
        "logs and poll counters are kept locally — writing them here is what "
        "1.7 million operations a month was made of"
    )

    message_indexes = {i["name"] for i in store.messages.list_indexes()}
    assert any("message_key" in name for name in message_indexes)


def test_the_unique_index_refuses_a_duplicate_message(repo: Repository, store: MongoStore):
    """The last line of defence for deduplication: even if two code paths race,
    the database refuses the second write."""
    chat_id = chat_id_for("Alice")
    key = message_key_for(chat_id, "Alice", "hello", "9:21 pm", "in")

    assert repo.save_message(StoredMessage(message_key=key, chat_id=chat_id, text="hello")) is True
    assert repo.save_message(StoredMessage(message_key=key, chat_id=chat_id, text="hello")) is False
    assert store.messages.count_documents({"message_key": key}) == 1


def test_datetimes_survive_the_round_trip_as_utc(repo: Repository, store: MongoStore):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice", last_poll_utc=utcnow())
    repo.save_chat(chat)

    document = store.chat_configs.find_one({"chat_id": chat.chat_id})
    restored = ChatConfig.from_document(document)
    # BSON dates come back naive; from_document re-attaches UTC so nothing
    # downstream ever compares an aware datetime with a naive one.
    assert restored.last_poll_utc is not None
    assert restored.last_poll_utc.tzinfo is not None
    assert abs((restored.last_poll_utc - chat.last_poll_utc).total_seconds()) < 1


def test_state_survives_a_repository_restart(store: MongoStore, tmp_path: Path):
    settings = Settings(mongodb_uri=URI, database_name=store._database_name,
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()

    first = Repository(settings, store, backup)
    first.start()
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      webhook_url="https://x.test/alice", automation_enabled=True, seeded=True)
    first.save_chat(chat)
    key = message_key_for(chat.chat_id, "Alice", "hello", "9:00 am", "in")
    first.save_message(StoredMessage(message_key=key, chat_id=chat.chat_id, text="hello",
                                     status=MessageStatus.REPLIED))
    runs_before = first.app_state.run_count
    first.stop()

    second = Repository(settings, store, JsonBackupStore(tmp_path, 0))
    second.start()
    restored = second.get_chat(chat.chat_id)
    assert restored is not None
    assert restored.automation_enabled is True
    assert restored.webhook_url == "https://x.test/alice"
    assert second.has_message(key) is True
    assert second.app_state.run_count == runs_before + 1
    second.stop()


def test_incomplete_work_is_queryable_after_a_restart(store: MongoStore, tmp_path: Path):
    settings = Settings(mongodb_uri=URI, database_name=store._database_name,
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()

    first = Repository(settings, store, backup)
    first.start()
    chat_id = chat_id_for("Alice")
    first.save_chat(ChatConfig(chat_id=chat_id, chat_name="Alice"))
    for index, status in enumerate((MessageStatus.PENDING, MessageStatus.DISPATCHING,
                                    MessageStatus.AWAITING_SEND, MessageStatus.REPLIED)):
        first.save_message(StoredMessage(
            message_key=message_key_for(chat_id, "Alice", f"m{index}", "", "in"),
            chat_id=chat_id, text=f"m{index}", status=status,
        ))
    first.stop()

    second = Repository(settings, store, JsonBackupStore(tmp_path, 0))
    second.start()
    incomplete = second.incomplete_messages()
    assert {m.status for m in incomplete} == {
        MessageStatus.PENDING, MessageStatus.DISPATCHING, MessageStatus.AWAITING_SEND
    }
    second.stop()


def test_deleting_a_chat_removes_its_rows(repo: Repository, store: MongoStore):
    chat_id = chat_id_for("Alice")
    repo.save_chat(ChatConfig(chat_id=chat_id, chat_name="Alice"))
    repo.save_message(StoredMessage(
        message_key=message_key_for(chat_id, "Alice", "hi", "", "in"),
        chat_id=chat_id, text="hi",
    ))
    repo.save_webhook(WebhookRecord(chat_id=chat_id, url="https://x.test/h"))

    repo.delete_chat(chat_id)

    assert store.chat_configs.count_documents({"chat_id": chat_id}) == 0
    assert store.messages.count_documents({"chat_id": chat_id}) == 0
    assert store.webhooks.count_documents({"chat_id": chat_id}) == 0


def test_bulk_chat_save_upserts_every_row(repo: Repository, store: MongoStore):
    chats = [ChatConfig(chat_id=chat_id_for(f"c{i}"), chat_name=f"c{i}") for i in range(25)]
    repo.save_chats(chats)
    assert store.chat_configs.count_documents({}) >= 25

    for chat in chats:
        chat.automation_enabled = True
    repo.save_chats(chats)
    assert store.chat_configs.count_documents({"automation_enabled": True}) == 25


def test_logs_and_poll_counters_stay_out_of_the_database(repo: Repository, store: MongoStore):
    """Against a real mongod.

    Sixty log lines and a poll state used to be sixty-one billable writes, plus
    a periodic scan-and-delete to stop the collection growing without limit.
    Now they are no writes at all and the collections never come into
    existence — the ring buffer and its JSON mirror were always the copies
    anyone actually read."""
    for index in range(60):
        repo.log("INFO", "test.bulk", message=f"line {index}")
    repo.save_poll_state(repo.poll_state)

    names = set(store._db.list_collection_names())
    assert not set(constants.RETIRED_COLLECTIONS) & names
    assert len(repo.recent_logs(limit=100)) >= 60, "kept locally, in full"
