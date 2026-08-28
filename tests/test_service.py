"""The HTTP boundary: what a delivery gets answered with.

Every check answers 200 except a bad signature. A 4xx or 5xx tells OpenWA the
delivery failed and earns a retry, and there is nothing to retry about a
message that was correctly ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.engine.service import AutomationService
from wadam.engine.webhook import WebhookOutcome
from wadam.openwa import SendError
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.fakes import FakeMongo

SECRET = "test-secret-at-least-16-chars"
CHAT_ID = "111111111111111@lid"


class FakeClient:
    def __init__(self, fail_with: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_with = fail_with

    def send_text(self, chat_id: str, text: str) -> dict:
        if self._fail_with:
            raise SendError(self._fail_with, status=500)
        self.sent.append((chat_id, text))
        return {"ok": True}


def body(text: str = "hi", message_id: str = "m1", direction: str = "incoming") -> bytes:
    return json.dumps({
        "event": "message.received",
        "data": {"waMessageId": message_id, "chatId": CHAT_ID, "chatName": "Alice",
                 "body": text, "type": "text", "direction": direction},
    }).encode("utf-8")


def sign(payload: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class FakeWebhook:
    """The endpoint, stubbed. Answers with `reply`, or fails."""

    def __init__(self, reply="pong", ok=True) -> None:
        self._reply, self._ok = reply, ok
        self.calls = []

    def call(self, url, payload, sleep=None):
        self.calls.append((url, payload))
        if not self._ok:
            return WebhookOutcome(False, "502", error="HTTP 502", attempts=1)
        return WebhookOutcome(True, "200 OK", reply_text=self._reply or "")


def build(tmp_path: Path, secret: str = SECRET, reply="pong", client=None):
    settings = Settings(
        mongodb_uri="mongodb://localhost:27017", database_name="test",
        json_backup_folder=tmp_path, json_autosave_interval=0,
        openwa_url="http://localhost:2785", openwa_api_key="k",
        openwa_session_id="s", webhook_secret=secret, cooldown_seconds=0,
        default_webhook="https://example.test/hook",
    )
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()

    service = AutomationService(settings, repository)
    client = client or FakeClient()
    service._client = client                             # noqa: SLF001
    service._pipeline._client = client                   # noqa: SLF001
    service._pipeline._webhook = FakeWebhook(reply)      # noqa: SLF001
    return service, repository, client


@pytest.fixture()
def service(tmp_path: Path):
    svc, repo, client = build(tmp_path)
    yield svc, repo, client
    repo.stop()


def enable(repo, service):
    """Register the chat, then switch it on the way the window would."""
    service.handle_delivery(body(message_id="m0"), sign(body(message_id="m0")))
    service.set_chat_automation(CHAT_ID, True)


# ── signatures ────────────────────────────────────────────────────────


def test_a_bad_signature_is_refused_with_401(service):
    svc, _, client = service

    status, payload = svc.handle_delivery(body(), "sha256=wrong")

    assert status == 401
    assert payload["ok"] is False
    assert client.sent == []


def test_a_missing_signature_is_refused(service):
    svc, _, client = service
    assert svc.handle_delivery(body(), None)[0] == 401


def test_a_rejected_delivery_is_counted(service):
    svc, _, _ = service
    svc.handle_delivery(body(), "sha256=wrong")

    assert svc.snapshot().metrics.rejected == 1


def test_no_secret_means_no_check(tmp_path: Path):
    svc, repo, client = build(tmp_path, secret="")
    try:
        assert svc.handle_delivery(body(), None)[0] == 200
    finally:
        repo.stop()


# ── everything else answers 200 ───────────────────────────────────────


def test_an_unparseable_body_is_ignored_not_retried(service):
    svc, _, _ = service
    payload = b"{not json"

    status, response = svc.handle_delivery(payload, sign(payload))

    assert status == 200
    assert response["action"] == "ignored"


def test_an_event_without_a_message_is_ignored(service):
    svc, _, _ = service
    payload = json.dumps({"event": "session.status", "data": {"status": "ready"}}).encode()

    status, response = svc.handle_delivery(payload, sign(payload))

    assert status == 200
    assert response["action"] == "ignored"


def test_a_failed_send_still_answers_200(tmp_path: Path):
    """A retry would re-run the decision and could deliver twice. This is not
    theoretical: OpenWA 0.7.2 returned HTTP 500 for a message it had already
    delivered, and a retrying client would have sent four copies."""
    svc, repo, _ = build(tmp_path, client=FakeClient(fail_with="boom"))
    try:
        enable(repo, svc)
        payload = body("ping", message_id="m1")

        status, response = svc.handle_delivery(payload, sign(payload))

        assert status == 200
        assert response["action"] == "send_failed"
        assert response["ok"] is False
    finally:
        repo.stop()


# ── the happy path, end to end through the boundary ───────────────────


def test_a_signed_delivery_to_an_enabled_chat_is_answered(service):
    svc, repo, client = service
    enable(repo, svc)
    payload = body("ping", message_id="m1")

    status, response = svc.handle_delivery(payload, sign(payload))

    assert status == 200
    assert response["action"] == "replied"
    assert client.sent == [(CHAT_ID, "pong")]


def test_deliveries_and_replies_are_counted(service):
    svc, repo, _ = service
    enable(repo, svc)
    payload = body("ping", message_id="m1")
    svc.handle_delivery(payload, sign(payload))

    metrics = svc.snapshot().metrics
    assert metrics.deliveries == 2   # the registering one, plus this
    assert metrics.replies_sent == 1


# ── the one control ───────────────────────────────────────────────────


def test_turning_automation_off_keeps_the_history(service):
    """winSpark deleted everything the chat had stored when unticked. Turning
    automation off should stop replies, not destroy the history you turned it
    off in order to read."""
    svc, repo, _ = service
    enable(repo, svc)
    payload = body("ping", message_id="m1")
    svc.handle_delivery(payload, sign(payload))
    before = len(repo.messages_for(CHAT_ID))

    svc.set_chat_automation(CHAT_ID, False)

    assert repo.get_chat(CHAT_ID).automation_enabled is False
    assert len(repo.messages_for(CHAT_ID)) == before


def test_toggling_an_unknown_chat_does_nothing(service):
    svc, _, _ = service
    svc.set_chat_automation("nobody@c.us", True)  # must not raise


def test_the_snapshot_reports_the_stores(service):
    svc, _, _ = service
    snapshot = svc.snapshot()

    assert snapshot.mongo_ok is True
    assert snapshot.json_ok is True
    assert snapshot.listening is False
