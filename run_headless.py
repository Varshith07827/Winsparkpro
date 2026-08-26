"""Run the service without the window.

The GUI is the normal way in. This is for a server, a container, or a check
that the pipeline works without a display attached — everything except the
tick box, which is why chats have to be switched on in the database (or from
the window once) rather than here.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from wadam.api.host import SendApiHost
from wadam.config import ConfigError, load_settings
from wadam.engine.service import AutomationService
from wadam.logging_setup import configure_logging
from wadam.reply import reply_for
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.mongo import MongoStore
from wadam.storage.repository import Repository


def main() -> int:
    try:
        settings = load_settings(Path(__file__).parent / ".env")
    except ConfigError as ex:
        print("Configuration is not usable:\n", file=sys.stderr)
        for problem in ex.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    configure_logging(settings.log_level, settings.json_backup_folder)
    log = logging.getLogger("wadam")

    backup = JsonBackupStore(settings.json_backup_folder, settings.json_autosave_interval)
    backup.ensure_folder()
    mongo = MongoStore(settings.mongodb_uri, settings.database_name)
    mongo.connect()
    repository = Repository(settings, mongo, backup)
    repository.start()

    service = AutomationService(settings, repository, reply_for)
    service.start()
    service.refresh_session()
    snapshot = service.snapshot()
    log.info("session %s · mongo %s · %d chat(s)",
             snapshot.session_status, snapshot.mongo_status, len(snapshot.chats))

    # After the listener: a send arriving before the service exists would fail
    # for a reason the caller could do nothing about.
    api = SendApiHost(settings, repository, service)
    if api.enabled:
        api.start()
    else:
        log.info("send API off (set API_PORT to enable)")

    stopping = False

    def shutdown(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stopping:
            signal.pause() if hasattr(signal, "pause") else __import__("time").sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.stop()
        service.stop()
        repository.stop()
        mongo.close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
