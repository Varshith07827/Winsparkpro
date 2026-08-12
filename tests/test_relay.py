"""The relay — winSpark's fetch-webhook model.

The interesting problem is deduplication. A GET is not inherently a destructive
read, so an endpoint that keeps returning the same message must not produce the
same message being sent over and over — while an endpoint that legitimately
wants to send "OK" twice must still be able to.

winSpark solved the first half by hashing content and refusing forever, which
also blocks the second half. These tests pin down the two-rule replacement.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from wadam.config import Settings, load_settings
from wadam.domain.models import ChatConfig, chat_id_for
from wadam.engine.relay import RelayService
from wadam.engine.webhook import RelayMessage, WebhookClient, parse_relay_messages
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository

from tests.test_storage import FakeMongo


# ---------------------------------------------------------------------------
# Parsing what an endpoint offers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,expected", [
    ('{"message": "Hello Varshith"}', ["Hello Varshith"]),
    ('{"text": "Hello"}', ["Hello"]),
    ('{"content": "Hello"}', ["Hello"]),
    ('{"body": "Hello"}', ["Hello"]),
    ('{"msg": "Hello"}', ["Hello"]),
    ('{"reply": "Hello"}', ["Hello"]),
    ('{"data": {"message": "Hello"}}', ["Hello"]),
    ('{"result": {"text": "Hello"}}', ["Hello"]),
    ('"Hello"', ["Hello"]),
    ("Hello Varshith", ["Hello Varshith"]),      # plain text IS the message
])
def test_the_shapes_winspark_understood_still_work(body, expected):
    assert [m.text for m in parse_relay_messages(body)] == expected


@pytest.mark.parametrize("body", ["", "   ", "{}", '{"message": ""}', "[]", "null"])
def test_nothing_waiting_is_not_a_message(body):
    assert parse_relay_messages(body) == []


def test_an_array_yields_every_message_not_just_the_first():
    """An endpoint answering a burst with three objects means three messages.
    Quietly delivering one of them is how a backlog disappears."""
    body = json.dumps([
        {"id": "1", "message": "first"},
        {"id": "2", "message": "second"},
        {"id": "3", "message": "third"},
    ])
    messages = parse_relay_messages(body)
    assert [(m.external_id, m.text) for m in messages] == [
        ("1", "first"), ("2", "second"), ("3", "third")
    ]


def test_ids_are_picked_up_from_any_of_their_names():
    for key in ("id", "external_id", "externalId", "message_id", "messageId", "uid"):
        parsed = parse_relay_messages(json.dumps({key: "abc", "message": "hi"}))
        assert parsed[0].external_id == "abc", key


def test_an_unquoted_numeric_id_is_accepted():
    assert parse_relay_messages('{"id": 9423, "message": "hi"}')[0].external_id == "9423"


def test_an_envelope_id_covers_a_single_nested_message():
    parsed = parse_relay_messages('{"id": "x1", "data": {"message": "hi"}}')
    assert parsed[0].external_id == "x1"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0,
                        relay_enabled=True)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    service = RelayService(repository, WebhookClient(timeout=5), asyncio.to_thread)
    yield service, repository
    repository.stop()


def make_chat(repo: Repository, **kwargs) -> ChatConfig:
    chat = ChatConfig(chat_id=chat_id_for("Alice"), chat_name="Alice",
                      webhook_url="https://x.test/hook", automation_enabled=True, **kwargs)
    repo.save_chat(chat)
    return chat


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    async def send_async(self, chat_name: str, text: str):
        from wadam.whatsapp.sender import SendResult

        self.sent.append((chat_name, text))
        return SendResult.succeeded("test") if self.ok else SendResult.failed("not verified")


def test_a_non_dequeuing_endpoint_sends_once_then_goes_quiet(relay):
    """The failure this rule exists to prevent: an endpoint that keeps
    answering with the same text must not send it every few seconds."""
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()
    message = RelayMessage(text="Hello Varshith")

    send, _ = service.should_send(chat, message)
    assert send
    asyncio.run(service.deliver(chat, message, sender))

    for _ in range(5):
        send, reason = service.should_send(chat, message)
        assert not send
        assert "identical to the last relayed" in reason
    assert sender.sent == [("Alice", "Hello Varshith")]


def test_an_empty_poll_clears_the_duplicate_guard(relay):
    """A dequeuing endpoint must be able to send the same text twice.

    This is the case that was broken: queue "Hello", have it delivered, queue
    "Hello" again later, and the consecutive-repeat rule dropped the second one
    even though the endpoint had reported "nothing waiting" in between."""
    from wadam.engine.relay import RelayPoll

    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()
    message = RelayMessage(text="Hello Varshith")

    asyncio.run(service.deliver(chat, message, sender))
    # Straight away, with nothing in between, it is still a duplicate.
    assert service.should_send(chat, message)[0] is False

    # The endpoint now says it has nothing — it dequeues.
    asyncio.run(service.record_poll(chat, RelayPoll(chat_id=chat.chat_id,
                                                    status="200 OK · nothing waiting")))
    assert service.should_send(chat, message)[0] is True
    asyncio.run(service.deliver(chat, message, sender))
    assert sender.sent == [("Alice", "Hello Varshith"), ("Alice", "Hello Varshith")]


def test_a_dequeuing_endpoint_may_repeat_itself_back_to_back(relay):
    """The failure that cost four of eight live messages: a queue holding two
    identical messages hands them over on consecutive polls, with no empty poll
    in between, and the second was suppressed. Suppressing it did not defer it
    — the endpoint had already removed it, so it was destroyed."""
    from wadam.engine.relay import RelayPoll

    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()
    message = RelayMessage(text="Hello Varshith")

    # One empty poll is all it takes to learn that this endpoint dequeues.
    asyncio.run(service.record_poll(chat, RelayPoll(chat_id=chat.chat_id, status="200 OK")))
    assert chat.relay_dequeues is True

    # Now three identical messages arrive back to back, no empty poll between.
    for _ in range(3):
        assert service.should_send(chat, message)[0] is True
        asyncio.run(service.deliver(chat, message, sender))
    assert sender.sent == [("Alice", "Hello Varshith")] * 3


def test_the_dequeue_flag_survives_a_restart(relay):
    """It lives on the chat, so a restart does not re-arm a guard that was
    already proved unnecessary — which is what made a five-minute window
    deliver nothing at all."""
    from wadam.engine.relay import RelayPoll

    service, repo = relay
    chat = make_chat(repo)
    asyncio.run(service.record_poll(chat, RelayPoll(chat_id=chat.chat_id, status="200 OK")))
    repo.save_chat(chat)

    reloaded = repo.get_chat(chat.chat_id)
    assert reloaded.relay_dequeues is True


def test_a_failed_poll_does_not_prove_dequeuing(relay):
    from wadam.engine.relay import RelayPoll

    service, repo = relay
    chat = make_chat(repo)
    asyncio.run(service.record_poll(chat, RelayPoll(chat_id=chat.chat_id, error="HTTP 503")))
    assert chat.relay_dequeues is False


def test_a_queue_document_id_beats_a_destination_id():
    """Their queue returns {"_id": <message>, "id": <destination>}. Reading `id`
    would take the contact number — identical on every message — and suppress
    everything after the first."""
    from wadam.engine.webhook import parse_relay_messages as parse

    parsed = parse('{"_id":"6a75121b","id":"9423","message":"Hello Varshith"}')
    assert parsed[0].external_id == "6a75121b"


def test_a_never_empty_endpoint_is_still_silenced(relay):
    """Rule 3 must not undo rule 2: an endpoint that never reports empty keeps
    being suppressed, which is the whole reason the guard exists."""
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()
    message = RelayMessage(text="Hello")

    asyncio.run(service.deliver(chat, message, sender))
    for _ in range(10):        # ten polls, all offering the same thing
        assert service.should_send(chat, message)[0] is False
    assert len(sender.sent) == 1


def test_a_failed_poll_does_not_clear_the_guard(relay):
    """An endpoint that is down has not told us anything about its queue, so a
    503 must not be read as "nothing waiting"."""
    from wadam.engine.relay import RelayPoll

    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()
    message = RelayMessage(text="Hello")
    asyncio.run(service.deliver(chat, message, sender))

    asyncio.run(service.record_poll(chat, RelayPoll(chat_id=chat.chat_id, error="HTTP 503")))
    assert service.should_send(chat, message)[0] is False


def test_a_changed_message_goes_out(relay):
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()

    asyncio.run(service.deliver(chat, RelayMessage(text="first"), sender))
    send, _ = service.should_send(chat, RelayMessage(text="second"))
    assert send


def test_the_same_text_can_be_sent_again_once_something_changed(relay):
    """winSpark hashed content and refused forever, so a chat could never be
    sent the same text twice. "OK" is a reasonable thing to say twice."""
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()

    asyncio.run(service.deliver(chat, RelayMessage(text="OK"), sender))
    asyncio.run(service.deliver(chat, RelayMessage(text="on my way"), sender))

    send, _ = service.should_send(chat, RelayMessage(text="OK"))
    assert send, "the value changed in between — this is a new message"


def test_an_id_lets_the_same_text_go_twice_immediately(relay):
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()

    asyncio.run(service.deliver(chat, RelayMessage(text="OK", external_id="a1"), sender))
    send, _ = service.should_send(chat, RelayMessage(text="OK", external_id="a2"))
    assert send, "a different id is the endpoint saying these are distinct"


def test_a_repeated_id_is_refused_even_after_other_messages(relay):
    """An id is authoritative and permanent — unlike the content rule, it is not
    reset by an intervening message."""
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()

    asyncio.run(service.deliver(chat, RelayMessage(text="first", external_id="a1"), sender))
    asyncio.run(service.deliver(chat, RelayMessage(text="second", external_id="a2"), sender))

    send, reason = service.should_send(chat, RelayMessage(text="first", external_id="a1"))
    assert not send
    assert "already relayed" in reason


def test_a_failed_send_is_not_recorded_as_relayed(relay):
    """So the next poll offers it again. An endpoint that has dequeued it will
    not offer it — and that is the endpoint's call, not ours."""
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender(ok=False)
    message = RelayMessage(text="Hello")

    assert asyncio.run(service.deliver(chat, message, sender)) is False
    assert chat.last_relay_text == "", "a failed send must not become the dedup guard"
    send, _ = service.should_send(chat, message)
    assert send
    assert repo.messages_for(chat.chat_id) == [], "nothing is claimed as sent"


