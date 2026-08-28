"""What happens to one inbound message.

Nothing here touches WhatsApp, OpenWA, or the network — the send client is a
fake that records calls. That is the point of keeping the pipeline free of
HTTP: the rules that decide whether a message is answered are the part worth
testing, and they test in milliseconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, MessageStatus
from wadam.engine.guards import Cooldown
from wadam.engine.pipeline import MessagePipeline
from wadam.engine.webhook import WebhookOutcome
from wadam.openwa import InboundMessage, SendError
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.fakes import FakeMongo

CHAT_ID = "111111111111111@lid"


class FakeClient:
    """Records sends instead of making them. Can be told to fail."""

    def __init__(self, fail_with: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_with = fail_with

    def send_text(self, chat_id: str, text: str) -> dict:
        if self._fail_with:
            raise SendError(self._fail_with, status=500)
        self.sent.append((chat_id, text))
        return {"ok": True}


def message(text: str = "hi", chat_id: str = CHAT_ID, message_id: str = "m1",
            outgoing: bool = False, group: bool = False,
            media_kind: str = "") -> InboundMessage:
    return InboundMessage(
        chat_id=chat_id,
        chat_name="Alice",
        message_id=message_id,
        text=text,
        sender="333333333333333@lid",
        media_kind=media_kind,
        is_group=group,
        is_outgoing=outgoing,
        raw={},
    )


@pytest.fixture()
def repo(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    yield repository
    repository.stop()


class FakeWebhook:
    """Stands in for the endpoint. Records what it was asked, answers to order."""

    def __init__(self, reply="pong", ok=True, error="") -> None:
        self._reply = reply
        self._ok = ok
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    def call(self, url, payload, sleep=None):
        self.calls.append((url, payload))
        if not self._ok:
            return WebhookOutcome(False, "502", error=self._error or "HTTP 502", attempts=4)
        return WebhookOutcome(True, "200 OK", reply_text=self._reply or "", attempts=1)


def build(repo, reply="pong", cooldown=60.0, answer_groups=False, client=None,
          webhook=None, default_webhook="https://example.test/hook"):
    client = client or FakeClient()
    webhook = webhook or FakeWebhook(reply)
    pipeline = MessagePipeline(
        repository=repo,
        client=client,
        webhook=webhook,
        cooldown=Cooldown(cooldown),
        answer_groups=answer_groups,
        default_webhook=default_webhook,
    )
    pipeline.webhook = webhook          # for assertions
    return pipeline, client


def enable(repo, chat_id=CHAT_ID):
    chat = repo.get_chat(chat_id)
    chat.automation_enabled = True
    repo.save_chat(chat)


# ── registration ──────────────────────────────────────────────────────


def test_an_unknown_chat_is_registered_with_automation_off(repo):
    pipeline, client = build(repo)

    outcome = pipeline.process(message())

    chat = repo.get_chat(CHAT_ID)
    assert chat is not None
    assert chat.automation_enabled is False
    assert outcome.action == "skipped"
    assert client.sent == []


def test_a_renamed_contact_stays_the_same_chat(repo):
    """The id comes from OpenWA and is durable. winSpark hashed the display
    name, so a rename silently created a second chat with its own settings."""
    pipeline, _ = build(repo)
    pipeline.process(message(message_id="m1"))
    enable(repo)

    renamed = message(message_id="m2")
    object.__setattr__(renamed, "chat_name", "Alice Cooper")
    pipeline.process(renamed)

    assert len(repo.list_chats()) == 1
    assert repo.get_chat(CHAT_ID).chat_name == "Alice Cooper"
    assert repo.get_chat(CHAT_ID).automation_enabled is True


# ── the message is stored before anything is decided ──────────────────


def test_an_incoming_message_is_stored_even_when_automation_is_off(repo):
    pipeline, _ = build(repo)

    pipeline.process(message("hello"))

    stored = repo.messages_for(CHAT_ID)
    assert [m.text for m in stored] == ["hello"]
    assert stored[0].direction == "in"


def test_a_reply_is_stored_alongside_the_message_it_answered(repo):
    pipeline, client = build(repo)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    pipeline.process(message("ping", message_id="m1"))

    texts = [(m.direction, m.text) for m in repo.messages_for(CHAT_ID)]
    assert ("in", "ping") in texts
    assert ("out", "pong") in texts
    assert client.sent == [(CHAT_ID, "pong")]


def test_a_message_in_an_automation_off_chat_is_marked_collected(repo):
    """Not left PENDING. It was stored and deliberately not answered, which is
    a decision — a status that never advances makes every such message look
    like one the process died halfway through."""
    pipeline, _ = build(repo)

    pipeline.process(message("hello"))

    assert repo.messages_for(CHAT_ID)[0].status == MessageStatus.COLLECTED


def test_a_skipped_group_message_is_marked_collected(repo):
    pipeline, _ = build(repo)
    pipeline.process(message(message_id="m0", group=True))
    enable(repo)

    pipeline.process(message("hi", message_id="m1", group=True))

    statuses = {m.status for m in repo.messages_for(CHAT_ID)}
    assert MessageStatus.PENDING not in statuses


# ── rule: loop protection ─────────────────────────────────────────────


def test_an_outgoing_message_is_never_answered(repo):
    """The account's own traffic. Answering it is a loop with extra steps."""
    pipeline, client = build(repo)

    outcome = pipeline.process(message(outgoing=True))

    assert outcome.action == "skipped"
    assert outcome.reason == "outgoing message"
    assert client.sent == []


