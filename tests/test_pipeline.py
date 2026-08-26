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
from wadam.openwa import InboundMessage, SendError
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.fakes import FakeMongo

CHAT_ID = "216298915164281@lid"


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
        sender="259094657142792@lid",
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


def build(repo, reply="pong", cooldown=60.0, answer_groups=False, client=None):
    client = client or FakeClient()
    pipeline = MessagePipeline(
        repository=repo,
        client=client,
        reply_fn=(reply if callable(reply) else (lambda m, c: reply)),
        cooldown=Cooldown(cooldown),
        answer_groups=answer_groups,
    )
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
    answers = {"speak": "here", "quiet": None}
    pipeline, client = build(repo, reply=lambda m, c: answers.get(m.text))
    pipeline.process(message(message_id="m0"))
    enable(repo)

    pipeline.process(message("quiet", message_id="m1"))
    outcome = pipeline.process(message("speak", message_id="m2"))

    assert outcome.action == "replied"
    assert len(client.sent) == 1


# ── silence and failure ───────────────────────────────────────────────


def test_no_reply_wanted_is_a_success(repo):
    pipeline, client = build(repo, reply=None)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    outcome = pipeline.process(message("anything", message_id="m1"))

    assert outcome.ok is True
    assert outcome.reason == "no reply wanted"
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


def test_a_raising_reply_function_does_not_escape(repo):
    def boom(msg, chat):
        raise ValueError("bad rule")

    pipeline, client = build(repo, reply=boom)
    pipeline.process(message(message_id="m0"))
    enable(repo)

    outcome = pipeline.process(message("ping", message_id="m1"))

    assert outcome.action == "ignored"
    assert client.sent == []


def test_media_arrives_with_its_kind_and_no_text(repo):
    pipeline, _ = build(repo)

    pipeline.process(message(text="", media_kind="image", message_id="m1"))

    stored = repo.messages_for(CHAT_ID)[-1]
    assert stored.media_kind == "image"
    assert stored.text == ""