def test_a_delivered_message_is_persisted_like_any_other(relay):
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()

    asyncio.run(service.deliver(chat, RelayMessage(text="Hello", external_id="a1"), sender))

    stored = repo.messages_for(chat.chat_id)
    assert [(m.direction, m.text, m.origin, m.external_ref) for m in stored] == [
        ("out", "Hello", "relay", "a1")
    ]
    assert chat.last_outgoing_text == "Hello"
    assert "relayed via" in chat.last_relay_status
    assert any(e.event == "relay.sent" for e in repo.recent_logs())


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_only_automated_chats_with_a_webhook_are_polled(relay):
    service, repo = relay
    assert service.is_eligible(ChatConfig(automation_enabled=True, webhook_url="https://x/h"))
    assert not service.is_eligible(ChatConfig(automation_enabled=False, webhook_url="https://x/h"))
    assert not service.is_eligible(ChatConfig(automation_enabled=True, webhook_url=""))
    assert not service.is_eligible(ChatConfig(automation_enabled=True, webhook_url="   "))


# ---------------------------------------------------------------------------
# Against a real HTTP endpoint
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    queue: list[str] = []
    status = 200
    gets = 0

    def do_GET(self):  # noqa: N802
        type(self).gets += 1
        payload = (type(self).queue.pop(0) if type(self).queue else "{}").encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a):
        pass


