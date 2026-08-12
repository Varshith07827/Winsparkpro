"""How a chat's webhook URL is built from the global template.

One template, one URL per chat. The interesting part is what each placeholder
refuses to do: a substitution that came out empty must produce no URL at all,
because a plausible-looking URL pointing at nobody collects messages forever
without anyone noticing.
"""

from __future__ import annotations

from wadam.domain.webhook_url import webhook_url_for



# ---------------------------------------------------------------------------
# {external_id} — for endpoints keyed by a short id, not a full number
# ---------------------------------------------------------------------------


def test_a_web_key_plus_the_last_four_digits():
    """The shape a real endpoint used: `?agkfghxq9423` is an account key with
    the chat's last four digits appended. No amount of {phone_number} can build
    that, so the template could not express the integration at all."""
    url = webhook_url_for("https://noteify.org/ntext/whook/?agkfghxq{external_id}",
                          "917981149423")
    assert url == "https://noteify.org/ntext/whook/?agkfghxq9423"


def test_the_short_id_is_derived_not_stored():
    """It comes from the number, so it cannot drift out of step with it."""
    assert webhook_url_for("https://x.test/?{external_id}", "+91 79811 49423") == \
        "https://x.test/?9423"


def test_a_short_id_template_refuses_to_fall_back_to_the_name():
    """A name spliced into a key-shaped URL builds something that looks valid
    and reaches nobody. Better no webhook than a plausible wrong one."""
    assert webhook_url_for("https://x.test/?key{external_id}", "", chat_name="Varshith") == ""


def test_the_chat_name_placeholder_is_encoded():
    assert webhook_url_for("https://x.test/?{chat_name}", "", chat_name="Novus Tech Group") == \
        "https://x.test/?Novus%20Tech%20Group"


def test_placeholders_can_be_mixed():
    url = webhook_url_for("https://x.test/{chat_name}/?k{external_id}&n={phone_number}",
                          "917981149423", chat_name="Varshith")
    assert url == "https://x.test/Varshith/?k9423&n=917981149423"


def test_an_override_still_beats_every_placeholder():
    assert webhook_url_for("https://x.test/?{external_id}", "917981149423",
                           override="https://elsewhere.test/hook") == \
        "https://elsewhere.test/hook"


def test_the_phone_number_template_is_unchanged():
    """The default has to keep behaving exactly as it did."""
    assert webhook_url_for("https://noteify.org/ntext/whook/?{phone_number}",
                           "917981149423") == \
        "https://noteify.org/ntext/whook/?917981149423"
    assert webhook_url_for("https://noteify.org/ntext/whook/?{phone_number}",
                           "", chat_name="Novus Tech Group") == \
        "https://noteify.org/ntext/whook/?Novus%20Tech%20Group"
