"""Storage: the JSON mirror's controlled save, and the repository's
write-to-both contract with a fake MongoDB.

The fake is a dict-backed stand-in for the four collection methods the
repository actually calls. It exists so these tests assert the *contract*
("every write reaches both stores", "a duplicate is refused") without needing a
running mongod — the real driver is exercised at startup by a ping, which is a
different question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wadam import constants
from wadam.config import Settings, parse_env_text
from wadam.domain.models import ChatConfig, StoredMessage, chat_id_for, message_key_for
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository


def _matches(document: dict, query: dict) -> bool:
    """Does this document satisfy the query?

    Every key, not just the first. The fake originally matched only the leading
    key, which made a multi-key lookup answer from whatever document happened to
    share it — the relay's dedup query is exactly that shape, so the fake was
    quietly saying "already sent" about the wrong message. A stand-in that
    answers differently from the real thing is worse than no stand-in.

    Supports equality, `$in`, `$lt` and `$exists`, which is everything this
    application queries with. `$exists` was added when the legacy-field
    migration matched EVERY document here and none in real MongoDB — a
    stand-in that answers differently from the real thing is worse than no
    stand-in."""
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$exists" in expected and (key in document) != bool(expected["$exists"]):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$lt" in expected and not (actual is not None and actual < expected["$lt"]):
                return False
        elif actual != expected:
            return False
    return True


class FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict] = []

    def create_index(self, *_a, **_k):
        return "index"

    def insert_one(self, document):
        self.documents.append(dict(document))

    def update_one(self, query, update, upsert=False):
        key, value = next(iter(query.items()))
        for existing in self.documents:
            if existing.get(key) == value:
                existing.update(update["$set"])
                return
        if upsert:
            self.documents.append(dict(update["$set"]))

    def update_many(self, query, update):
        """Plain equality AND {"$in": [...]}, because real MongoDB takes both.

        This understood only `$in` and raised on `{"chat_id": "abc"}` — the
        third time a double here has been narrower than the thing it stands in
        for. See `tests/conftest.py`: the storage suites run against a real
        mongod as well for exactly this reason."""
        for existing in self.documents:
            if _matches(existing, query):
                existing.update(update.get("$set") or {})
                for field in (update.get("$unset") or {}):
                    existing.pop(field, None)

    def bulk_write(self, operations, ordered=False):
        for operation in operations:
            self.update_one(operation._filter, operation._doc, upsert=True)

    def delete_one(self, query):
        key, value = next(iter(query.items()))
        self.documents = [d for d in self.documents if d.get(key) != value]

    def delete_many(self, query):
        self.documents = [d for d in self.documents if not _matches(d, query)]

    def find(self, query=None, *_a, **_k):
        return FakeCursor([d for d in self.documents if _matches(d, query or {})])

    def find_one(self, query):
        for document in self.documents:
            if _matches(document, query):
                return dict(document)
        return None

    def count_documents(self, query=None):
        return sum(1 for d in self.documents if _matches(d, query or {}))

    def estimated_document_count(self):
        return len(self.documents)


class FakeCursor(list):
    def sort(self, *_a, **_k):
        return self

    def limit(self, n):
        return FakeCursor(self[:n])


class FakeMongo:
    def __init__(self) -> None:
        self.chat_configs = FakeCollection()
        self.messages = FakeCollection()
        self.webhooks = FakeCollection()
        self.outgoing = FakeCollection()
        self.automation_logs = FakeCollection()
        self.application_state = FakeCollection()
        self.poll_state = FakeCollection()
        self.connected = True
        self.status_text = "connected · test"

    def note_success(self):
        self.connected = True

    def note_failure(self, _ex):
        self.connected = False

    def prune_logs(self, *_a, **_k):
        pass


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


def read_json(folder: Path, name: str):
    return json.loads((folder / name).read_text(encoding="utf-8"))


def test_a_chat_write_reaches_mongo_and_json(repo: Repository, tmp_path: Path):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice")
    repo.save_chat(chat)
    repo.flush_json(force=True)

    assert repo.get_chat(chat.chat_id) is chat
    mirrored = read_json(tmp_path, constants.JSON_CHATS)
    assert [c["chat_name"] for c in mirrored] == ["Alice"]


def test_the_mirror_is_replaced_atomically_never_truncated(repo: Repository, tmp_path: Path):
    for index in range(5):
        repo.save_chat(ChatConfig(chat_id=chat_id_for(f"c{index}"), chat_name=f"c{index}"))
        repo.flush_json(force=True)
        # Every intermediate state is valid JSON — that is what os.replace buys.
        assert len(read_json(tmp_path, constants.JSON_CHATS)) == index + 1
    # No temp files left behind.
    assert not list(tmp_path.glob("*.tmp"))


def test_a_duplicate_message_is_refused(repo: Repository):
    chat_id = chat_id_for("Alice")
    key = message_key_for(chat_id, "Alice", "hello", "9:21 pm", "in")
    first = StoredMessage(message_key=key, chat_id=chat_id, text="hello")
    second = StoredMessage(message_key=key, chat_id=chat_id, text="hello")

    assert repo.save_message(first) is True
    assert repo.save_message(second) is False
    assert len(repo.messages_for(chat_id)) == 1


def test_deleting_a_chat_removes_its_messages_from_both_stores(repo: Repository, tmp_path: Path):
    chat_id = chat_id_for("Bob")
    repo.save_chat(ChatConfig(chat_id=chat_id, chat_name="Bob"))
    repo.save_message(StoredMessage(
        message_key=message_key_for(chat_id, "Bob", "hi", "", "in"),
        chat_id=chat_id, text="hi",
    ))
    repo.flush_json(force=True)

    repo.delete_chat(chat_id)
    repo.flush_json(force=True)

    assert repo.get_chat(chat_id) is None
    assert read_json(tmp_path, constants.JSON_CHATS) == []
    assert read_json(tmp_path, constants.JSON_MESSAGES) == []


def test_every_mirror_file_is_written(repo: Repository, tmp_path: Path):
    repo.flush_json(force=True)
    for name in (constants.JSON_CHATS, constants.JSON_MESSAGES, constants.JSON_WEBHOOKS,
                 constants.JSON_AUTOMATION, constants.JSON_APP_STATE, constants.JSON_LOGS,
                 constants.JSON_SETTINGS):
        assert (tmp_path / name).is_file(), f"{name} was not written"


def test_settings_mirror_redacts_credentials(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb+srv://user:s3cret@cluster.mongodb.net/",
                        webhook_api_key="token", json_backup_folder=tmp_path)
    redacted = settings.redacted()
    assert "s3cret" not in redacted["mongodb_uri"]
    assert redacted["webhook_api_key"] == "***"


def test_export_writes_a_standalone_file(repo: Repository, tmp_path: Path):
    chat_id = chat_id_for("Carol")
    repo.save_chat(ChatConfig(chat_id=chat_id, chat_name="Carol"))
    repo.save_message(StoredMessage(
        message_key=message_key_for(chat_id, "Carol", "yo", "", "in"),
        chat_id=chat_id, text="yo",
    ))
    target = tmp_path / "exports" / "carol.json"
    repo.export_chat(chat_id, target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["chat"]["chat_name"] == "Carol"
    assert [m["text"] for m in payload["messages"]] == ["yo"]


def test_env_parser_keeps_urls_with_equals_signs():
    values = parse_env_text(
        "# a comment\n"
        "export DEFAULT_WEBHOOK='https://x.test/hook?a=1&b=2'\n"
        "DATABASE_NAME=\"wadam\"\n"
        "BLANK=\n"
    )
    assert values["DEFAULT_WEBHOOK"] == "https://x.test/hook?a=1&b=2"
    assert values["DATABASE_NAME"] == "wadam"
    assert values["BLANK"] == ""
