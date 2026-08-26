"""The inbound send API — resolving an identifier to a chat, and sending.

The rule under test is the one worth being rigid about: an identifier that
matches more than one chat is refused, never delivered to a guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.api.host import SendApiHost
from wadam.config import Settings
from wadam.domain.models import ChatConfig
from wadam.engine.service import AutomationService
from wadam.openwa import SendError
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.fakes import FakeMongo


class FakeClient:
    def __init__(self, fail_with: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_with = fail_with

    def send_text(self, chat_id: str, text: str) -> dict:
        if self._fail_with:
            raise SendError(self._fail_with, status=502)
        self.sent.append((chat_id, text))
        return {"ok": True}


@pytest.fixture()
def host(tmp_path: Path):
    settings = Settings(
        mongodb_uri="mongodb://localhost:27017", database_name="test",
        json_backup_folder=tmp_path, json_autosave_interval=0,
        openwa_url="http://localhost:2785", openwa_api_key="k",
        openwa_session_id="s", api_port=0,
    )
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()

    service = AutomationService(settings, repository, lambda m, c: None)
    client = FakeClient()
    service._client = client          # noqa: SLF001 - swapping the transport is the point
    service._pipeline._client = client  # noqa: SLF001

    api = SendApiHost(settings, repository, service)
    yield api, repository, client
    repository.stop()


def add(repo, chat_id: str, name: str, phone: str = ""):
    repo.save_chat(ChatConfig(chat_id=chat_id, chat_name=name, phone_number=phone))


# ── resolution ────────────────────────────────────────────────────────


def test_an_exact_chat_id_is_used_as_is(host):
    api, repo, client = host
    add(repo, "216298915164281@lid", "Alice")

    response = api._send("216298915164281@lid", "hello")  # noqa: SLF001

    assert response.status == 200
    assert client.sent == [("216298915164281@lid", "hello")]


def test_a_chat_name_resolves(host):
    api, repo, client = host
    add(repo, "216298915164281@lid", "Alice")

    response = api._send("Alice", "hello")  # noqa: SLF001

    assert response.status == 200
    assert client.sent[0][0] == "216298915164281@lid"


def test_a_name_matches_regardless_of_case(host):
    api, repo, client = host
    add(repo, "x@c.us", "Alice")

    assert api._send("alice", "hi").status == 200  # noqa: SLF001


def test_a_phone_number_resolves(host):
    api, repo, client = host
    add(repo, "918985370703@c.us", "Alice", phone="918985370703")

    response = api._send("918985370703", "hello")  # noqa: SLF001

    assert response.status == 200
    assert client.sent[0][0] == "918985370703@c.us"


def test_an_ambiguous_identifier_is_refused_not_guessed(host):
    """Sending to the wrong person is the one failure that must not happen
    quietly, so this is a 409 rather than a choice."""
    api, repo, client = host
    add(repo, "a@c.us", "Alice")
    add(repo, "b@lid", "Alice")

    response = api._send("Alice", "hello")  # noqa: SLF001

    assert response.status == 409
    assert response.payload["code"] == "ambiguous"
    assert len(response.payload["candidates"]) == 2
    assert client.sent == []


def test_an_unknown_name_is_refused(host):
    api, repo, client = host

    response = api._send("Nobody", "hello")  # noqa: SLF001

    assert response.status == 404
    assert response.payload["code"] == "unknown_chat"
    assert client.sent == []


def test_an_unseen_chat_id_is_still_deliverable(host):
    """A chat this application has never received a message from is still a
    real chat; refusing would make the API useless for starting one."""
    api, repo, client = host

    response = api._send("918985370703@c.us", "hello")  # noqa: SLF001

    assert response.status == 200
    assert client.sent == [("918985370703@c.us", "hello")]


# ── outcomes ──────────────────────────────────────────────────────────


def test_a_sent_message_is_recorded_in_the_chats_history(host):
    api, repo, client = host
    add(repo, "216298915164281@lid", "Alice")

    api._send("Alice", "from the api")  # noqa: SLF001

    stored = repo.messages_for("216298915164281@lid")
    assert [(m.direction, m.origin, m.text) for m in stored] == [("out", "api", "from the api")]


def test_a_failed_send_reports_502(host, tmp_path: Path):
    api, repo, _ = host
    failing = FakeClient(fail_with="engine not ready")
    api._service._client = failing  # noqa: SLF001
    add(repo, "216298915164281@lid", "Alice")

    response = api._send("Alice", "hello")  # noqa: SLF001

    assert response.status == 502
    assert response.payload["code"] == "send_failed"


def test_the_api_is_off_without_a_port(host):
    api, _, _ = host
    assert api.enabled is False
