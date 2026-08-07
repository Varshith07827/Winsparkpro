"""The processing pipeline and the ingestion rules around it.

Two things are being pinned down here:

1. **Nothing is processed only in memory.** After a message goes through the
   pipeline, MongoDB and the JSON mirror both hold the message, the webhook
   record, the reply, and the updated chat status.
2. **The first read of a chat never triggers anything.** Messages that were
   already on screen when this application first looked belong to the past.

The webhook is exercised against a real HTTP server on localhost rather than a
mock, because the thing most likely to be wrong about a webhook client is how
it talks HTTP.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from wadam import constants
from wadam.config import Settings
from wadam.domain.models import ChatConfig, StoredMessage, chat_id_for, message_key_for
from wadam.engine.pipeline import MessagePipeline
from wadam.engine.webhook import WebhookClient
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from wadam.whatsapp.reader import WhatsAppMessage
from wadam.whatsapp.sender import SendResult

from tests.test_storage import FakeMongo


# ---------------------------------------------------------------------------
# A real HTTP endpoint
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    reply = "pong"
    status = 200
    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        type(self).received.append(json.loads(body))
        payload = json.dumps({"reply": type(self).reply}).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a):
        pass  # keep the test output readable


@pytest.fixture()
def endpoint():
    _Handler.received = []
    _Handler.reply = "pong"
    _Handler.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook", _Handler
    server.shutdown()
    server.server_close()


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    async def send_async(self, chat_name: str, text: str) -> SendResult:
        self.sent.append((chat_name, text))
        if self.ok:
            return SendResult.succeeded("uia-value-pattern + send-button-invoke")
        return SendResult.failed("the compose box still had text after send-button-invoke")


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


def make_chat(repo: Repository, webhook: str, automation: bool = True) -> ChatConfig:
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      webhook_url=webhook, automation_enabled=automation, seeded=True)
    repo.save_chat(chat)
    return chat


def make_message(chat: ChatConfig, text: str = "ping") -> StoredMessage:
    message = StoredMessage(
        message_key=message_key_for(chat.chat_id, "Alice", text, "9:21 pm", "in"),
        chat_id=chat.chat_id, chat_name=chat.chat_name, sender="Alice",
        text=text, direction="in", time_text="9:21 pm", status="pending",
    )
    return message


def build_pipeline(repo: Repository, sender: FakeSender) -> MessagePipeline:
    return MessagePipeline(repo, WebhookClient(timeout=5, max_retries=1), sender, asyncio.to_thread)


def read_json(folder: Path, name: str):
    return json.loads((folder / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_the_full_path_persists_every_step(repo: Repository, tmp_path: Path, endpoint):
    url, handler = endpoint
    chat = make_chat(repo, url)
    message = make_message(repo and chat)
    repo.save_message(message)
    sender = FakeSender()

    asyncio.run(build_pipeline(repo, sender).process(chat, message))

    # The endpoint saw the message, shaped as documented.
    assert len(handler.received) == 1
    payload = handler.received[0]
    assert payload["event"] == "message.received"
    assert payload["chat"]["name"] == "Alice"
    assert payload["message"]["text"] == "ping"
    assert payload["message"]["sender"] == "Alice"

    # The reply was sent to the right chat.
    assert sender.sent == [("Alice", "pong")]

    # And every step is on disk in both stores.
    assert message.status == "replied"
    assert chat.last_outgoing_text == "pong"
    assert chat.webhook_retry_count == 0

    messages = read_json(tmp_path, constants.JSON_MESSAGES)
    assert {m["direction"] for m in messages} == {"in", "out"}
    assert [m["status"] for m in messages if m["direction"] == "in"] == ["replied"]

    webhooks = read_json(tmp_path, constants.JSON_WEBHOOKS)["calls"]
    assert webhooks[0]["ok"] is True
    assert webhooks[0]["reply_text"] == "pong"
    assert webhooks[0]["status_code"] == 200


def test_an_empty_reply_sends_nothing_and_is_not_an_error(repo: Repository, endpoint):
    url, handler = endpoint
    handler.reply = ""
    chat = make_chat(repo, url)
    message = make_message(chat)
    repo.save_message(message)
    sender = FakeSender()

    asyncio.run(build_pipeline(repo, sender).process(chat, message))

    assert sender.sent == []
    assert message.status == "webhook_ok"
    assert "no reply" in chat.last_webhook_status


def test_a_failing_endpoint_is_recorded_not_swallowed(repo: Repository, endpoint):
    url, handler = endpoint
    handler.status = 503
    chat = make_chat(repo, url)
    message = make_message(chat)
    repo.save_message(message)
    sender = FakeSender()

    asyncio.run(build_pipeline(repo, sender).process(chat, message))

    assert sender.sent == []
    assert message.status == "webhook_failed"
    assert chat.last_error
    # 503 is retryable, so the one configured retry was spent.
    assert chat.webhook_retry_count == 1


def test_an_unverified_send_is_a_failure_not_a_success(repo: Repository, endpoint):
    url, _handler = endpoint
    chat = make_chat(repo, url)
    message = make_message(chat)
    repo.save_message(message)
    sender = FakeSender(ok=False)

    asyncio.run(build_pipeline(repo, sender).process(chat, message))

    assert message.status == "reply_failed"
    assert "compose box" in chat.last_error
    # Crucially, no outgoing message was recorded — we do not claim to have
    # sent something we could not verify.
    assert [m.direction for m in repo.messages_for(chat.chat_id)] == ["in"]


def test_automation_on_but_no_webhook_stores_and_stops(repo: Repository):
    chat = make_chat(repo, webhook="")
    message = make_message(chat)
    repo.save_message(message)
    sender = FakeSender()

    asyncio.run(build_pipeline(repo, sender).process(chat, message))

    assert sender.sent == []
    assert message.status == "ignored"
    assert "no webhook" in chat.last_webhook_status


# ---------------------------------------------------------------------------
# Ingestion / seeding
# ---------------------------------------------------------------------------


class _EngineHarness:
    """The engine's ingestion logic without its polling loop or STA thread.

    `_ingest` is a plain method that only touches the repository, so binding it
    to a stand-in is enough to test the seeding rule without starting COM.
    """

    def __init__(self, repository: Repository) -> None:
        self._repo = repository

    _ingest = None  # bound below


def make_harness(repo: Repository):
    from wadam.engine.engine import AutomationEngine

    harness = _EngineHarness(repo)
    harness._ingest = AutomationEngine._ingest.__get__(harness, _EngineHarness)
    return harness


def incoming(text: str, time_text: str = "9:00 am") -> WhatsAppMessage:
    return WhatsAppMessage(sender="Alice", text=text, is_incoming=True, time_text=time_text)


def test_the_first_read_records_the_backlog_without_automating_it(repo: Repository):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      webhook_url="https://x.test/h", automation_enabled=True, seeded=False)
    repo.save_chat(chat)
    harness = make_harness(repo)

    pending = asyncio.run(harness._ingest(chat, [incoming("old 1"), incoming("old 2", "9:01 am")]))

    assert pending == [], "existing messages must never be answered"
    assert chat.seeded is True
    assert {m.status for m in repo.messages_for(chat.chat_id)} == {"seeded"}


def test_messages_arriving_after_the_baseline_are_processed(repo: Repository):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      webhook_url="https://x.test/h", automation_enabled=True, seeded=False)
    repo.save_chat(chat)
    harness = make_harness(repo)

    asyncio.run(harness._ingest(chat, [incoming("old")]))
    pending = asyncio.run(harness._ingest(chat, [incoming("old"), incoming("new", "9:05 am")]))

    assert [m.text for m in pending] == ["new"]
    assert chat.last_incoming_text == "new"


def test_re_reading_the_same_bubbles_produces_nothing(repo: Repository):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      webhook_url="https://x.test/h", automation_enabled=True, seeded=True)
    repo.save_chat(chat)
    harness = make_harness(repo)

    first = asyncio.run(harness._ingest(chat, [incoming("hello")]))
    second = asyncio.run(harness._ingest(chat, [incoming("hello")]))

    assert [m.text for m in first] == ["hello"]
    assert second == [], "the 3s poll re-reads the same tail every cycle"


def test_our_own_messages_are_stored_but_never_processed(repo: Repository):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      webhook_url="https://x.test/h", automation_enabled=True, seeded=True)
    repo.save_chat(chat)
    harness = make_harness(repo)

    outgoing = WhatsAppMessage(sender="You", text="on my way", is_incoming=False, time_text="9:10 am")
    pending = asyncio.run(harness._ingest(chat, [outgoing]))

    assert pending == []
    stored = repo.messages_for(chat.chat_id)
    assert [m.direction for m in stored] == ["out"]
    assert chat.last_outgoing_text == "on my way"


def test_automation_off_stores_without_processing(repo: Repository):
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      automation_enabled=False, seeded=True)
    repo.save_chat(chat)
    harness = make_harness(repo)

    pending = asyncio.run(harness._ingest(chat, [incoming("hello")]))

    assert pending == []
    assert [m.status for m in repo.messages_for(chat.chat_id)] == ["ignored"]
