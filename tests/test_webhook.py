"""Webhook response parsing and retry policy."""

import pytest

from wadam.engine.webhook import WebhookOutcome, _is_retryable, extract_reply, optional_reply


@pytest.mark.parametrize("body,expected", [
    ('{"reply": "hello"}', "hello"),
    ('{"message": "hello"}', "hello"),
    ('{"text": "hello"}', "hello"),
    ('{"response": "hello"}', "hello"),
    ('{"answer": "hello"}', "hello"),
    ('{"data": {"reply": "hello"}}', "hello"),
    ('{"result": {"message": "hello"}}', "hello"),
    ('"hello"', "hello"),
    ("hello", "hello"),                       # a plain-text body IS the reply
    ('{"reply": ["a", "b"]}', "a\nb"),        # several lines, none lost
])
def test_reply_shapes_are_understood(body, expected):
    assert extract_reply(body) == expected


@pytest.mark.parametrize("body", ["", "   ", "{}", '{"reply": ""}', '{"reply": null}', "[]"])
def test_silence_is_a_valid_answer(body):
    assert extract_reply(body) == ""
    assert optional_reply(WebhookOutcome(ok=True, status_code=200, reply_text=extract_reply(body))) is None


def test_malformed_json_falls_back_to_the_raw_body():
    # Better to relay something the endpoint clearly meant as text than to
    # discard it because a brace was missing.
    assert extract_reply('{"reply": "unterminated') == '{"reply": "unterminated'


def test_retry_policy():
    assert _is_retryable(0) is True      # transport failure
    assert _is_retryable(429) is True    # rate limited
    assert _is_retryable(503) is True    # server side
    assert _is_retryable(500) is True
    assert _is_retryable(400) is False   # our request is wrong; repeating it won't help
    assert _is_retryable(404) is False
    assert _is_retryable(401) is False


def test_status_text_distinguishes_reply_from_silence():
    assert "reply" in WebhookOutcome(ok=True, status_code=200, reply_text="hi").status_text
    assert "no reply" in WebhookOutcome(ok=True, status_code=200).status_text


def test_the_payload_carries_the_chats_phone_number():
    """The endpoint is keyed on the number, so the body must contain it.

    With the name fallback the URL may read `?Novus%20Tech%20Group`, which left
    a receiver with no number ANYWHERE — not in the URL and not in the body."""
    from wadam.domain.models import ChatConfig, StoredMessage
    from wadam.engine.webhook import build_payload

    chat = ChatConfig(chat_id="c1", chat_name="Varshith", phone_number="919423155555")
    payload = build_payload(chat, StoredMessage(chat_id="c1", text="hello"))
    assert payload["chat"]["phone_number"] == "919423155555"


def test_an_unknown_number_is_an_empty_string_never_a_guess():
    from wadam.domain.models import ChatConfig, StoredMessage
    from wadam.engine.webhook import build_payload

    chat = ChatConfig(chat_id="c1", chat_name="Alice")
    payload = build_payload(chat, StoredMessage(chat_id="c1", text="hello"))
    assert payload["chat"]["phone_number"] == ""


def test_the_message_text_reaches_the_webhook_unmodified():
    """Section 3: no summarising, rewriting, classifying or enriching."""
    from wadam.domain.models import ChatConfig, StoredMessage
    from wadam.engine.webhook import build_payload

    raw = "  Hello  🚀\nsecond line   with   spaces  "
    payload = build_payload(ChatConfig(chat_id="c1", chat_name="A"),
                            StoredMessage(chat_id="c1", text=raw))
    assert payload["message"]["text"] == raw, "the raw message must pass through byte for byte"
