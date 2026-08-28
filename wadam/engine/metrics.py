"""Operational counters — what the application has actually been doing.

Deliberately in-process and cheap: counters and a rolling average, no time
series, no external dependency. The question these answer is the one an
operator asks first — *is it working, and if not, which stage stopped?*

The window matters. A lifetime mean stops moving after a few thousand samples
and will happily report a healthy 900 ms while every send in the last ten
minutes has taken twelve seconds.

Most of the old counters described UI Automation and are gone with it:
`messages_verified` and `verification_failures` counted outgoing bubbles;
`focus_restores` and `cursor_restores` counted times the sender had to give the
user their foreground window back; `relay_polls` counted a pull loop that no
longer exists. What is left is what a bridge over an API can actually fail at.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

WINDOW = 50  # samples kept for the send-duration average


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


@dataclass(frozen=True)
class MetricsSnapshot:
    """An immutable read of the counters, safe to hand to the GUI thread."""

    started_at: Optional[datetime] = None
    uptime_seconds: float = 0.0

    deliveries: int = 0
    """Webhook deliveries accepted from OpenWA."""

    messages_received: int = 0
    replies_sent: int = 0
    send_failures: int = 0
    webhook_calls: int = 0
    webhook_failures: int = 0
    rejected: int = 0
    """Deliveries refused for a bad or missing signature."""

    avg_send_ms: float = 0.0
    avg_webhook_ms: float = 0.0


class Metrics:
    """Thread-safe counters. Every mutator is called from the HTTP threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = datetime.now(timezone.utc)
        self._deliveries = 0
        self._received = 0
        self._replies = 0
        self._send_failures = 0
        self._rejected = 0
        self._send_ms: deque[float] = deque(maxlen=WINDOW)
        self._webhook_calls = 0
        self._webhook_failures = 0
        self._webhook_ms: deque[float] = deque(maxlen=WINDOW)

    def record_delivery(self) -> None:
        with self._lock:
            self._deliveries += 1

    def record_received(self) -> None:
        with self._lock:
            self._received += 1

    def record_send(self, ok: bool, duration_ms: float = 0.0) -> None:
        with self._lock:
            if ok:
                self._replies += 1
                if duration_ms:
                    self._send_ms.append(duration_ms)
            else:
                self._send_failures += 1

    def record_webhook(self, ok: bool, duration_ms: float = 0.0) -> None:
        with self._lock:
            self._webhook_calls += 1
            if not ok:
                self._webhook_failures += 1
            if duration_ms:
                self._webhook_ms.append(duration_ms)

    def record_rejected(self) -> None:
        with self._lock:
            self._rejected += 1

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
            return MetricsSnapshot(
                started_at=self._started_at,
                uptime_seconds=uptime,
                deliveries=self._deliveries,
                messages_received=self._received,
                replies_sent=self._replies,
                send_failures=self._send_failures,
                webhook_calls=self._webhook_calls,
                webhook_failures=self._webhook_failures,
                avg_webhook_ms=_mean(self._webhook_ms),
                rejected=self._rejected,
                avg_send_ms=_mean(self._send_ms),
            )
