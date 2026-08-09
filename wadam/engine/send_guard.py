"""A lock that lets only one kind of message out.

Built after a controlled end-to-end test sent about thirty unintended messages
to a real contact. The cause was not the pipeline under test: a relay endpoint
answered a poll every three seconds and the relay dutifully sent what it got.
Nothing in the application was wrong to do that — nothing had been told not to.

So the guard is not a bug fix, it is a *test instrument*. While a real person is
on the other end, exactly one producer may speak:

    WADAM_ONLY_ORIGIN=webhook_reply

Anything else is refused and logged with its origin, which is the question that
mattered during the incident: **which producer sent this?**

Placed where the origin is known rather than at one chokepoint, because there
isn't one — the audit found three queue producers and three more paths that
reach the sender directly, bypassing the queue. A guard on `enqueue` alone
would have missed half of them.

Unset (the default) means no restriction: production behaviour is untouched.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ENV_VAR = "WADAM_ONLY_ORIGIN"

#: The producer that answers an incoming message — the one under test.
INCOMING_WEBHOOK_RESPONSE = "webhook_reply"


class SendRefused(RuntimeError):
    """A producer tried to send while the guard allowed only another one."""


def armed() -> bool:
    return bool(os.environ.get(ENV_VAR, "").strip())


def allowed_origins() -> set[str]:
    raw = os.environ.get(ENV_VAR, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def check(origin: str, *, chat_name: str = "", text: str = "") -> None:
    """Log this send's origin, and refuse it if the guard forbids it.

    The log line is unconditional. During the incident the application's own
    records were the only way to establish that all nine sends came from the
    relay and none from the pipeline being tested, and that answer should not
    depend on anyone having thought to enable extra logging first."""
    origin = (origin or "").strip() or "unknown"
    logger.info("send origin=%s chat=%r text=%r", origin, chat_name, text[:60])
    if not armed():
        return
    allowed = allowed_origins()
    if origin not in allowed:
        logger.error("REFUSED send: origin=%s is not in %s (guard armed via %s)",
                     origin, sorted(allowed), ENV_VAR)
        raise SendRefused(
            f"send from origin {origin!r} refused: {ENV_VAR} allows only "
            f"{sorted(allowed)}"
        )
