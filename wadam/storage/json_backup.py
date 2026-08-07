"""The JSON mirror — a human-readable copy of everything MongoDB holds.

    MongoDB  ──primary──▶  authoritative store
       │
       └──mirror──▶  data/*.json   crash recovery · debugging · inspection
                                   import/export · disaster recovery

Rules this module enforces:

* **Nothing is edited in place.** A flush writes the whole file to a `.tmp`
  sibling, fsyncs it, then `os.replace()`s it over the real one. `os.replace`
  is atomic on Windows for same-volume paths, so a reader either sees the old
  complete file or the new complete file — never a half-written one, and never
  a file that vanished because the process died mid-write.
* **The application never touches these files outside that controlled save.**
  Reads happen once, at recovery time; writes happen only through `flush()`.
* **Every write reaches JSON.** Writes are coalesced over
  `JSON_AUTOSAVE_INTERVAL` so a three-second poll doesn't rewrite the mirror
  ten times a minute, but a coalesced batch still lands — and `flush(force=True)`
  on shutdown, on any pipeline completion, and on any user action drains
  whatever is outstanding.

The mirror is capped (see `constants.JSON_*_LIMIT`): it's a backup, not an
archive. MongoDB keeps the full history.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from wadam import constants

logger = logging.getLogger(__name__)


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


class JsonBackupStore:
    """Owns `data/*.json`. Thread-safe: the engine thread marks sections dirty,
    a timer thread flushes them."""

    def __init__(self, folder: Path, autosave_interval: float = 15.0) -> None:
        self._folder = Path(folder)
        self._interval = max(0.0, float(autosave_interval))
        self._lock = threading.RLock()
        self._sections: dict[str, Any] = {}
        self._dirty: set[str] = set()
        self._last_flush: dict[str, float] = {}
        self._write_failures = 0
        self._last_error = ""
        self._last_write_utc: Optional[datetime] = None

    # -- lifecycle ---------------------------------------------------------

    def ensure_folder(self) -> None:
        """Create the backup folder. Raises OSError with a readable message if
        it can't be created — that's a startup error, not something to limp
        past, because a mirror nobody can write is not a mirror."""
        self._folder.mkdir(parents=True, exist_ok=True)
        probe = self._folder / ".write-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
        finally:
            probe.unlink(missing_ok=True)

    @property
    def folder(self) -> Path:
        return self._folder

    @property
    def healthy(self) -> bool:
        return self._write_failures == 0

    @property
    def status_text(self) -> str:
        if self._write_failures:
            return f"error ({self._write_failures}) — {self._last_error}"
        if self._last_write_utc is None:
            return "idle"
        return f"ok · {self._last_write_utc.strftime('%H:%M:%S')}"

    # -- staging -----------------------------------------------------------

    def set_section(self, filename: str, payload: Any) -> None:
        """Stage a whole file's contents. Marked dirty; written on the next
        flush that its interval allows."""
        with self._lock:
            self._sections[filename] = payload
            self._dirty.add(filename)

    def flush(self, force: bool = False) -> None:
        """Write every dirty section whose coalescing window has elapsed
        (`force=True` writes them all regardless)."""
        now = time.monotonic()
        with self._lock:
            due = [
                name for name in self._dirty
                if force or self._interval <= 0
                or (now - self._last_flush.get(name, 0.0)) >= self._interval
            ]
            batch = [(name, self._sections.get(name)) for name in due]
            for name in due:
                self._dirty.discard(name)
                self._last_flush[name] = now

        for name, payload in batch:
            self._write_atomic(name, payload)

    def _write_atomic(self, filename: str, payload: Any) -> None:
        target = self._folder / filename
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            # Written into the SAME directory as the target: os.replace is only
            # atomic within one volume, and a temp file in %TEMP% may well be on
            # another drive.
            handle, temp_path = tempfile.mkstemp(
                prefix=f".{filename}.", suffix=".tmp", dir=str(self._folder)
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, ensure_ascii=False, default=_encode)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, target)
            except BaseException:
                Path(temp_path).unlink(missing_ok=True)
                raise
            self._last_write_utc = datetime.now().astimezone()
            if self._write_failures:
                logger.info("JSON mirror recovered — %s written", filename)
            self._write_failures = 0
            self._last_error = ""
        except Exception as ex:  # noqa: BLE001 - the mirror must never kill a cycle
            self._write_failures += 1
            self._last_error = str(ex)
            logger.error("Failed to write JSON mirror %s: %s", filename, ex)

    # -- recovery ----------------------------------------------------------

    def read_section(self, filename: str) -> Any:
        """Read a mirror file back. Used for crash recovery and export — never
        during normal operation."""
        path = self._folder / filename
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as ex:  # noqa: BLE001
            logger.error("Could not read JSON mirror %s: %s", filename, ex)
            return None

    def export_document(self, path: Path, payload: Any) -> None:
        """Write an arbitrary export (e.g. a single chat's full history) through
        the same atomic save. Raises on failure — the caller asked for this
        file explicitly and deserves to be told it didn't happen."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False, default=_encode)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        except BaseException:
            Path(temp_path).unlink(missing_ok=True)
            raise


class AutosaveTimer:
    """Calls `flush` on a daemon thread so a coalesced batch still lands even
    when the poll loop goes quiet."""

    def __init__(self, flush: Callable[[], None], interval: float) -> None:
        self._flush = flush
        self._interval = max(1.0, float(interval) or 1.0)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wadam-json-autosave", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._flush()
            except Exception:  # noqa: BLE001
                logger.exception("JSON autosave failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


__all__ = ["JsonBackupStore", "AutosaveTimer", "constants"]
