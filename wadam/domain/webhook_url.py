"""Building a chat's webhook URL from the global template.

One template, one number per chat, one URL — instead of a URL typed in per
chat. The template is the source of truth: a chat only ever carries an override
if somebody deliberately set one, and nothing is stored per chat that can drift
out of step with the template.

    https://noteify.org/ntext/whook/?{phone_number}
                                     └── replaced with the chat's number

Two placeholders:

    {phone_number}   918106972933   the digits
    {chat_name}      Novus%20Tech   the display name, URL-encoded

A short `{external_id}` placeholder briefly existed for endpoints keyed by the
last four digits. It went with the four-digit id itself: four digits is 10,000
values, so two contacts sharing one is likely rather than exotic, and an
identifier that can quietly address the wrong person is not worth having. The
full number is what a caller already holds and what the endpoint was tested
with.

A chat whose number is not known — **every group**, since a group has no single
number — falls back to its **name**, URL-encoded:

    https://noteify.org/ntext/whook/?Novus%20Tech%20Group

An empty substitution is still refused — that would produce a valid,
sending-looking URL pointing at nobody, and messages would post to it forever
unnoticed. A name is different: it identifies the chat, the receiving end can
tell the two apart, and it means every chat forwards from the moment it is
ticked instead of waiting for somebody to look up a phone number.

The number remains preferred and replaces the name the moment it is known.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

from wadam.constants import CHAT_NAME_PLACEHOLDER, PHONE_PLACEHOLDER


def webhook_url_for(template: str, phone_number: str, override: str = "",
                    chat_name: str = "") -> str:
    """The URL to call for a chat, or "" when there is genuinely nothing to
    identify it by.

    Order of preference: an explicit `override`, then the phone number, then
    the chat name. `override` is an escape hatch for a chat that needs a
    different endpoint — the UI does not offer it, the data model honours it."""
    if override.strip():
        return override.strip()
    template = (template or "").strip()
    if not template:
        return ""

    number = (phone_number or "").strip()
    # Quoted, because a chat name can hold spaces, "&", "#" or an emoji, any of
    # which would otherwise truncate or corrupt the query string.
    name = quote((chat_name or "").strip(), safe="")
    if CHAT_NAME_PLACEHOLDER in template:
        if not name:
            return ""
        template = template.replace(CHAT_NAME_PLACEHOLDER, name)
    if PHONE_PLACEHOLDER not in template:
        # Either it never had one — the same URL for every chat, odd but
        # explicit and warned about at startup — or {chat_name} already filled
        # the template in.
        return template

    identifier = number or name
    if not identifier:
        return ""
    return template.replace(PHONE_PLACEHOLDER, identifier)


def describe_missing(phone_number: str) -> str:
    """Why a chat's URL uses its name rather than its number."""
    if not (phone_number or "").strip():
        return ("This chat is addressed by name because WhatsApp does not show "
                "a number for a saved contact. Enter the number to use it "
                "instead.")
    return ""


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """Is this something the dispatcher can actually POST to?

    Empty is valid — it means "no webhook", which is what a chat with no
    resolvable phone number has and a legitimate way to park one. Anything else
    has to be an absolute http(s) URL with a host, because those are the only
    two things the client speaks and a typo like `htp://` or a bare
    `example.com/hook` should be caught here rather than discovered as a failed
    delivery an hour later."""
    text = (url or "").strip()
    if not text:
        return True, ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return False, "The URL must start with http:// or https://"
    if not parsed.netloc:
        return False, "The URL has no host — expected something like https://example.com/hook"
    if " " in text:
        return False, "The URL contains a space"
    return True, ""
