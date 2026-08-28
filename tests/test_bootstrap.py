"""Working out what was left out of .env.

Each of these replaced a value that had to be typed correctly into two places,
and whose failure showed up somewhere other than where the mistake was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.config import ConfigError, Settings
from wadam.engine.bootstrap import prepare, resolve_session_id, resolve_webhook_secret
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.fakes import FakeMongo

ONE = {"id": "aaaa-1111", "name": "my-bot", "status": "ready"}
TWO = {"id": "bbbb-2222", "name": "spare", "status": "ready"}


class FakeClient:
    def __init__(self, sessions=None, webhooks=None, fail_ensure=False) -> None:
        self._sessions = sessions if sessions is not None else [ONE]
        self._webhooks = webhooks or []
        self._fail_ensure = fail_ensure
        self.ensured: list[tuple] = []

    def list_sessions(self):
        return list(self._sessions)

    def list_webhooks(self, session_id):
        return list(self._webhooks)

    def ensure_webhook(self, session_id, url, secret, events=("message.received",)):
        if self._fail_ensure:
            raise RuntimeError("SSRF guard refused a private address")
        self.ensured.append((session_id, url, secret))
        return "hook-1"


def settings_for(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        mongodb_uri="mongodb://localhost:27017", database_name="test",
        json_backup_folder=tmp_path, json_autosave_interval=0,
        openwa_url="http://localhost:2785", openwa_api_key="k",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def repo(tmp_path: Path):
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings_for(tmp_path), FakeMongo(), backup)
    repository.start()
    yield repository
    repository.stop()


# ── the session ───────────────────────────────────────────────────────


def test_the_only_session_is_used_without_being_named(tmp_path):
    assert resolve_session_id(settings_for(tmp_path), FakeClient()) == "aaaa-1111"


def test_an_explicit_session_is_honoured(tmp_path):
    settings = settings_for(tmp_path, openwa_session_id="bbbb-2222")
    assert resolve_session_id(settings, FakeClient([ONE, TWO])) == "bbbb-2222"


def test_several_sessions_ask_which_and_name_them(tmp_path):
    with pytest.raises(ConfigError) as caught:
        resolve_session_id(settings_for(tmp_path), FakeClient([ONE, TWO]))

    problem = " ".join(caught.value.problems)
    assert "OPENWA_SESSION_ID" in problem
    assert "my-bot" in problem and "spare" in problem


def test_a_stale_session_id_is_caught_at_startup(tmp_path):
    """Rather than 404-ing on every send later. The mistake people make is
    pasting the session's name, so the error names the real ones."""
    settings = settings_for(tmp_path, openwa_session_id="my-bot")

    with pytest.raises(ConfigError) as caught:
        resolve_session_id(settings, FakeClient([ONE]))

    problem = " ".join(caught.value.problems)
    assert "not a session" in problem
    assert "aaaa-1111" in problem


def test_no_sessions_at_all_says_so(tmp_path):
    with pytest.raises(ConfigError) as caught:
        resolve_session_id(settings_for(tmp_path), FakeClient([]))
    assert "no sessions" in " ".join(caught.value.problems)


# ── the secret ────────────────────────────────────────────────────────


def test_a_secret_is_generated_and_remembered(tmp_path, repo):
    settings = settings_for(tmp_path)

    first = resolve_webhook_secret(settings, repo)
    second = resolve_webhook_secret(settings, repo)

    assert len(first) >= 32
    assert first == second, "a secret that changed per run would break every delivery"


def test_an_explicit_secret_wins(tmp_path, repo):
    settings = settings_for(tmp_path, webhook_secret="a-secret-that-is-long-enough")
    assert resolve_webhook_secret(settings, repo) == "a-secret-that-is-long-enough"


# ── the webhook ───────────────────────────────────────────────────────


def test_openwa_is_told_where_to_deliver(tmp_path, repo):
    client = FakeClient()

    effective, _ = prepare(settings_for(tmp_path), repo, lambda _sid: client)

    assert effective.openwa_session_id == "aaaa-1111"
    assert client.ensured, "the webhook should have been registered"
    session_id, url, secret = client.ensured[0]
    assert session_id == "aaaa-1111"
    assert url == "http://host.docker.internal:8765/hook"
    assert secret == effective.webhook_secret


def test_an_explicit_public_url_is_used(tmp_path, repo):
    client = FakeClient()
    settings = settings_for(tmp_path, webhook_public_url="https://wadam.example/hook")

    prepare(settings, repo, lambda _sid: client)

    assert client.ensured[0][1] == "https://wadam.example/hook"


def test_registration_can_be_turned_off(tmp_path, repo):
    client = FakeClient()

    prepare(settings_for(tmp_path, register_webhook=False), repo, lambda _sid: client)

    assert client.ensured == []


def test_a_refused_registration_does_not_stop_the_launch(tmp_path, repo):
    """Sending works with no inbound webhook at all, and the usual cause is
    OpenWA's SSRF guard — a change this process cannot make."""
    client = FakeClient(fail_ensure=True)

    effective, _ = prepare(settings_for(tmp_path), repo, lambda _sid: client)

    assert effective.openwa_session_id == "aaaa-1111"
