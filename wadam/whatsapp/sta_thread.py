"""A single STA thread that owns every UI Automation call.

UI Automation is COM, and COM is not safe to call from arbitrary or rotating
threads. So one dedicated background thread initialises a single-threaded
apartment (`pythoncom.CoInitialize()`, STA by default) and every automation
call is marshalled onto it.

Two details that were learned the hard way in the reference implementation and
are kept verbatim here:

* **The queue is polled, not blocked on.** Some UIA client calls (SetFocus,
  certain pattern invocations) block waiting for message delivery on the
  *calling* STA thread. A plain blocking `queue.get()` starves that message
  pump and the target window goes "Not Responding". Polling with a short
  timeout and calling `PumpWaitingMessages()` between checks avoids it.
* **`set_running_or_notify_cancel()` is called before the work runs.** Without
  it, an awaiting task that gets cancelled mid-work propagates the cancel into
  the future, `set_result()` raises `InvalidStateError`, the handler's
  `set_exception()` raises again uncaught, and this thread dies — freezing
  every later caller.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AutomationThreadHealth:
    is_running: bool = False
    thread_id: int = 0
    pending_requests: int = 0
    total_requests_processed: int = 0
    failed_requests: int = 0
    last_request_utc: Optional[datetime] = None
    restart_count: int = 0
    last_error: Optional[str] = None


@dataclass
class _WorkItem:
    func: Callable[[], object]
    future: "concurrent.futures.Future"


class StaAutomationThread:
    def __init__(self) -> None:
        self._queue: "queue.Queue[Optional[_WorkItem]]" = queue.Queue()
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._disposed = False
        self._total_processed = 0
        self._failed_requests = 0
        self._last_error: Optional[str] = None
        self._last_request_utc: Optional[datetime] = None
        self._restart_count = 0
        self._action_lock = asyncio.Lock()
        self._start_thread()

    @property
    def action_lock(self) -> "asyncio.Lock":
        """Serialises WHOLE real-input sequences — not individual STA calls
        (the queue already serialises those).

        Sending a message is several calls with real foreground changes,
        clicks and keystrokes in between. Bringing the WhatsApp *window*
        forward says nothing about *which chat is open*, so if another
        operation switches chats between this send's steps, the text lands in
        the wrong conversation. Holding this across the whole sequence makes
        concurrent sends and chat-opens queue instead of race."""
        return self._action_lock

    @property
    def is_healthy(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._disposed

    async def invoke_async(self, work: Callable[[], T]) -> T:
        self._ensure_healthy()
        future: "concurrent.futures.Future" = concurrent.futures.Future()
        self._queue.put(_WorkItem(work, future))
        return await asyncio.wrap_future(future)

    def get_health(self) -> AutomationThreadHealth:
        return AutomationThreadHealth(
            is_running=self.is_healthy,
            thread_id=(self._thread.ident or 0) if self._thread else 0,
            pending_requests=self._queue.qsize(),
            total_requests_processed=self._total_processed,
            failed_requests=self._failed_requests,
            last_request_utc=self._last_request_utc,
            restart_count=self._restart_count,
            last_error=self._last_error,
        )

    def _ensure_healthy(self) -> None:
        if self._disposed:
            raise RuntimeError("The automation thread has been disposed.")
        if self._thread is None or not self._thread.is_alive():
            with self._thread_lock:
                self._restart_count += 1
            logger.warning("Restarting the STA automation thread (attempt %d)", self._restart_count)
            self._start_thread()

    def _start_thread(self) -> None:
        with self._thread_lock:
            self._thread = threading.Thread(target=self._process_queue, name="wadam-STA", daemon=True)
            self._thread.start()
            logger.info("STA automation thread started (id=%s)", self._thread.ident)

    def _process_queue(self) -> None:
        try:
            import pythoncom

            pythoncom.CoInitialize()
        except ImportError:  # pragma: no cover - exercised only off-Windows
            pythoncom = None

        try:
            while True:
                if pythoncom is not None:
                    pythoncom.PumpWaitingMessages()
                try:
                    item = self._queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if item is None:  # shutdown sentinel
                    break
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = item.func()
                    # Counters updated BEFORE set_result: set_result wakes the
                    # awaiting coroutine through call_soon_threadsafe, which can
                    # read health before this thread reaches its next line.
                    self._total_processed += 1
                    self._last_request_utc = datetime.now(timezone.utc)
                    try:
                        item.future.set_result(result)
                    except Exception:  # noqa: BLE001 - caller gone; drop the result
                        pass
                except Exception as ex:  # noqa: BLE001
                    self._failed_requests += 1
                    self._last_error = str(ex)
                    try:
                        item.future.set_exception(ex)
                    except Exception:  # noqa: BLE001
                        pass
                    logger.error("STA work item failed", exc_info=True)
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "StaAutomationThread":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.dispose()