@pytest.fixture()
def endpoint():
    _Handler.queue = []
    _Handler.status = 200
    _Handler.gets = 0
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/hook", _Handler
    server.shutdown()
    server.server_close()


def test_a_real_poll_reads_the_queue(relay, endpoint):
    service, repo = relay
    url, handler = endpoint
    handler.queue = [json.dumps({"id": "1", "message": "Hello Varshith"})]
    chat = make_chat(repo)
    chat.webhook_url = url

    poll = asyncio.run(service.poll(chat))

    assert poll.ok
    assert [(m.external_id, m.text) for m in poll.messages] == [("1", "Hello Varshith")]
    assert "1 waiting" in poll.status


def test_an_empty_queue_is_a_successful_poll(relay, endpoint):
    service, repo = relay
    url, _handler = endpoint
    chat = make_chat(repo)
    chat.webhook_url = url

    poll = asyncio.run(service.poll(chat))
    assert poll.ok and poll.messages == ()
    assert "nothing waiting" in poll.status


def test_a_failing_endpoint_is_reported_not_retried(relay, endpoint):
    """Not retried on purpose: this runs again in a moment anyway, and
    retrying inside a call that is about to repeat only multiplies load on an
    endpoint that is already struggling."""
    service, repo = relay
    url, handler = endpoint
    handler.status = 503
    chat = make_chat(repo)
    chat.webhook_url = url

    poll = asyncio.run(service.poll(chat))

    assert not poll.ok
    assert handler.gets == 1
    asyncio.run(service.record_poll(chat, poll))
    assert "poll failed" in chat.last_relay_status


