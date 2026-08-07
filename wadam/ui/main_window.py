"""The main window — WhatsApp Desktop's layout, with configuration where the
conversation would be.

    ┌───────────────────────────────┬──────────────────────────────────────┐
    │ profile                       │ chat name          [Read now]        │
    │ search                        ├──────────────────────────────────────┤
    │───────────────────────────────│ Automation   enabled · webhook URL   │
    │ ● Alice      12:04   [3][AUTO]│ Activity     last poll / in / out /  │
    │ ● Team chat  11:58      [HOOK]│              webhook / retries       │
    │ ● Bob        Yesterday        │ Storage      MongoDB · JSON          │
    │ …                             │ [Export] [Reset]          [Delete]   │
    ├───────────────────────────────┴──────────────────────────────────────┤
    │ status bar: poll cadence · queue · MongoDB · JSON                    │
    └──────────────────────────────────────────────────────────────────────┘

The global ON/OFF sits top-right, as specified. It is a bulk action across every
chat, not a master gate — after pressing it, individual chats can still be
toggled and their setting stands.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
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
from wadam.ui.config_panel import ChatConfigPanel
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

        layout.addWidget(self._build_top_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)

        self._chat_list = ChatListPanel()
        self._chat_list.chat_selected.connect(self._on_chat_selected)
        splitter.addWidget(self._chat_list)

        self._config = ChatConfigPanel()
        self._config.automation_toggled.connect(self._on_automation_toggled)
        self._config.webhook_saved.connect(self._on_webhook_saved)
        self._config.external_id_saved.connect(self._on_external_id_saved)
        self._config.webhook_tested.connect(self._on_webhook_tested)
        self._config.scan_requested.connect(self._on_scan_requested)
        self._config.export_requested.connect(self._on_export_requested)
        self._config.delete_requested.connect(self._on_delete_requested)
        self._config.reset_requested.connect(self._on_reset_requested)
        splitter.addWidget(self._config)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 900])
        layout.addWidget(splitter, 1)

        layout.addWidget(self._build_status_bar())
        self.setCentralWidget(central)

        host.snapshot_ready.connect(self._on_snapshot)
        host.engine_stopped.connect(self._on_engine_stopped)

        # The snapshot carries relative times ("2m ago") that go stale between
        # polls, so the selected chat's panel is re-rendered on a slow tick too.
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._refresh_selected)
        self._tick.start()

    # -- chrome ------------------------------------------------------------

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("panelHeader")
        bar.setFixedHeight(52)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 16, 0)
        row.setSpacing(12)

        title = QLabel(constants.APP_NAME)
        title.setObjectName("railTitle")
        row.addWidget(title)

        self._engine_state = QLabel("starting…")
        self._engine_state.setObjectName("configSubtitle")
        row.addWidget(self._engine_state)
        row.addStretch(1)

        self._rescan_button = QPushButton("Rescan chats")
        self._rescan_button.setToolTip(
            "Scroll the whole WhatsApp chat list and register every chat found.\n"
            "The ordinary poll only reads the chats WhatsApp has on screen."
        )
        self._rescan_button.clicked.connect(self._on_rescan)
        row.addWidget(self._rescan_button)

        label = QLabel("Automation")
        label.setObjectName("fieldLabel")
        row.addWidget(label)

        self._global_toggle = QPushButton("OFF")
        self._global_toggle.setObjectName("globalToggle")
        self._global_toggle.setCheckable(True)
        self._global_toggle.setFixedWidth(74)
        self._global_toggle.setToolTip(
            "Turn automation on or off for EVERY chat at once.\n"
            "Individual chats can still be changed afterwards."
        )
        self._global_toggle.clicked.connect(self._on_global_toggled)
        row.addWidget(self._global_toggle)
        return bar

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

        self._global_toggle.blockSignals(True)
        self._global_toggle.setChecked(snapshot.global_automation)
        self._global_toggle.setText("ON" if snapshot.global_automation else "OFF")
        self._global_toggle.blockSignals(False)

        if not snapshot.whatsapp_found:
            self._engine_state.setText("waiting for WhatsApp Desktop")
        elif snapshot.busy_with:
            self._engine_state.setText(f"reading {snapshot.busy_with}…")
        elif snapshot.last_error:
            self._engine_state.setText(snapshot.last_error[:80])
        else:
            self._engine_state.setText(f"watching {len(snapshot.chats)} chats")

        self._status_poll.setText(
            f"cycle {snapshot.cycle_count} · {snapshot.last_cycle_ms}ms · "
            f"every {constants.POLL_INTERVAL_SECONDS}s"
        )
        self._status_queue.setText(
            f"queue {snapshot.queued_jobs}" if snapshot.queued_jobs else "queue empty"
        )
        self._refresh_api_status()
        self._set_status(self._status_mongo, f"MongoDB {snapshot.mongo_status}", snapshot.mongo_ok)
        self._set_status(self._status_json, f"JSON {snapshot.json_status}", snapshot.json_ok)

        self._config.set_session_health(snapshot.session_rows, snapshot.send_blocked_reason)
        self._config.set_storage_status(
            snapshot.mongo_status, snapshot.mongo_ok, snapshot.json_status, snapshot.json_ok
        )
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
            self._engine_state.setText("engine stopped")
            QMessageBox.critical(self, "Automation engine stopped", error)

    # -- actions -----------------------------------------------------------

    def _on_chat_selected(self, chat_id: str) -> None:
        self._config.set_chat(self._repository.get_chat(chat_id))

    def _on_automation_toggled(self, chat_id: str, enabled: bool) -> None:
        self._host.submit(lambda: self._host.engine.set_chat_automation(chat_id, enabled))

    def _on_webhook_saved(self, chat_id: str, url: str) -> None:
        future = self._host.submit(lambda: self._host.engine.set_webhook(chat_id, url))

        def report(finished) -> None:
            # The panel already showed "Saved." optimistically, which is right
            # for the common case. If the write actually failed, say so rather
            # than leaving a success message over a change that didn't happen.
            error = finished.exception()
            if error is not None:
                self._config.report_save_failed(str(error))

        future.add_done_callback(report)

    def _on_external_id_saved(self, chat_id: str, external_id: str) -> None:
        self._host.submit(lambda: self._host.engine.set_external_id(chat_id, external_id))

    def _on_webhook_tested(self, chat_id: str) -> None:
        future = self._host.submit(lambda: self._host.engine.test_webhook(chat_id))
        # The result is what the user asked for, so this one waits — with a busy
        # cursor, because a window that stops repainting for twenty seconds
        # without saying why looks like a crash.
        try:
            with _busy_cursor():
                outcome = future.result(timeout=self._settings.webhook_timeout + 10)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, "Webhook test", f"The test could not run:\n\n{ex}")
            return
        body = outcome.reply_text or outcome.body or outcome.error or "(empty response)"
        message = f"{outcome.status_text}\nAttempts: {outcome.attempts}\n\n{body[:1200]}"
        if outcome.ok:
            QMessageBox.information(self, "Webhook test", message)
        else:
            QMessageBox.warning(self, "Webhook test", message)

    def _on_scan_requested(self, chat_id: str) -> None:
        self._host.submit(lambda: self._host.engine.scan_chat_now(chat_id))

    def _on_rescan(self) -> None:
        self._rescan_button.setEnabled(False)
        future = self._host.submit(lambda: self._host.engine.rescan())

        def done(_f) -> None:
            self._rescan_button.setEnabled(True)

        future.add_done_callback(done)

    def _on_export_requested(self, chat_id: str) -> None:
        chat = self._repository.get_chat(chat_id)
        if chat is None:
            return
        safe_name = "".join(c for c in chat.chat_name if c.isalnum() or c in " -_").strip() or "chat"
        default = str(self._settings.json_backup_folder / "exports" / f"{safe_name}.json")
        path, _filter = QFileDialog.getSaveFileName(self, "Export chat", default, "JSON (*.json)")
        if not path:
            return
        future = self._host.submit(lambda: self._host.engine.export_chat(chat_id, Path(path)))
        try:
            with _busy_cursor():
                written = future.result(timeout=30)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, "Export failed", str(ex))
            return
        QMessageBox.information(self, "Export complete", f"Written to:\n{written}")

    def _on_reset_requested(self, chat_id: str) -> None:
        chat = self._repository.get_chat(chat_id)
        if chat is None:
            return
        confirmed = QMessageBox.question(
            self, "Reset automation",
            f"Reset automation for “{chat.chat_name}”?\n\n"
            "Automation will be turned off, the webhook status and counters cleared, and the\n"
            "message backlog re-baselined so nothing already on screen is answered.\n\n"
            "The webhook URL and the stored messages are kept.",
        )
        if confirmed == QMessageBox.Yes:
            self._host.submit(lambda: self._host.engine.reset_automation(chat_id))

    def _on_delete_requested(self, chat_id: str) -> None:
        chat = self._repository.get_chat(chat_id)
        if chat is None:
            return
        confirmed = QMessageBox.question(
            self, "Delete chat",
            f"Delete “{chat.chat_name}” and its {chat.messages_stored} stored message(s)?\n\n"
            "This removes the configuration, messages and webhook history from MongoDB and\n"
            "the JSON backup. Nothing in WhatsApp is affected — the chat will be discovered\n"
            "again on the next poll, with a clean configuration.",
        )
        if confirmed == QMessageBox.Yes:
            self._host.submit(lambda: self._host.engine.delete_chat(chat_id))

    def _on_global_toggled(self, checked: bool) -> None:
        count = len(self._snapshot.chats) if self._snapshot else 0
        confirmed = QMessageBox.question(
            self, "Automation " + ("ON" if checked else "OFF"),
            f"Turn automation {'ON' if checked else 'OFF'} for all {count} chats?\n\n"
            + ("Every chat with a webhook URL will start replying to incoming messages."
               if checked else
               "No chat will reply until it is switched back on."),
        )
        if confirmed != QMessageBox.Yes:
            self._global_toggle.blockSignals(True)
            self._global_toggle.setChecked(not checked)
            self._global_toggle.blockSignals(False)
            return
        self._global_toggle.setText("ON" if checked else "OFF")
        self._host.submit(lambda: self._host.engine.set_global_automation(checked))

    # -- theming -----------------------------------------------------------

    def restyle(self) -> None:
        """Re-apply everything that isn't driven by the application stylesheet,
        after the system switched between light and dark."""
        self._chat_list.restyle()
        self._config.restyle()
        if self._snapshot is not None:
            self._set_status(self._status_mongo,
                             f"MongoDB {self._snapshot.mongo_status}", self._snapshot.mongo_ok)
            self._set_status(self._status_json,
                             f"JSON {self._snapshot.json_status}", self._snapshot.json_ok)

    # -- shutdown ----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._tick.stop()
        self._host.stop()
        try:
            self._repository.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Shutdown persistence failed")
        event.accept()
