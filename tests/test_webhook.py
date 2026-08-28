"""Calling the endpoint: what counts as a reply, and what is worth retrying.

The leniency here is deliberate. The endpoint should be as simple as the person
writing it likes, and a bridge that understood only one response shape would be
a bridge you had to write code for.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from wadam.domain.models import ChatConfig, StoredMessage
from wadam.engine.webhook import WebhookClient, build_payload, parse_reply


# ── understanding the answer ──────────────────────────────────────────


@pytest.mark.parametrize("body, expected", [
    ('{"reply": "Confirmed"}', "Confirmed"),
    ('{"message": "Confirmed"}', "Confirmed"),
    ('{"text": "Confirmed"}', "Confirmed"),
    ('{"body": "Confirmed"}', "Confirmed"),
    ('{"answer": "Confirmed"}', "Confirmed"),
    ('{"data": {"reply": "Confirmed"}}', "Confirmed"),
    ('"Confirmed"', "Confirmed"),
    ('Confirmed', "Confirmed"),
    ('  Confirmed  ', "Confirmed"),
])
def test_every_shape_an_endpoint_might_answer_with(body, expected):
    assert parse_reply(body) == expected


@pytest.mark.parametrize("body", ["", "   ", "{}", '{"reply": ""}', '{"reply": "   "}',
                                  '{"unrelated": "x"}', "null", "[]"])
def test_an_empty_answer_means_seen_dont_answer(body):
    """Not an error. Most messages in a live chat do not want an answer."""
    assert parse_reply(body) == ""


def test_a_number_is_read_as_text():
    assert parse_reply("42") == "42"


def test_the_first_present_key_wins():
    assert parse_reply('{"reply": "first", "message": "second"}') == "first"


# ── the payload ───────────────────────────────────────────────────────


def test_the_payload_is_winsparks_envelope():
    chat = ChatConfig(chat_id="111111111111111@lid", contact_name="Priya Menon",
                      phone_number="919876543210", is_group=False)
    message = StoredMessage(message_key="m1", sender="someone", text="hello",
                            direction="in", media_kind="")

    payload = build_payload(chat, message)

    assert payload["event"] == "message.received"
    assert set(payload) == {"event", "app", "chat", "message"}
    assert payload["chat"] == {
        "id": "111111111111111@lid", "name": "Priya Menon",
        "phone": "919876543210", "is_group": False,
    }
    assert payload["message"]["text"] == "hello"
    assert payload["message"]["key"] == "m1"


def test_the_chat_name_falls_back_when_there_is_no_contact():
    chat = ChatConfig(chat_id="x@lid", chat_name="+91 98765 00000")
    payload = build_payload(chat, StoredMessage())
    assert payload["chat"]["name"] == "+91 98765 00000"


# ── retrying, and not ─────────────────────────────────────────────────


class Recorder:
    """Answers a scripted sequence and counts how often it was called."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


class FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode()
        self.status = status
        self.reason = "OK"

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(code: int) -> urllib.error.HTTPError:
    import io
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"body"))


def call(monkeypatch, opener, retries=3):
    monkeypatch.setattr("urllib.request.urlopen", opener)
    client = WebhookClient(timeout=1, max_retries=retries, backoff=0)
    return client.call("https://example.test/hook", {"x": 1}, sleep=lambda _s: None)


def test_a_success_is_not_retried(monkeypatch):
    opener = Recorder(FakeResponse('{"reply": "pong"}'))

    outcome = call(monkeypatch, opener)

    assert outcome.ok and outcome.reply_text == "pong"
    assert opener.calls == 1


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_failures_are_retried(monkeypatch, code):
    opener = Recorder(http_error(code))

    outcome = call(monkeypatch, opener, retries=2)

    assert not outcome.ok
    assert opener.calls == 3          # the first attempt plus two retries


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_a_4xx_fails_immediately(monkeypatch, code):
    """The endpoint saying the request itself is wrong. Repeating it verbatim
    would only be noise."""
    opener = Recorder(http_error(code))

    outcome = call(monkeypatch, opener)

    assert not outcome.ok
    assert opener.calls == 1


def test_a_transport_failure_is_retried(monkeypatch):
    opener = Recorder(urllib.error.URLError("connection refused"))

    outcome = call(monkeypatch, opener, retries=1)

    assert not outcome.ok
    assert opener.calls == 2
    assert "connection refused" in outcome.error


def test_a_retry_that_succeeds_returns_the_reply(monkeypatch):
    opener = Recorder(http_error(503), FakeResponse('{"reply": "late but here"}'))

    outcome = call(monkeypatch, opener)

    assert outcome.ok
    assert outcome.reply_text == "late but here"
    assert outcome.attempts == 2


def test_no_url_is_refused_without_a_request(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: pytest.fail("should not have been called"))
    outcome = WebhookClient().call("", {"x": 1})

    assert not outcome.ok
    assert "no webhook configured" in outcome.error


def test_an_api_key_is_sent_as_a_bearer_token(monkeypatch):
    seen = {}

    def opener(request, timeout=None):
        seen.update(request.headers)
        return FakeResponse("")

    monkeypatch.setattr("urllib.request.urlopen", opener)
    WebhookClient(api_key="s3cret").call("https://example.test/hook", {"x": 1})

    assert seen.get("Authorization") == "Bearer s3cret"


def test_the_body_posted_is_the_payload(monkeypatch):
    seen = {}

    def opener(request, timeout=None):
        seen["body"] = json.loads(request.data.decode())
        return FakeResponse("")

    monkeypatch.setattr("urllib.request.urlopen", opener)
    WebhookClient().call("https://example.test/hook", {"event": "message.received"})

    assert seen["body"] == {"event": "message.received"}