def test_an_unreachable_endpoint_does_not_raise(relay):
    service, repo = relay
    chat = make_chat(repo)
    chat.webhook_url = "http://127.0.0.1:9/nothing-here"
    poll = asyncio.run(service.poll(chat))
    assert not poll.ok and poll.error


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def write_env(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "MONGODB_URI=mongodb://localhost:27017\nDATABASE_NAME=wadam\n"
        f"JSON_BACKUP_FOLDER={tmp_path / 'backup'}\n{extra}\n", encoding="utf-8")
    return path


def test_the_relay_is_on_when_the_file_does_not_mention_it(tmp_path: Path):
    # The two-key .env first-run setup writes never mentions RELAY_ENABLED, and
    # that file has to produce a working outbound path, so silence cannot mean
    # "no way to send".
    settings = load_settings(write_env(tmp_path, ""))
    assert settings.relay_enabled is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_the_relay_can_still_be_switched_off(tmp_path: Path, value):
    assert load_settings(write_env(tmp_path, f"RELAY_ENABLED={value}")).relay_enabled is False


def test_the_two_key_setup_file_can_send(tmp_path: Path):
    """The exact file `first_run.write_env` produces, loaded the way a frozen
    build loads it. A built EXE was reading one of these, listening on nothing
    because API_PORT is absent, and polling nothing because the relay was off —
    so it had no outbound path in either direction."""
    from wadam.ui.first_run import env_text

    path = tmp_path / ".env"
    path.write_text(env_text("mongodb://localhost:27017",
                             "https://noteify.org/ntext/whook/?{phone_number}"),
                    encoding="utf-8")
    settings = load_settings(path)

    assert settings.api_port == 0, "nothing writes API_PORT, so there is no listener"
    assert settings.relay_enabled is True, "so the relay has to be the way out"
    assert settings.webhook_template.endswith("?{phone_number}")


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_the_relay_can_be_switched_on(tmp_path: Path, value):
    assert load_settings(write_env(tmp_path, f"RELAY_ENABLED={value}")).relay_enabled is True


def test_the_poll_interval_has_a_floor(tmp_path: Path):
    from wadam.config import ConfigError

    with pytest.raises(ConfigError):
        load_settings(write_env(tmp_path, "RELAY_ENABLED=true\nRELAY_POLL_INTERVAL=0.1"))


# ---------------------------------------------------------------------------
# The record of what was sent
# ---------------------------------------------------------------------------


def test_every_send_gets_its_own_record_even_when_the_text_repeats(relay):
    """Found by a live relay run: three messages went out and only two were
    stored, because the third repeated the first. A message we SEND is a known
    distinct event, so its key carries the moment rather than only the text."""
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()

    asyncio.run(service.deliver(chat, RelayMessage(text="OK"), sender))
    asyncio.run(service.deliver(chat, RelayMessage(text="on my way"), sender))
    asyncio.run(service.deliver(chat, RelayMessage(text="OK"), sender))

    assert [m.text for m in repo.messages_for(chat.chat_id)] == ["OK", "on my way", "OK"]


def test_our_own_message_read_back_from_whatsapp_is_not_stored_twice(relay):
    """Everything we send is read back by a later poll as an outgoing bubble.
    Without this, each automated reply appears twice in the record."""
    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()
    asyncio.run(service.deliver(chat, RelayMessage(text="Hello Varshith"), sender))

    assert repo.recently_originated(chat.chat_id, "Hello Varshith") is True
    # Whitespace differences in the read-back must not defeat the match.
    assert repo.recently_originated(chat.chat_id, "Hello   Varshith") is True
    assert repo.recently_originated(chat.chat_id, "something else") is False


def test_a_message_the_user_typed_is_not_mistaken_for_ours(relay):
    service, repo = relay
    chat = make_chat(repo)
    # Nothing originated by us at all — a bubble read from WhatsApp must be
    # stored, not silently dropped as a duplicate of a message we never sent.
    assert repo.recently_originated(chat.chat_id, "typed by hand") is False


def test_an_old_send_no_longer_suppresses_a_read_back(relay):
    from datetime import timedelta

    from wadam.domain.models import utcnow

    service, repo = relay
    chat = make_chat(repo)
    sender = FakeSender()
    asyncio.run(service.deliver(chat, RelayMessage(text="Hello"), sender))

    stored = repo.messages_for(chat.chat_id)[0]
    stored.detected_at = utcnow() - timedelta(hours=2)
    assert repo.recently_originated(chat.chat_id, "Hello") is False
