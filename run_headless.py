"""Run the service without the window.

The GUI is the normal way in. This is for a server, a container, or a check
that the pipeline works without a display attached — everything except the
tick box, which is why chats have to be switched on in the database (or from
the window once) rather than here.
"""

from __future__ import annotations

import logging
import signal
import threading
import sys
from pathlib import Path

from wadam.api.host import SendApiHost
from wadam.config import ConfigError, load_settings
from wadam.engine.bootstrap import prepare
from wadam.openwa import OpenWAClient
from wadam.engine.service import AutomationService
from wadam.logging_setup import configure_logging
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

    # Fills in the session id and webhook secret when they were left out, and
    # makes OpenWA deliver here. Raises ConfigError with something actionable.
    try:
        settings, _ = prepare(
            settings, repository,
            lambda sid: OpenWAClient(settings.openwa_url, settings.openwa_api_key, sid),
        )
    except ConfigError as ex:
        for problem in ex.problems:
            log.error("%s", problem)
        repository.stop()
        mongo.close()
        return 1

    service = AutomationService(settings, repository)
    service.start()
    service.refresh_session()
    log.info("syncing the directory from OpenWA…")
    log.info("directory: %s", service.sync_directory())
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

    # SIGTERM as well as SIGINT, and this is not a nicety. `systemctl stop` and
    # `systemctl restart` both send SIGTERM, whose default action kills the
    # process outright -- so the `finally` below never ran under systemd, and
    # `repository.stop()`, which drains the JSON mirror, never ran with it.
    #
    # That is worst in exactly the situation where it matters most: with
    # MongoDB unreachable the mirror is the ONLY store, and every restart was
    # discarding whatever had not yet been flushed on its own timer.
    stopping = threading.Event()

    def shutdown(signum, _frame):
        log.info("received %s, shutting down",
                 signal.Signals(signum).name if hasattr(signal, "Signals") else signum)
        stopping.set()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        # Event.wait rather than signal.pause: pause() does not exist on
        # Windows, and a bare sleep loop delays shutdown by up to its interval.
        while not stopping.wait(timeout=1.0):
            pass
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