def test_group_chats_are_ignored_by_default(repo):
    pipeline, client = build(repo)
    pipeline.process(message(message_id="m0", group=True))
    enable(repo)

    outcome = pipeline.process(message(message_id="m1", group=True))

    assert outcome.reason == "group chat"
    assert client.sent == []


def test_group_chats_are_answered_when_enabled(repo):
    pipeline, client = build(repo, answer_groups=True)
    pipeline.process(message(message_id="m0", group=True))
    enable(repo)

    outcome = pipeline.process(message(message_id="m1", group=True))

    assert outcome.action == "replied"
    assert client.sent == [(CHAT_ID, "pong")]


# ── rule: deduplication ───────────────────────────────────────────────


def test_a_retried_delivery_is_not_answered_twice(repo):
    pipeline, client = build(repo)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    pipeline.process(message("ping", message_id="m1"))
    outcome = pipeline.process(message("ping", message_id="m1"))

    assert outcome.reason == "duplicate delivery"
    assert len(client.sent) == 1


def test_the_same_text_from_two_messages_is_not_a_duplicate(repo):
    """winSpark keyed on a hash of the content, so two people genuinely saying
    "ok" a minute apart looked like one message read twice.

    Cooldown off, so the only thing that could suppress the second send is
    deduplication.
    """
    pipeline, client = build(repo, cooldown=0)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    pipeline.process(message("ok", message_id="m1"))
    pipeline.process(message("ok", message_id="m2"))

    assert len(client.sent) == 2


# ── rule: cooldown ────────────────────────────────────────────────────


def test_a_second_reply_to_the_same_chat_is_held_off(repo):
    pipeline, client = build(repo)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    pipeline.process(message("ping", message_id="m1"))
    outcome = pipeline.process(message("ping", message_id="m2"))

    assert "cooldown" in outcome.reason
    assert len(client.sent) == 1


def test_a_zero_cooldown_disables_it(repo):
    pipeline, client = build(repo, cooldown=0)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    pipeline.process(message("ping", message_id="m1"))
    pipeline.process(message("ping", message_id="m2"))

    assert len(client.sent) == 2


