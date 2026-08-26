"""Application entry point.

    load  →  validate  →  launch
                 ↓
        startup error screen (with Retry)

The sequence is deliberately linear and every step can only fail in one way,
because "it didn't start and I don't know why" is the worst outcome for an
application that is meant to run unattended once configured.

After this, no further intervention is expected: the service listens for
OpenWA's webhook deliveries, registers whatever chat is new, stores every
message in MongoDB and the JSON mirror, and answers the chats that are
switched on — sending back through OpenWA's API.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from wadam import constants
from wadam.api.host import SendApiHost
from wadam.config import ConfigError, Settings, default_env_path, load_settings
from wadam.logging_setup import configure_logging
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.mongo import MongoStore, MongoUnavailableError
from wadam.storage.repository import Repository
from wadam.ui import theme
from wadam.reply import reply_for
from wadam.ui.engine_host import EngineHost
from wadam.ui.main_window import MainWindow
from wadam.ui.first_run import START, FirstRunDialog, needs_setup
from wadam.ui.startup import RETRY, StartupErrorDialog, StartupWarningDialog

logger = logging.getLogger(__name__)


class _Startup:
    """One attempt at getting from nothing to a running window."""

    def __init__(self) -> None:
        self.settings: Optional[Settings] = None
        self.repository: Optional[Repository] = None
        self.mongo: Optional[MongoStore] = None
        self.warnings: list[str] = []

    def attempt(self) -> Optional[StartupErrorDialog]:
        """Returns None on success, or the dialog to show on failure."""
        # 1. Load + validate configuration.
        try:
            settings = load_settings()
        except ConfigError as ex:
            return StartupErrorDialog(
                "Configuration problem", ex.problems,
                hint="Copy .env.example to .env and fill it in, then press Retry.",
            )
        self.settings = settings
        self.warnings = list(settings.warnings)

        log_path = configure_logging(settings.log_level, settings.json_backup_folder)
        logger.info("%s %s starting", constants.APP_NAME, constants.APP_VERSION)
        if log_path:
            logger.info("Diagnostic log: %s", log_path)

        # 2. The JSON mirror must be writable. A backup nobody can write is not
        #    a backup, and finding that out during a crash is too late.
        backup = JsonBackupStore(settings.json_backup_folder, settings.json_autosave_interval)
        try:
            backup.ensure_folder()
        except OSError as ex:
            return StartupErrorDialog(
                "The JSON backup folder is not writable",
                [f"{settings.json_backup_folder}", str(ex)],
                env_path=settings.env_path,
                hint="Set JSON_BACKUP_FOLDER in .env to a folder this account can write to.",
            )

        # 3. MongoDB is the primary store — required, and verified with a real
        #    round-trip rather than pymongo's lazy connect.
        mongo = MongoStore(settings.mongodb_uri, settings.database_name)
        try:
            mongo.connect()
        except MongoUnavailableError as ex:
            return StartupErrorDialog(
                "MongoDB is not reachable", [str(ex)],
                env_path=settings.env_path,
                hint="Check MONGODB_URI in .env. For a local server, confirm mongod is running; "
                     "for Atlas, check the cluster is awake and this machine's IP is allowed.",
            )
        self.mongo = mongo

        repository = Repository(settings, mongo, backup)
        repository.start()
        self.repository = repository
        if repository.recovered_from_json:
            self.warnings.append(
                "MongoDB held no chats, so the configuration was restored from the JSON backup "
                "and written back to the database."
            )
        return None


def _install_theme(app: QApplication, window=None) -> None:
    """Paint with the operating system's light/dark preference, and keep
    following it if the user changes it while the application is open."""
    app.setStyleSheet(theme.apply(theme.detect_scheme(app)))
    if window is not None and hasattr(window, "restyle"):
        window.restyle()


def main(argv: Optional[list[str]] = None) -> int:
    QApplication.setAttribute(Qt.AA_DontUseNativeMenuBar, False)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(constants.APP_NAME)
    app.setApplicationVersion(constants.APP_VERSION)
    app.setStyle("Fusion")
    _install_theme(app)
    app.setFont(QFont("Segoe UI", 10))

    startup = _Startup()
    # First run: ask the handful of things the application cannot work out for
    # itself, write .env, and never ask again. Anything already configured
    # skips straight past this.
    env_path = default_env_path()
    if needs_setup(env_path):
        if FirstRunDialog(env_path).exec() != START:
            return 2

    while True:
        dialog = startup.attempt()
        if dialog is None:
            break
        if dialog.exec() != RETRY:
            return 2

    settings = startup.settings
    repository = startup.repository
    assert settings is not None and repository is not None  # attempt() succeeded

    if startup.warnings:
        StartupWarningDialog(startup.warnings).exec()

    host = EngineHost(settings, repository, reply_for)
    api = SendApiHost(settings, repository, host.service)
    window = MainWindow(settings, repository, host, api)

    try:
        app.styleHints().colorSchemeChanged.connect(
            lambda _scheme: _install_theme(app, window)
        )
    except (AttributeError, RuntimeError):  # pragma: no cover - Qt older than 6.5
        logger.debug("This Qt build cannot report system theme changes", exc_info=True)

    window.show()
    host.start()

    # After the listener, because a request arriving before the service exists
    # would fail for a reason the caller could do nothing about.
    if api.enabled:
        try:
            api.start()
        except OSError as ex:
            # A listener the caller believes is running but isn't would fail
            # silently forever, so this is said out loud rather than logged.
            QMessageBox.warning(
                window, "Send API could not start",
                f"The send API could not bind to {settings.api_host}:{settings.api_port}.\n\n"
                f"{ex}\n\nThe rest of the application is running normally; incoming send "
                f"requests will not be accepted until this is resolved.",
            )
            logger.error("Send API failed to start: %s", ex)

    try:
        return app.exec()
    finally:
        api.stop()
        host.stop()
        repository.stop()
        if startup.mongo is not None:
            startup.mongo.close()
        logger.info("%s stopped", constants.APP_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
