"""Bridges the service to Qt.

The service runs an HTTP server on its own threads — it has to, because the
GUI cannot be at the mercy of a reply function that takes twenty seconds, and a
webhook delivery cannot be at the mercy of the GUI's paint cycle.

Traffic crosses the boundary in exactly two ways, and no others:

* **service → UI**: the service calls `on_snapshot` with an immutable snapshot;
  this object re-emits it as a Qt signal. Emitting a signal from a non-GUI
  thread is safe — Qt queues it onto the receiving thread's event loop.
* **UI → service**: direct method calls. They are cheap and take the
  repository's lock, so there is nothing to marshal.

This used to run an asyncio loop and marshal commands into it with
`run_coroutine_threadsafe`, because the engine was a long-lived polling
coroutine that owned an automation lock. There is no loop and no lock now: the
HTTP server is the scheduler.

The UI still never touches the repository to *write*. It reads from the
snapshot and asks the service to change things.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from wadam.config import Settings
from wadam.engine.service import AutomationService, EngineSnapshot
from wadam.storage.repository import Repository

logger = logging.getLogger(__name__)

#: How often the session indicator asks OpenWA how it is. Slow on purpose:
#: it is a status light, not a heartbeat, and each tick is an HTTP round trip.
SESSION_POLL_MS = 10_000


class EngineHost(QObject):
    snapshot_ready = Signal(object)
    engine_stopped = Signal(str)  # error text, empty on a clean stop

    def __init__(self, settings: Settings, repository: Repository, reply_fn,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._repository = repository
        self.service = AutomationService(settings, repository, reply_fn, self._on_snapshot)
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(SESSION_POLL_MS)
        self._session_timer.timeout.connect(self._poll_session)

    def _on_snapshot(self, snapshot: EngineSnapshot) -> None:
        self.snapshot_ready.emit(snapshot)

    def start(self) -> None:
        try:
            self.service.start()
        except OSError as ex:
            # A port already in use is the common one, and it is worth naming
            # precisely: the window would otherwise just sit there receiving
            # nothing, looking like OpenWA was misconfigured.
            message = (f"Could not listen on {self._settings.webhook_host}:"
                       f"{self._settings.webhook_port} — {ex}")
            logger.error(message)
            self.engine_stopped.emit(message)
            return
        self._poll_session()
        self._session_timer.start()

    def _poll_session(self) -> None:
        self.service.refresh_session()
        self.service.publish()

    def stop(self, timeout: float = 5.0) -> None:
        self._session_timer.stop()
        self.service.stop(timeout=timeout)
