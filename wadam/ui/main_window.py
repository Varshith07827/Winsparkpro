"""The main window — a chat list and, when you pick one, where it sends.

    ┌───────────────────────────────┬──────────────────────────────────────┐
    │ profile                    ⟳  │ Alice                                │
    │ search                        ├──────────────────────────────────────┤
    │───────────────────────────────│ Webhook                              │
    │ ☑ Alice        Hello there  3 │ https://noteify.org/ntext/whook/?…   │
    │ ☐ Team chat    Are you…       │                                      │
    │ ☑ Bob          Send the file  │                                      │
    ├───────────────────────────────┴──────────────────────────────────────┤
    │ status bar: poll cadence · queue · MongoDB · JSON                    │
    └──────────────────────────────────────────────────────────────────────┘

**The checkbox is the entire user interface.** Ticking it turns a chat's
automation on, immediately, with no dialog and no save button; clicking the row
selects it and never changes it. The badge counts messages that have arrived
and not yet finished the round trip — not WhatsApp's unread count, which the
user can already see in WhatsApp.

The two directions are not symmetrical, and the window is where that shows.
Chats arrive already ticked, so ticking is usually just switching one back on
and stays instant and silent. **Unticking deletes that chat's stored records**,
which is the only irreversible thing a user can do here, so it is the only
thing that asks — naming what will be destroyed rather than saying "all
records", and refusing outright when the database is unreachable.

What used to be here and is not any more: a global automation switch, a rescan
button, and a right-hand panel of six cards carrying activity history, session
health, operations counters, storage status and Export/Reset/Delete. The
subsystems behind them all still exist and still run; none of them is something
a user of this tool has to look at, and all of them remain in the logs.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from wadam import constants
from wadam.config import Settings
from wadam.engine.engine import EngineSnapshot
from wadam.storage.repository import Repository
from wadam.ui import theme
from wadam.ui.chat_list import ChatListPanel
from wadam.ui.chat_details import ChatDetailsPanel
from wadam.ui.engine_host import EngineHost

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from wadam.api.host import SendApiHost

logger = logging.getLogger(__name__)


@contextmanager
def _busy_cursor():
    QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, repository: Repository, host: EngineHost,
                 api: Optional["SendApiHost"] = None) -> None:
        super().__init__()
        self._settings = settings
        self._repository = repository
        self._host = host
        self._api = api
        self._snapshot: Optional[EngineSnapshot] = None

        self.setWindowTitle(f"{constants.APP_NAME} {constants.APP_VERSION}")
        self.resize(1280, 820)
        self.setMinimumSize(980, 620)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)

        self._chat_list = ChatListPanel()
        self._chat_list.chat_selected.connect(self._on_chat_selected)
        # The checkbox in the list IS the automation control. There is no
        # second one on the right, and no save button anywhere.
        self._chat_list.automation_toggled.connect(self._on_automation_toggled)
        self._chat_list.refresh_requested.connect(self._on_rescan)
        splitter.addWidget(self._chat_list)

        self._config = ChatDetailsPanel()
        self._config.phone_saved.connect(self._on_phone_saved)
        self._config.webhook_saved.connect(self._on_webhook_saved)
        splitter.addWidget(self._config)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 900])
        layout.addWidget(splitter, 1)

        layout.addWidget(self._build_status_bar())
        self.setCentralWidget(central)

        host.snapshot_ready.connect(self._on_snapshot)
        host.engine_stopped.connect(self._on_engine_stopped)

        # No repainting timer. The panel used to show relative times ("2m ago")
        # and needed one; it now shows a name, a number and a URL, none of which
        # age. Every update arrives with a snapshot, and the timer's only
        # remaining effect was to overwrite a half-typed phone number.

    # -- chrome ------------------------------------------------------------


    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("statusBar")
        bar.setFixedHeight(30)
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(20)

        self._status_poll = QLabel("polling every 3s")
        self._status_queue = QLabel("")
        self._status_api = QLabel("")
        self._status_mongo = QLabel("MongoDB —")
        self._status_json = QLabel("JSON —")
        for widget in (self._status_poll, self._status_queue):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self._status_api)
        row.addWidget(self._status_mongo)
        row.addWidget(self._status_json)
        return bar

    # -- snapshots ---------------------------------------------------------

    def _on_snapshot(self, snapshot: EngineSnapshot) -> None:
        self._snapshot = snapshot
        self._chat_list.set_chats(snapshot.chats, snapshot.whatsapp_found)



        self._status_poll.setText(
            f"cycle {snapshot.cycle_count} · {snapshot.last_cycle_ms}ms · "
            f"every {constants.POLL_INTERVAL_SECONDS}s"
        )
        # Two different queues, and conflating them hides the important one:
        # `queued_jobs` is chats waiting to be read, `queue_depth` is messages
        # waiting to be delivered.
        parts = []
        if snapshot.queued_jobs:
            parts.append(f"{snapshot.queued_jobs} to read")
        if snapshot.queue_depth:
            parts.append(f"{snapshot.queue_depth} to send")
        self._status_queue.setText(" · ".join(parts) if parts else "queue empty")
        self._refresh_api_status()
        self._set_status(self._status_mongo, f"MongoDB {snapshot.mongo_status}", snapshot.mongo_ok)
        self._set_status(self._status_json, f"JSON {snapshot.json_status}", snapshot.json_ok)

        self._refresh_selected()

    def _refresh_api_status(self) -> None:
        """The send API is a listening socket, not something the engine polls,
        so its state is read directly rather than carried on a snapshot."""
        if self._api is None or not self._api.enabled:
            self._status_api.setText("")
            return
        running = self._api.server.running
        self._set_status(
            self._status_api,
            f"send API {self._api.server.status_text}" if running else "send API not listening",
            running,
        )

    @staticmethod
    def _set_status(label: QLabel, text: str, ok: bool) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {theme.TEXT_MUTED if ok else theme.DANGER};")

    def _refresh_selected(self) -> None:
        chat_id = self._chat_list.selected_id
        self._config.set_chat(self._repository.get_chat(chat_id) if chat_id else None)

    def _on_engine_stopped(self, error: str) -> None:
        if error:
            QMessageBox.critical(self, "Automation engine stopped", error)

    # -- actions -----------------------------------------------------------

    def _on_phone_saved(self, chat_id: str, phone_number: str) -> None:
        self._host.submit(
            lambda: self._host.engine.set_chat_phone_number(chat_id, phone_number))

    def _on_webhook_saved(self, chat_id: str, url: str) -> None:
        self._host.submit(lambda: self._host.engine.set_chat_webhook(chat_id, url))

    def _on_chat_selected(self, chat_id: str) -> None:
        self._config.set_chat(self._repository.get_chat(chat_id))

    def _on_automation_toggled(self, chat_id: str, enabled: bool) -> None:
        if not enabled and not self._confirm_purge(chat_id):
            return
        self._host.submit(lambda: self._host.engine.set_chat_automation(chat_id, enabled))

    def _confirm_purge(self, chat_id: str) -> bool:
        """Unticking deletes the chat's stored history, so it asks first.

        The one dialog in an interface that deliberately has none. Ticking a box
        is still immediate and silent; this is the other direction, where a
        stray click on a 14-pixel target would otherwise destroy a history that
        nothing can restore. It names the counts rather than saying "all
        records", because "delete everything?" tells a user nothing about what
        they are about to lose."""
        chat = self._repository.get_chat(chat_id)
        if chat is None:
            return False

        # Refused rather than half-done. The purge deletes from MongoDB and from
        # the JSON backup; with the database unreachable only the backup would
        # go, and the UI would report a deletion that did not happen.
        if self._snapshot is not None and not self._snapshot.mongo_ok:
            QMessageBox.warning(
                self, "Database unreachable",
                f"Turning automation off deletes this chat's stored records, and "
                f"{self._snapshot.mongo_status}.\n\n"
                "Nothing was changed. Try again once the database is back.",
            )
            return False

        with _busy_cursor():
            counts = self._repository.chat_record_counts(chat_id)
        total = sum(counts.values())
        if not total:
            return True      # nothing to lose, so nothing to ask about
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Warning)
        confirm.setWindowTitle("Turn off automation")
        confirm.setText(f"Delete everything stored for {chat.chat_name}?")
        confirm.setInformativeText(
            f"{counts['messages']} message(s), {counts['webhooks']} webhook call(s) "
            f"and {counts['outgoing']} queued send(s) will be removed from "
            f"{self._settings.database_name}.\n\n"
            "The chat stays in the list and can be switched back on, but this "
            "history cannot be recovered."
        )
        confirm.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        confirm.setDefaultButton(QMessageBox.Cancel)
        confirm.button(QMessageBox.Yes).setText("Turn off and delete")
        return confirm.exec() == QMessageBox.Yes





    def _on_rescan(self) -> None:
        """Manual refresh: re-read the whole WhatsApp chat list.

        Deliberately only a re-read. It does not touch the outgoing queue, does
        not reset any message state and cannot duplicate anything — the drainer
        owns all of that and is not consulted here."""
        self._host.submit(lambda: self._host.engine.rescan())





    # -- theming -----------------------------------------------------------

    def restyle(self) -> None:
        """Re-apply everything that isn't driven by the application stylesheet,
        after the system switched between light and dark."""
        self._chat_list.restyle()
        if self._snapshot is not None:
            self._set_status(self._status_mongo,
                             f"MongoDB {self._snapshot.mongo_status}", self._snapshot.mongo_ok)
            self._set_status(self._status_json,
                             f"JSON {self._snapshot.json_status}", self._snapshot.json_ok)

    # -- shutdown ----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # No timer to stop: the repainting tick was removed when the details
        # panel stopped showing anything time-dependent. This method kept
        # calling it and raised AttributeError on every close, which skipped
        # the engine and repository shutdown below — the final JSON flush only
        # happened because app.py's `finally` repeats these calls.
        self._host.stop()
        try:
            self._repository.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Shutdown persistence failed")
        event.accept()
