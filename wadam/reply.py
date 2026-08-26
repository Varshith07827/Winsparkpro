"""The decision function — this is the file you edit.

`reply_for` is handed one inbound message and the chat it belongs to, and
returns the text to send back, or `None` to stay silent.

**Returning None is a success, not an error.** It is recorded as COLLECTED and
never retried. That is deliberate: most messages in a live chat do not want an
answer, and an endpoint that has to invent one for every message is an endpoint
that will eventually say something stupid.

Everything else — signature checks, deduplication, cooldown, loop protection,
persistence, the send itself — is handled before this is called. This file only
decides *what to say*.

It runs on one of the HTTP server's threads, so taking a second or two here is
fine; taking thirty is not, because OpenWA's delivery timeout will fire and it
will retry.
"""

from __future__ import annotations

from typing import Optional

from wadam.domain.models import ChatConfig
from wadam.openwa import InboundMessage


def reply_for(msg: InboundMessage, chat: ChatConfig) -> Optional[str]:
    """Return the reply text, or None to stay silent.

    Replace this body with whatever decides your answers — a keyword table, a
    MongoDB lookup, an HTTP call to your own service, a language model. The
    `chat` argument carries what this application already knows about the
    conversation, so a decision can depend on more than the message text.
    """
    text = (msg.text or "").strip().lower()

    if not text:
        # Media arrives with an empty body and a `media_kind` like "image" or
        # "audio". Answer those explicitly if you want to; by default, silence.
        return None

    if text in ("hi", "hello", "hey"):
        return "Hello! This is an automated reply."

    if text == "ping":
        return "pong"

    if "help" in text:
        return "Commands: hi, ping, help"

    return None
