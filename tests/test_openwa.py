"""The OpenWA transport: proving a delivery, reading it, and sending."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from wadam.domain.models import phone_from_chat_id
from wadam.openwa import OpenWAClient, SendError, inbound

SECRET = "test-secret-at-least-16-chars"


def body_for(text: str = "hi", chat_id: str = "111111111111111@lid",
             direction: str = "incoming", message_id: str = "m1") -> bytes:
    return json.dumps({
        "event": "message.received",
        "sessionId": "sess-1",
        "idempotencyKey": "idem-1",
        "data": {
            "waMessageId": message_id,
            "chatId": chat_id,
            "from": "333333333333333@lid",
            "body": text,
            "type": "text",
            "direction": direction,
        },
    }).encode("utf-8")


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── signatures ────────────────────────────────────────────────────────


def test_a_valid_signature_is_accepted():
    body = body_for()
    assert inbound.verify_signature(body, sign(body), SECRET) is True


def test_a_wrong_signature_is_refused():
    assert inbound.verify_signature(body_for(), "sha256=deadbeef", SECRET) is False


def test_a_missing_signature_is_refused_when_a_secret_is_set():
    assert inbound.verify_signature(body_for(), None, SECRET) is False


def test_no_configured_secret_disables_the_check():
    assert inbound.verify_signature(body_for(), None, "") is True


def test_the_signature_covers_the_raw_bytes_not_a_reserialization():
    """Re-serializing a parsed body reorders keys and changes whitespace, so
    verification has to happen before json.loads."""
    body = body_for()
    reserialized = json.dumps(json.loads(body), sort_keys=True).encode()

    assert inbound.verify_signature(body, sign(body), SECRET)
    assert not inbound.verify_signature(reserialized, sign(body), SECRET)


# ── parsing ───────────────────────────────────────────────────────────


def test_a_message_is_read_out_of_a_delivery():
    msg = inbound.parse_delivery(json.loads(body_for("hello")))

    assert msg is not None
    assert msg.chat_id == "111111111111111@lid"
    assert msg.text == "hello"
    assert msg.message_id == "m1"
    assert msg.is_outgoing is False
    assert msg.is_group is False


def test_direction_outgoing_is_recognised():
    msg = inbound.parse_delivery(json.loads(body_for(direction="outgoing")))
    assert msg.is_outgoing is True


def test_from_me_is_recognised_when_direction_is_absent():
    payload = json.loads(body_for())
    del payload["data"]["direction"]
    payload["data"]["fromMe"] = True

    assert inbound.parse_delivery(payload).is_outgoing is True


def test_a_group_chat_is_recognised_from_its_id():
    msg = inbound.parse_delivery(json.loads(body_for(chat_id="1203630@g.us")))
    assert msg.is_group is True


def test_field_names_are_read_from_whichever_key_is_present():
    """OpenWA has moved field names between releases. Being strict about a
    shape you do not control turns somebody else's rename into your outage."""
    payload = {"data": {"chat_id": "x@c.us", "message": "hi there", "messageId": "m9"}}
    msg = inbound.parse_delivery(payload)

    assert msg.chat_id == "x@c.us"
    assert msg.text == "hi there"
    assert msg.message_id == "m9"


def test_the_push_name_is_read_from_the_nested_contact():
    """OpenWA sets `incoming.contact = { pushName }` rather than putting it at
    the top level. Reading only the top level named every chat after its raw
    identifier — `111111111111111@lid` in the list instead of a person."""
    payload = json.loads(body_for())
    payload["data"]["contact"] = {"pushName": "Alice"}

    assert inbound.parse_delivery(payload).chat_name == "Alice"


def test_a_top_level_name_still_wins():
    payload = json.loads(body_for())
    payload["data"]["chatName"] = "From the top"
    payload["data"]["contact"] = {"pushName": "Nested"}

    assert inbound.parse_delivery(payload).chat_name == "From the top"


def test_a_nameless_delivery_reports_no_name(): 
    """Not the chat id. The caller has to be able to tell "this named the chat"
    from "there was no name" — conflating them let a nameless message overwrite
    a good name with a raw @lid."""
    assert inbound.parse_delivery(json.loads(body_for())).chat_name == ""


def test_a_contact_that_is_not_an_object_is_survived():
    payload = json.loads(body_for())
    payload["data"]["contact"] = "not an object"

    assert inbound.parse_delivery(payload).chat_name == ""


def test_a_payload_with_no_chat_yields_nothing():
    assert inbound.parse_delivery({"event": "session.status", "data": {"status": "ready"}}) is None


def test_a_payload_that_is_not_an_object_yields_nothing():
    assert inbound.parse_delivery({"data": "nonsense"}) is None


def test_an_unparseable_body_yields_nothing():
    assert inbound.parse_body(b"{not json") is None
    assert inbound.parse_body(b"[1,2,3]") is None


def test_a_text_type_is_not_reported_as_media():
    msg = inbound.parse_delivery(json.loads(body_for()))
    assert msg.media_kind == ""


# ── identity ──────────────────────────────────────────────────────────


def test_a_phone_number_is_read_from_a_c_us_chat_id():
    assert phone_from_chat_id("919876500000@c.us") == "919876500000"


def test_a_lid_yields_no_phone_number():
    """A LID is opaque. Reading it as a number would display a plausible-looking
    number belonging to nobody."""
    assert phone_from_chat_id("111111111111111@lid") == ""


def test_a_group_id_yields_no_phone_number():
    assert phone_from_chat_id("120363000000000000@g.us") == ""


# ── the client ────────────────────────────────────────────────────────


def test_sending_without_a_chat_id_is_refused():
    """Never build a chat id. Sending to the wrong person is the one failure
    that must not happen quietly."""
    client = OpenWAClient("http://localhost:2785", "key", "sess")

    with pytest.raises(SendError):
        client.send_text("", "hello")


def test_the_send_url_names_the_session():
    client = OpenWAClient("http://localhost:2785/", "key", "sess-42")
    assert client.send_url.endswith("/api/sessions/sess-42/messages/send-text")


def test_session_status_reports_unreachable_rather_than_raising():
    """This drives a status light that polls on a timer. An unreachable gateway
    is a thing to display, not an exception to handle at every call site."""
    client = OpenWAClient("http://127.0.0.1:9", "key", "sess", timeout=0.5)

    assert client.session_status()["status"] == "unreachable"
