"""The main window — a chat list and, when you pick one, where it sends.

    ┌───────────────────────────────┬──────────────────────────────────────┐
    │ search                        │ Alice                                │
    │───────────────────────────────├──────────────────────────────────────┤
    │ ☑ Alice        Hello there  3 │  ← ping                    9:21 pm   │
    │ ☐ Team chat    Are you…       │  → pong                    9:21 pm   │
    │ ☑ Bob          Send the file  │                                      │
    ├───────────────────────────────┴──────────────────────────────────────┤
    │ session ready · 12 delivered · 4 replied · MongoDB · JSON            │
    └──────────────────────────────────────────────────────────────────────┘

The tick box is the only control. Everything else is a read of what happened.

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
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
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
from wadam.engine.service import EngineSnapshot
from wadam.storage.repository import Repository
from wadam.ui import theme
from wadam.ui.chat_list import ChatListPanel
from wadam.ui.chat_details import ChatDetailsPanel
from wadam.ui.engine_host import EngineHost

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from wadam.api.host import SendApiHost

logger = logging.getLogger(__name__)


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
        splitter.addWidget(self._chat_list)

        self._config = ChatDetailsPanel()
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

        self._status_session = QLabel("session —")
        self._status_listen = QLabel("")
        self._status_api = QLabel("")
        self._status_mongo = QLabel("MongoDB —")
        self._status_json = QLabel("JSON —")
        for widget in (self._status_session, self._status_listen):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self._status_api)
        row.addWidget(self._status_mongo)
        row.addWidget(self._status_json)
        return bar

    # -- snapshots ---------------------------------------------------------

    def _on_snapshot(self, snapshot: EngineSnapshot) -> None:
        self._snapshot = snapshot
        self._chat_list.set_chats(snapshot.chats, snapshot.openwa_ok)

        phone = f" · {snapshot.session_phone}" if snapshot.session_phone else ""
        self._set_status(
            self._status_session,
            f"session {snapshot.session_status}{phone}",
            snapshot.openwa_ok,
        )
        # What the listener has actually done, which is the question asked when
        # a chat is ticked and nothing happens. A delivery count of zero says
        # "OpenWA is not reaching this process" far more clearly than a green
        # session light says the opposite.
        metrics = snapshot.metrics
        if snapshot.listening:
            summary = (f"{snapshot.contact_count} contacts · "
                       f"{metrics.deliveries} delivered · {metrics.replies_sent} replied")
            if metrics.webhook_failures:
                summary += f" · {metrics.webhook_failures} hook failed"
            if metrics.send_failures:
                summary += f" · {metrics.send_failures} failed"
            if metrics.rejected:
                summary += f" · {metrics.rejected} unsigned"
            self._status_listen.setText(summary)
        else:
            self._status_listen.setText("not listening")

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
        self._show_chat(chat_id)

    def _on_engine_stopped(self, error: str) -> None:
        if error:
            QMessageBox.critical(self, "Automation engine stopped", error)

    # -- actions -----------------------------------------------------------

    def _on_chat_selected(self, chat_id: str) -> None:
        self._show_chat(chat_id)

    def _show_chat(self, chat_id: str) -> None:
        """Render one chat and its transcript, or the empty state."""
        if not chat_id:
            self._config.set_chat(None, [])
            return
        chat = self._repository.get_chat(chat_id)
        messages = self._repository.messages_for(chat_id) if chat else []
        self._config.set_chat(chat, messages)

    def _on_webhook_saved(self, chat_id: str, url: str) -> None:
        self._host.service.set_chat_webhook(chat_id, url)

    def _on_automation_toggled(self, chat_id: str, enabled: bool) -> None:
        """The one control in the window. Immediate and silent, both ways.

        winSpark asked for confirmation here, because unticking also *deleted*
        everything the chat had stored and a stray click on a 14-pixel target
        would destroy a history nothing could restore. Turning automation off
        no longer deletes anything — it stops replies — so the dialog that
        existed to guard the deletion went with it.
        """
        self._host.service.set_chat_automation(chat_id, enabled)

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
