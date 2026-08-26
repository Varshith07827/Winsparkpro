"""What stops a working bridge from becoming a runaway one.

**Deduplication** lives in MongoDB, not here. OpenWA retries a delivery it
could not confirm, reusing `X-OpenWA-Idempotency-Key`, and the repository's
unique index on `message_key` already refuses a second write. Storing the check
means it survives a restart, which the old in-memory scheme could not — and the
key is now WhatsApp's own message id rather than a hash of the message's text,
so two people genuinely sending "ok" a minute apart are no longer one message.

**Cooldown** lives here, because it is about pacing rather than truth. Nothing
in the protocol stops two automated endpoints from answering each other
forever. winSpark made that impossible structurally by never letting an inbound
message cause an outbound one; this application deliberately joins those flows,
so the loop is bounded instead of excluded.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional


class Cooldown:
    """Per-chat quiet period. Thread-safe, bounded in memory.

    `seconds <= 0` disables it — knowingly, and only worth doing when the other
    end is definitely a person.
    """

    def __init__(self, seconds: float = 60.0, capacity: int = 10_000) -> None:
        self._seconds = seconds
        self._capacity = capacity
        self._last: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, chat_id: str, now: Optional[float] = None) -> bool:
        """True if this chat may be answered now. Records the send if so.

        Asks and records together, because a reply that was never delivered
        must not start a cooldown the next real reply then waits out.
        """
        if self._seconds <= 0:
            return True

        moment = time.monotonic() if now is None else now
        with self._lock:
            previous = self._last.get(chat_id)
            if previous is not None and moment - previous < self._seconds:
                return False
            self._last[chat_id] = moment
            self._last.move_to_end(chat_id)
            while len(self._last) > self._capacity:
                self._last.popitem(last=False)
            return True

    def remaining(self, chat_id: str, now: Optional[float] = None) -> float:
        """Seconds left on this chat's cooldown. 0.0 when it may be answered."""
        if self._seconds <= 0:
            return 0.0
        moment = time.monotonic() if now is None else now
        with self._lock:
            previous = self._last.get(chat_id)
        if previous is None:
            return 0.0
        return max(0.0, self._seconds - (moment - previous))
