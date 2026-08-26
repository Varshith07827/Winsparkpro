"""Test doubles for the storage layer.

Two doubles here have been caught lying: `FakeCollection.find_one` matched only
the query's first key, and `FakeMongo` was missing a collection entirely. Both
times the production code was correct and the double was not, which is the
failure mode that makes doubles worse than useless — they produce confidence in
proportion to how wrong they are.

That is why `conftest.py` runs the storage-dependent suites against a real
`mongod` as well. Anything that passes here and fails there is a bug in this
file.
"""

from __future__ import annotations


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

    @property
    def database(self):
        """The retired-collection sweep asks for this. Nothing to drop in a
        dict-backed store, so it reports an empty database rather than raising
        and filling every test run with warnings."""
        return _FakeDatabase()


class _FakeDatabase:
    def list_collection_names(self):
        return []

    def drop_collection(self, _name):
        pass
