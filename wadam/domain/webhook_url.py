"""Building a chat's webhook URL from the global template.

One template, one number per chat, one URL — instead of a URL typed in per
chat. The template is the source of truth: a chat only ever carries an override
if somebody deliberately set one, and nothing is stored per chat that can drift
out of step with the template.

    https://noteify.org/ntext/whook/?{phone_number}
                                     └── replaced with the chat's number

A chat whose number could not be resolved gets **no URL at all**. That is the
whole point of the rule: substituting an empty string would produce a valid,
sending-looking URL pointing at nobody, and messages would be posted to it
forever without anyone noticing.
"""

from __future__ import annotations

from urllib.parse import urlparse

from wadam.constants import PHONE_PLACEHOLDER


def webhook_url_for(template: str, phone_number: str, override: str = "") -> str:
    """The URL to call for a chat, or "" when there isn't one.

    `override` wins when set — an escape hatch for a chat that genuinely needs
    a different endpoint, which the UI does not offer but the data model still
    honours."""
    if override.strip():
        return override.strip()
    template = (template or "").strip()
    number = (phone_number or "").strip()
    if not template:
        return ""
    if PHONE_PLACEHOLDER not in template:
        # A template with no placeholder is the same URL for every chat. Odd,
        # but explicit, and warned about at startup.
        return template
    if not number:
        return ""
    return template.replace(PHONE_PLACEHOLDER, number)


def describe_missing(phone_number: str) -> str:
    """Why a chat has no webhook URL, in words a non-technical user can act on."""
    if not (phone_number or "").strip():
        return ("No phone number could be read for this chat, so its webhook "
                "address cannot be built. WhatsApp only shows a number for "
                "contacts that are not saved in your address book.")
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