def test_an_ignored_message_does_not_consume_the_cooldown(repo):
    """A message the reply function stayed silent on must not silence the next
    one that mattered."""
    class Selective(FakeWebhook):
        def call(self, url, payload, sleep=None):
            text = payload["message"]["text"]
            return WebhookOutcome(True, "200 OK",
                                  reply_text="here" if text == "speak" else "")

    pipeline, client = build(repo, webhook=Selective())
    pipeline.process(message(message_id="m0"))
    enable(repo)

    pipeline.process(message("quiet", message_id="m1"))
    outcome = pipeline.process(message("speak", message_id="m2"))

    assert outcome.action == "replied"
    assert len(client.sent) == 1


# ── silence and failure ───────────────────────────────────────────────


def test_no_reply_wanted_is_a_success(repo):
    pipeline, client = build(repo, reply="")
    pipeline.process(message(message_id="m0"))
    enable(repo)

    outcome = pipeline.process(message("anything", message_id="m1"))

    assert outcome.ok is True
    assert outcome.reason == "endpoint sent no reply"
    assert client.sent == []
    assert repo.messages_for(CHAT_ID)[-1].status == MessageStatus.COLLECTED


def test_a_failed_send_is_recorded_against_the_message(repo):
    client = FakeClient(fail_with="engine not ready")
    pipeline, _ = build(repo, client=client)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    outcome = pipeline.process(message("ping", message_id="m1"))

    assert outcome.action == "send_failed"
    assert outcome.ok is False
    failed = [m for m in repo.messages_for(CHAT_ID) if m.status == MessageStatus.FAILED]
    assert failed and "engine not ready" in failed[0].error


def test_a_failing_endpoint_is_recorded_and_nothing_is_sent(repo):
    pipeline, client = build(repo, webhook=FakeWebhook(ok=False, error="HTTP 502"))
    pipeline.process(message(message_id="m0"))
    enable(repo)

    outcome = pipeline.process(message("ping", message_id="m1"))

    assert outcome.action == "webhook_failed"
    assert client.sent == []
    chat = repo.get_chat(CHAT_ID)
    assert chat.last_webhook_status == "502"
    assert "502" in chat.last_error


def test_a_chat_with_no_webhook_and_no_default_is_skipped(repo):
    pipeline, client = build(repo, default_webhook="")
    pipeline.process(message(message_id="m0"))
    enable(repo)

    outcome = pipeline.process(message("ping", message_id="m1"))

    assert outcome.reason == "no webhook configured for this chat"
    assert client.sent == []


def test_a_per_chat_url_wins_over_the_default(repo):
    pipeline, _ = build(repo, default_webhook="https://default.test/hook")
    pipeline.process(message(message_id="m0"))
    chat = repo.get_chat(CHAT_ID)
    chat.automation_enabled = True
    chat.webhook_url = "https://this-chat.test/hook"
    repo.save_chat(chat)

    pipeline.process(message("ping", message_id="m1"))

    assert pipeline.webhook.calls[0][0] == "https://this-chat.test/hook"


def test_the_payload_carries_the_chat_and_the_message(repo):
    """winSpark's envelope. An endpoint written against it still works."""
    pipeline, _ = build(repo)
    pipeline.process(message(message_id="m0"))
    chat = repo.get_chat(CHAT_ID)
    chat.automation_enabled = True
    chat.contact_name = "Priya Menon"
    chat.phone_number = "919876543210"
    repo.save_chat(chat)

    pipeline.process(message("are you there?", message_id="m1"))

    _, payload = pipeline.webhook.calls[0]
    assert payload["event"] == "message.received"
    assert payload["chat"]["id"] == CHAT_ID
    assert payload["chat"]["name"] == "Priya Menon"
    assert payload["chat"]["phone"] == "919876543210"
    assert payload["message"]["text"] == "are you there?"
    assert payload["message"]["key"] == "m1"


def test_media_arrives_with_its_kind_and_no_text(repo):
    pipeline, _ = build(repo)

    pipeline.process(message(text="", media_kind="image", message_id="m1"))

    stored = repo.messages_for(CHAT_ID)[-1]
    assert stored.media_kind == "image"
    assert stored.text == ""
