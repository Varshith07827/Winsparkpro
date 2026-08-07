"""Bridges the engine's asyncio loop to Qt.

The engine runs its own event loop on its own thread — it has to, because a
three-second poll cannot be at the mercy of the GUI thread's paint cycle, and
the GUI cannot be at the mercy of a webhook that takes twenty seconds to answer.

Traffic crosses the boundary in exactly two ways, and no others:

* **engine → UI**: the engine calls `on_snapshot` with an immutable snapshot;
  this object re-emits it as a Qt signal. Emitting a signal from a non-GUI
  thread is safe — Qt queues it onto the receiving thread's event loop.
* **UI → engine**: `AutomationEngine.submit` schedules a coroutine on the
  engine loop with `run_coroutine_threadsafe`.

The UI never touches the repository, the reader, or WhatsApp directly.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from wadam.config import Settings
from wadam.engine.engine import AutomationEngine, EngineSnapshot
from wadam.storage.repository import Repository

logger = logging.getLogger(__name__)


class EngineHost(QObject):
    snapshot_ready = Signal(object)
    engine_stopped = Signal(str)  # error text, empty on a clean stop

    def __init__(self, settings: Settings, repository: Repository,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._repository = repository
        self._thread: Optional[threading.Thread] = None
        self.engine = AutomationEngine(settings, repository, self._on_snapshot)

    def _on_snapshot(self, snapshot: EngineSnapshot) -> None:
        self.snapshot_ready.emit(snapshot)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wadam-engine", daemon=True)
        self._thread.start()
        # Give the loop a moment to exist before the UI starts submitting
        # commands into it; a command submitted too early would fail loudly for
        # no reason the user could act on.
        self.engine.wait_until_ready(timeout=10.0)

    def _run(self) -> None:
        error = ""
        try:
            asyncio.run(self.engine.run())
        except Exception as ex:  # noqa: BLE001
            error = f"{type(ex).__name__}: {ex}"
            logger.exception("The automation engine stopped unexpectedly")
        finally:
            self.engine_stopped.emit(error)

    def stop(self, timeout: float = 8.0) -> None:
        self.engine.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def submit(self, coroutine_factory: Callable[[], object]):
        return self.engine.submit(coroutine_factory)
