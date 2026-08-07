"""Delivery verification — reading the sent message back out of the chat.

Until now "sent" meant *the compose box cleared*. That is real evidence, and it
is the strongest signal the send path itself can produce, but it is evidence
about the **input box**, not about the conversation. It cannot distinguish:

* a message that left the box and reached the chat,
* a message that left the box and was rejected (blocked contact, left group,
  a chat that has become read-only),
* a box cleared by something else entirely — the user pressing Escape, WhatsApp
  re-rendering, a second automation.

So the box clearing is now the *transport* signal, and delivery is confirmed
separately by finding the bubble:

    send → wait for the render → read the conversation → locate the new
    outgoing bubble → check text, direction and recency → delivered

**Counting, not set membership.** The obvious implementation — "is this text
present afterwards?" — passes when the message was already there from an
earlier send, which is exactly the case a verifier exists to catch. This takes
a census of matching outgoing bubbles *before* the send and requires the count
to have gone **up**. Sending "OK" for the third time verifies only when a third
"OK" appears.

A verification failure is deliberately not the same thing as a transport
failure, and the two are recorded separately: the first means "we could not
prove it arrived", the second means "we could not get it out of the box". They
have different causes and want different responses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# How long to keep re-reading the conversation before giving up. WhatsApp
# renders a sent bubble almost immediately, but a slow machine or a busy
# accessibility tree can lag; measured re-reads land well inside this.
VERIFY_TIMEOUT_SECONDS = 8.0
VERIFY_POLL_SECONDS = 0.6


class Verification:
    """Why a send was or wasn't confirmed. Distinct from transport outcomes."""

    VERIFIED = "verified"
    NOT_FOUND = "not_found"          # sent, but no matching bubble appeared
    UNREADABLE = "unreadable"        # the conversation could not be read
    SKIPPED = "skipped"              # verification disabled or not applicable


@dataclass(frozen=True)
class VerificationResult:
    status: str
    reason: str = ""
    bubble_time: str = ""            # the bubble's own clock label, e.g. "9:21 pm"
    elapsed_ms: int = 0
    matches_before: int = 0
    matches_after: int = 0

    @property
    def ok(self) -> bool:
        return self.status == Verification.VERIFIED

    def describe(self) -> str:
        if self.ok:
            at = f" at {self.bubble_time}" if self.bubble_time else ""
            return f"delivery confirmed{at} in {self.elapsed_ms} ms"
        return f"{self.status}: {self.reason}"


def normalise(text: str) -> str:
    """Compare the way WhatsApp renders, not the way we sent.

    The bubble read back is not byte-identical to what went in: whitespace is
    collapsed by the layout, and an emoji can come back as the
    object-replacement placeholder. The same normalisation the compose-box
    verification uses, for the same reasons."""
    ignored = ("￼", "︎", "️", "​", "‍")
    kept = "".join(ch for ch in (text or "") if ch not in ignored)
    return " ".join(kept.split()).casefold()


def count_outgoing(messages, text: str) -> int:
    """How many outgoing bubbles currently read as `text`."""
    wanted = normalise(text)
    if not wanted:
        return 0
    return sum(1 for m in messages
               if not m.is_incoming and normalise(m.text) == wanted)


def newest_matching(messages, text: str):
    """The last outgoing bubble matching `text`, for its timestamp."""
    wanted = normalise(text)
    for message in reversed(list(messages)):
        if not message.is_incoming and normalise(message.text) == wanted:
            return message
    return None


class SendVerifier:
    """Confirms delivery by reading the conversation back.

    Takes a `read_messages` coroutine rather than a reader object so it can be
    tested without WhatsApp, and so the relay, pipeline and send API all verify
    through exactly the same code."""

    def __init__(self, read_messages, timeout: float = VERIFY_TIMEOUT_SECONDS) -> None:
        self._read = read_messages
        self._timeout = timeout

    async def census(self, chat_name: str, text: str) -> Optional[int]:
        """How many matching outgoing bubbles exist *before* sending.

        None means the conversation could not be read — in which case the send
        still proceeds, but it is verified as `unreadable` rather than being
        falsely confirmed."""
        try:
            messages = await self._read(chat_name)
        except Exception as ex:  # noqa: BLE001
            logger.debug("pre-send census failed: %s", ex)
            return None
        if messages is None:
            return None
        return count_outgoing(messages, text)

    async def confirm(self, chat_name: str, text: str,
                      before: Optional[int]) -> VerificationResult:
        """Poll the conversation until a *new* matching outgoing bubble appears."""
        started = time.monotonic()
        if before is None:
            return VerificationResult(
                Verification.UNREADABLE,
                "the conversation could not be read before sending, so a new bubble "
                "cannot be told apart from one that was already there",
                elapsed_ms=0,
            )

        last_seen = before
        while (time.monotonic() - started) < self._timeout:
            await asyncio.sleep(VERIFY_POLL_SECONDS)
            try:
                messages = await self._read(chat_name)
            except Exception as ex:  # noqa: BLE001
                logger.debug("verification read failed: %s", ex)
                continue
            if messages is None:
                continue
            after = count_outgoing(messages, text)
            last_seen = after
            if after > before:
                bubble = newest_matching(messages, text)
                return VerificationResult(
                    Verification.VERIFIED,
                    bubble_time=getattr(bubble, "time_text", "") or "",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    matches_before=before, matches_after=after,
                )

        return VerificationResult(
            Verification.NOT_FOUND,
            f"the compose box cleared but no new outgoing message appeared within "
            f"{self._timeout:.0f}s (found {last_seen}, expected more than {before})",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            matches_before=before, matches_after=last_seen,
        )
