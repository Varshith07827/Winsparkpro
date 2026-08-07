"""Operational counters — what the application has actually been doing.

Deliberately in-process and cheap: counters and rolling averages, no time
series, no external dependency. The question these answer is the one an
operator asks first — *is it working, and if not, which stage stopped?* — and
that is answerable from totals plus a few averages.

Averages are kept over a bounded window rather than for all time. A lifetime
mean stops moving after a few thousand samples and will happily report a
healthy 900 ms while every send in the last ten minutes has taken twelve
seconds. The window is what makes a metric say something about *now*.

Counts are cumulative since start, which is why `started_at` is reported
alongside them — a number without a denominator is not a measurement.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

WINDOW = 50          # samples kept per timing series


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


@dataclass
class MetricsSnapshot:
    """An immutable copy for the UI, so nothing renders a moving target."""

    started_at: Optional[datetime] = None
    uptime_seconds: float = 0.0

    messages_read: int = 0
    messages_queued: int = 0
    messages_sent: int = 0
    messages_verified: int = 0
    verification_failures: int = 0
    send_failures: int = 0
    retries: int = 0

    webhook_calls: int = 0
    webhook_failures: int = 0
    relay_polls: int = 0
    relay_messages: int = 0

    reconnects: int = 0
    session_holds: int = 0
    focus_restores: int = 0
    cursor_restores: int = 0

    queue_depth: int = 0
    needs_review: int = 0
    avg_read_ms: float = 0.0
    avg_send_ms: float = 0.0
    avg_verify_ms: float = 0.0
    avg_webhook_ms: float = 0.0

    def rows(self) -> list[tuple[str, str]]:
        """(label, value) pairs for the operations display."""
        def ms(value: float) -> str:
            return f"{value:.0f} ms" if value else "—"

        delivered = self.messages_verified
        attempted = self.messages_sent + self.send_failures
        success = f"{100 * delivered / attempted:.0f}%" if attempted else "—"
        return [
            ("Messages read", str(self.messages_read)),
            ("Messages queued", str(self.messages_queued)),
            ("Messages sent", str(self.messages_sent)),
            ("Messages verified", str(self.messages_verified)),
            ("Verification failures", str(self.verification_failures)),
            ("Send failures", str(self.send_failures)),
            ("Retries", str(self.retries)),
            ("Delivery rate", success),
            ("Queue depth", str(self.queue_depth)),
            ("Needs review", str(self.needs_review)),
            ("Webhook calls", f"{self.webhook_calls} ({self.webhook_failures} failed)"),
            ("Relay polls", f"{self.relay_polls} ({self.relay_messages} messages)"),
            ("WhatsApp reconnects", str(self.reconnects)),
            ("Sends held (session)", str(self.session_holds)),
            ("Focus restores", str(self.focus_restores)),
            ("Cursor restores", str(self.cursor_restores)),
            ("Average read", ms(self.avg_read_ms)),
            ("Average send", ms(self.avg_send_ms)),
            ("Average verification", ms(self.avg_verify_ms)),
            ("Average webhook", ms(self.avg_webhook_ms)),
        ]


class Metrics:
    """Thread-safe: the engine loop, the worker and the send API all report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = datetime.now(timezone.utc)
        self._counts: dict[str, int] = {}
        self._read_ms: deque[float] = deque(maxlen=WINDOW)
        self._send_ms: deque[float] = deque(maxlen=WINDOW)
        self._verify_ms: deque[float] = deque(maxlen=WINDOW)
        self._webhook_ms: deque[float] = deque(maxlen=WINDOW)

    def _bump(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + by

    # -- reading -----------------------------------------------------------

    def record_read(self, message_count: int, elapsed_ms: float) -> None:
        self._bump("messages_read", message_count)
        with self._lock:
            self._read_ms.append(elapsed_ms)

    def record_reconnect(self) -> None:
        self._bump("reconnects")

    # -- outgoing ----------------------------------------------------------

    def record_queued(self) -> None:
        self._bump("messages_queued")

    def record_sent(self, duration_ms: float) -> None:
        self._bump("messages_sent")
        with self._lock:
            if duration_ms:
                self._send_ms.append(duration_ms)

    def record_send_failure(self) -> None:
        self._bump("send_failures")

    def record_retry(self) -> None:
        self._bump("retries")

    def record_session_hold(self) -> None:
        """A send refused because the session had no interactive desktop."""
        self._bump("session_holds")

    def record_focus_restore(self, restored: bool) -> None:
        if restored:
            self._bump("focus_restores")

    def record_cursor_restore(self) -> None:
        self._bump("cursor_restores")

    # -- verification ------------------------------------------------------

    def record_verification(self, result) -> None:
        with self._lock:
            if getattr(result, "elapsed_ms", 0):
                self._verify_ms.append(result.elapsed_ms)
        if getattr(result, "ok", False):
            self._bump("messages_verified")

    def record_verification_failure(self) -> None:
        self._bump("verification_failures")

    # -- webhook / relay ---------------------------------------------------

    def record_webhook(self, ok: bool, duration_ms: float) -> None:
        self._bump("webhook_calls")
        if not ok:
            self._bump("webhook_failures")
        with self._lock:
            if duration_ms:
                self._webhook_ms.append(duration_ms)

    def record_relay_poll(self, message_count: int) -> None:
        self._bump("relay_polls")
        if message_count:
            self._bump("relay_messages", message_count)

    # -- reporting ---------------------------------------------------------

    def snapshot(self, queue_depth: int = 0, needs_review: int = 0) -> MetricsSnapshot:
        with self._lock:
            counts = dict(self._counts)
            read, send = _mean(self._read_ms), _mean(self._send_ms)
            verify, webhook = _mean(self._verify_ms), _mean(self._webhook_ms)
        return MetricsSnapshot(
            started_at=self._started,
            uptime_seconds=(datetime.now(timezone.utc) - self._started).total_seconds(),
            messages_read=counts.get("messages_read", 0),
            messages_queued=counts.get("messages_queued", 0),
            messages_sent=counts.get("messages_sent", 0),
            messages_verified=counts.get("messages_verified", 0),
            verification_failures=counts.get("verification_failures", 0),
            send_failures=counts.get("send_failures", 0),
            retries=counts.get("retries", 0),
            webhook_calls=counts.get("webhook_calls", 0),
            webhook_failures=counts.get("webhook_failures", 0),
            relay_polls=counts.get("relay_polls", 0),
            relay_messages=counts.get("relay_messages", 0),
            reconnects=counts.get("reconnects", 0),
            session_holds=counts.get("session_holds", 0),
            focus_restores=counts.get("focus_restores", 0),
            cursor_restores=counts.get("cursor_restores", 0),
            queue_depth=queue_depth,
            needs_review=needs_review,
            avg_read_ms=read, avg_send_ms=send,
            avg_verify_ms=verify, avg_webhook_ms=webhook,
        )
