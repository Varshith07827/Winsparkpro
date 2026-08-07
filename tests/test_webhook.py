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
