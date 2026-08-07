"""Logging — console plus a rotating file next to the JSON mirror.

The file lives in `JSON_BACKUP_FOLDER/logs/` deliberately: when something goes
wrong, the person debugging is already looking in that folder at
`messages.json` and `logs.json`, and the Python-level log belongs beside them.
`logs.json` is the *automation* log (what the engine decided); this file is the
*diagnostic* log (what the code did, including stack traces).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


def configure_logging(level: str = "INFO", folder: Path | None = None) -> Path | None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)

    log_path: Path | None = None
    if folder is not None:
        try:
            log_dir = Path(folder) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "wadam.log"
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter(_FORMAT))
            root.addHandler(file_handler)
        except OSError:
            # A log file we can't write is not a reason to refuse to start —
            # the console handler is already attached.
            logging.getLogger(__name__).warning("Could not open the log file", exc_info=True)
            log_path = None

    # pymongo's heartbeat chatter at DEBUG buries everything else.
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    return log_path
