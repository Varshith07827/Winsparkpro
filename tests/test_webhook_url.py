"""How a chat's webhook URL is built from the global template.

One template, one URL per chat. The interesting part is what each placeholder
refuses to do: a substitution that came out empty must produce no URL at all,
because a plausible-looking URL pointing at nobody collects messages forever
without anyone noticing.
"""

from __future__ import annotations

from wadam.domain.webhook_url import webhook_url_for



# ---------------------------------------------------------------------------
# The two placeholders
# ---------------------------------------------------------------------------





def test_a_group_is_addressed_by_its_encoded_name():
    """A group has no number, so the name is not a fallback for it — it is the
    identifier."""
    assert webhook_url_for("https://x.test/?{phone_number}", "",
                           chat_name="Novus Tech Group") == \
        "https://x.test/?Novus%20Tech%20Group"


def test_the_chat_name_placeholder_is_encoded():
    assert webhook_url_for("https://x.test/?{chat_name}", "", chat_name="Novus Tech Group") == \
        "https://x.test/?Novus%20Tech%20Group"



def test_an_override_still_beats_every_placeholder():
    assert webhook_url_for("https://x.test/?{phone_number}", "917981149423",
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
