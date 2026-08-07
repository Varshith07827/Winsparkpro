"""The right panel — where WhatsApp Desktop puts the conversation, this puts
that chat's automation configuration and status.

Selecting a chat never shows a conversation. It shows: webhook URL, automation
enabled, webhook status, messages stored, MongoDB status, JSON backup status,
retry count, last poll, last incoming, last outgoing, chat ID — and the delete,
export and reset actions.

**The webhook field always shows the selected chat's URL.** That sounds too
obvious to state, but it is the one behaviour here with a footnote: the panel
re-renders every second so relative times stay honest, and it deliberately does
*not* overwrite the URL field while it holds unsaved text — losing what someone
is halfway through typing because a poll came round is a small betrayal that
makes a tool feel hostile. That preservation applies only while re-rendering the
*same* chat. Clicking a different chat always reloads the field, because the
text sitting there belongs to the chat you just left.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from wadam.domain.models import ChatConfig
from wadam.ui import theme


def _format_time(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    local = value.astimezone() if value.tzinfo else value.replace(tzinfo=timezone.utc).astimezone()
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - aware).total_seconds())
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago · {local:%H:%M}"
    if local.date() == datetime.now().date():
        return f"{local:%H:%M:%S}"
    return f"{local:%d %b %H:%M}"


def _truncate(text: str, limit: int = 220) -> str:
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return "—"
    return text if len(text) <= limit else text[:limit] + "…"


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """Is this something the dispatcher can actually POST to?

    Empty is valid — it means "no webhook", which is how every chat starts and
    a legitimate way to park one. Anything else has to be an absolute http(s)
    URL with a host, because those are the only two things the client speaks
    and a typo like `htp://` or a bare `example.com/hook` should be caught here
    rather than discovered as a failed delivery an hour later."""
    text = (url or "").strip()
    if not text:
        return True, ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return False, "The URL must start with http:// or https://"
    if not parsed.netloc:
        return False, "The URL has no host — expected something like https://example.com/hook"
    if " " in text:
        return False, "The URL contains a space"
    return True, ""


class _Card(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 16, 18, 16)
        self._layout.setSpacing(12)
        if title:
            label = QLabel(title.upper())
            label.setObjectName("sectionTitle")
            self._layout.addWidget(label)

    def add(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)


class ChatConfigPanel(QWidget):
    automation_toggled = Signal(str, bool)
    webhook_saved = Signal(str, str)
    external_id_saved = Signal(str, str)
    webhook_tested = Signal(str)
    scan_requested = Signal(str)
    export_requested = Signal(str)
    delete_requested = Signal(str)
    reset_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("configPanel")
        self._chat: Optional[ChatConfig] = None
        # The value the field was last loaded or saved with. "Dirty" is the
        # field differing from THIS, not from whatever chat is now selected —
        # comparing against the newly selected chat is what made a chat show
        # the previous chat's webhook.
        self._loaded_webhook = ""
        self._loaded_external_id = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        # Status text can be long (a webhook body, an error). It wraps; the
        # panel must never scroll sideways to reach a button.
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        body = QWidget()
        # The cards live in a width-capped, centred column. Left unconstrained
        # on a wide monitor, a webhook URL field grows to 1,600px and the status
        # values drift a hand's width away from their labels.
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(24, 20, 24, 24)
        body_row.addStretch(1)
        column = QWidget()
        column.setMaximumWidth(1040)
        body_row.addWidget(column, 12)
        body_row.addStretch(1)

        self._body_layout = QVBoxLayout(column)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(16)
        self._body_layout.addWidget(self._build_automation_card())
        self._body_layout.addWidget(self._build_contact_id_card())
        self._body_layout.addWidget(self._build_activity_card())
        self._body_layout.addWidget(self._build_health_card())
        self._body_layout.addWidget(self._build_storage_card())
        self._body_layout.addLayout(self._build_actions())
        self._body_layout.addStretch(1)
        self._scroll.setWidget(body)
        outer.addWidget(self._scroll, 1)

        self._empty = QLabel(
            "Select a chat to configure its automation.\n\n"
            "Chats appear here automatically as they are discovered in WhatsApp Desktop.\n"
            "New chats start with automation OFF and no webhook."
        )
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setObjectName("fieldValueMuted")
        outer.addWidget(self._empty, 1)

        # Clears the transient "Saved" / "Not a valid URL" line.
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(lambda: self._set_feedback("", ""))

        self.set_chat(None)

    # -- construction ------------------------------------------------------

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("panelHeader")
        header.setFixedHeight(60)
        row = QHBoxLayout(header)
        row.setContentsMargins(20, 0, 16, 0)
        row.setSpacing(12)

        self._avatar = QLabel("—")
        self._avatar.setFixedSize(40, 40)
        self._avatar.setAlignment(Qt.AlignCenter)
        row.addWidget(self._avatar)

        column = QVBoxLayout()
        column.setSpacing(0)
        self._title = QLabel("No chat selected")
        self._title.setObjectName("configTitle")
        self._subtitle = QLabel("")
        self._subtitle.setObjectName("configSubtitle")
        column.addWidget(self._title)
        column.addWidget(self._subtitle)
        row.addLayout(column, 1)

        self._scan_button = QPushButton("Read now")
        self._scan_button.setToolTip(
            "Open this chat in WhatsApp and read it immediately, instead of waiting\n"
            "for the next poll. This switches the conversation WhatsApp is showing."
        )
        self._scan_button.clicked.connect(
            lambda: self._chat and self.scan_requested.emit(self._chat.chat_id)
        )
        row.addWidget(self._scan_button)
        return header

    def _build_automation_card(self) -> QWidget:
        card = _Card("Automation")

        self._enabled = QCheckBox("Enabled — reply to incoming messages via the webhook")
        self._enabled.toggled.connect(self._on_enabled_toggled)
        card.add(self._enabled)

        label = QLabel("Webhook URL")
        label.setObjectName("fieldLabel")
        card.add(label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._webhook = QLineEdit()
        self._webhook.setPlaceholderText("https://example.com/whatsapp-hook")
        self._webhook.setClearButtonEnabled(True)
        self._webhook.returnPressed.connect(self._save_webhook)
        self._webhook.textEdited.connect(self._on_webhook_edited)
        row.addWidget(self._webhook, 1)

        self._save_button = QPushButton("Save")
        self._save_button.setObjectName("primary")
        self._save_button.clicked.connect(self._save_webhook)
        row.addWidget(self._save_button)

        self._test_button = QPushButton("Test")
        self._test_button.setToolTip("POST a webhook.test payload to this URL and show the result.")
        self._test_button.clicked.connect(
            lambda: self._chat and self.webhook_tested.emit(self._chat.chat_id)
        )
        row.addWidget(self._test_button)
        card.add_layout(row)

        self._feedback = QLabel("")
        self._feedback.setWordWrap(True)
        self._feedback.setVisible(False)
        card.add(self._feedback)

        hint = QLabel(
            "Every incoming message is POSTed as JSON. Reply with "
            "<code>{\"reply\": \"…\"}</code> — or plain text — and it is sent back to the chat. "
            "An empty reply means \"seen, no answer\"."
        )
        hint.setWordWrap(True)
        hint.setObjectName("fieldValueMuted")
        card.add(hint)
        return card

    def _build_contact_id_card(self) -> QWidget:
        card = _Card("Contact ID")

        row = QHBoxLayout()
        row.setSpacing(8)
        self._external_id = QLineEdit()
        self._external_id.setPlaceholderText("9423")
        self._external_id.setMaximumWidth(160)
        self._external_id.returnPressed.connect(self._save_external_id)
        self._external_id.textEdited.connect(self._on_external_id_edited)
        row.addWidget(self._external_id)

        self._external_id_save = QPushButton("Save")
        self._external_id_save.clicked.connect(self._save_external_id)
        row.addWidget(self._external_id_save)
        row.addStretch(1)
        card.add_layout(row)

        self._external_id_feedback = QLabel("")
        self._external_id_feedback.setWordWrap(True)
        self._external_id_feedback.setVisible(False)
        card.add(self._external_id_feedback)

        hint = QLabel(
            "How the send API addresses this chat — by default the last four digits of the "
            "contact's number, filled in automatically when the chat name is the number "
            "itself. A saved contact shows a name and never its number, so type it in here.<br><br>"
            "Four digits is only 10,000 values: if two chats end up sharing one, a send to it "
            "is <b>refused</b> rather than delivered to the wrong person. Give one of them "
            "something longer."
        )
        hint.setWordWrap(True)
        hint.setObjectName("fieldValueMuted")
        card.add(hint)
        return card

    def _build_activity_card(self) -> QWidget:
        card = _Card("Activity")
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self._values: dict[str, QLabel] = {}
        rows = [
            ("last_poll", "Last poll"),
            ("last_incoming", "Last incoming message"),
            ("last_outgoing", "Last outgoing message"),
            ("webhook_status", "Webhook status"),
            ("relay_status", "Relay (webhook poll)"),
            ("last_webhook", "Last webhook response"),
            ("retries", "Webhook retry count"),
            ("stored", "Messages stored"),
            ("error", "Last error"),
        ]
        for index, (key, caption) in enumerate(rows):
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            value = QLabel("—")
            value.setObjectName("fieldValue")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            grid.addWidget(label, index, 0, Qt.AlignTop)
            grid.addWidget(value, index, 1)
            self._values[key] = value
        card.add_layout(grid)
        return card

    def _build_health_card(self) -> QWidget:
        """Session health — the preconditions a send depends on, stated before
        it fails rather than after."""
        card = _Card("Session health")
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self._health_rows: list[tuple[QLabel, QLabel]] = []
        for index in range(5):
            label = QLabel("")
            label.setObjectName("fieldLabel")
            value = QLabel("")
            value.setWordWrap(True)
            grid.addWidget(label, index, 0, Qt.AlignTop)
            grid.addWidget(value, index, 1)
            self._health_rows.append((label, value))
        card.add_layout(grid)
        self._health_note = QLabel("")
        self._health_note.setWordWrap(True)
        self._health_note.setObjectName("statusBad")
        self._health_note.setVisible(False)
        card.add(self._health_note)
        return card

    def set_session_health(self, rows: list, blocked_reason: str = "") -> None:
        for (label, value), row in zip(self._health_rows, list(rows) + [None] * 5):
            if row is None:
                label.setText(""); value.setText(""); continue
            name, text, health = row
            if label.text() != name:
                label.setText(name)
            if value.text() != text:
                value.setText(text)
            style = {"ok": "statusOk", "degraded": "statusWarn"}.get(health, "statusBad")
            if value.objectName() != style:
                value.setObjectName(style)
                value.style().unpolish(value); value.style().polish(value)
        self._health_note.setText(blocked_reason)
        self._health_note.setVisible(bool(blocked_reason))

    def _build_storage_card(self) -> QWidget:
        card = _Card("Storage")
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        rows = [
            ("mongo", "MongoDB status"),
            ("json", "JSON backup status"),
            ("chat_id", "Chat ID"),
        ]
        self._storage_values: dict[str, QLabel] = {}
        for index, (key, caption) in enumerate(rows):
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(label, index, 0, Qt.AlignTop)
            grid.addWidget(value, index, 1)
            self._storage_values[key] = value
        # The chat ID is the MongoDB key and the id in every log line and JSON
        # record, so it is shown in a monospaced font and is selectable — its
        # whole purpose is being copied into a query.
        self._storage_values["chat_id"].setObjectName("fieldMono")
        card.add_layout(grid)
        return card

    def _build_actions(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        self._export_button = QPushButton("Export JSON")
        self._export_button.setToolTip(
            "Write this chat's configuration, stored messages and webhook history\n"
            "to a standalone JSON file."
        )
        self._export_button.clicked.connect(
            lambda: self._chat and self.export_requested.emit(self._chat.chat_id)
        )
        row.addWidget(self._export_button)

        self._reset_button = QPushButton("Reset automation")
        self._reset_button.setToolTip(
            "Turn automation off, clear the webhook status and counters, and re-baseline\n"
            "the message backlog. The webhook URL and stored messages are kept."
        )
        self._reset_button.clicked.connect(
            lambda: self._chat and self.reset_requested.emit(self._chat.chat_id)
        )
        row.addWidget(self._reset_button)

        row.addStretch(1)

        self._delete_button = QPushButton("Delete chat")
        self._delete_button.setObjectName("danger")
        self._delete_button.setToolTip(
            "Delete this chat's configuration, stored messages and webhook history.\n"
            "Nothing in WhatsApp is touched — the chat will be rediscovered on the\n"
            "next poll with a clean configuration."
        )
        self._delete_button.clicked.connect(
            lambda: self._chat and self.delete_requested.emit(self._chat.chat_id)
        )
        row.addWidget(self._delete_button)
        return row

    # -- data --------------------------------------------------------------

    def set_storage_status(self, mongo_status: str, mongo_ok: bool,
                           json_status: str, json_ok: bool) -> None:
        self._apply_status(self._storage_values["mongo"], mongo_status or "—", mongo_ok)
        self._apply_status(self._storage_values["json"], json_status or "—", json_ok)

    @staticmethod
    def _apply_status(label: QLabel, text: str, ok: bool) -> None:
        if label.text() != text:
            label.setText(text)
        name = "statusOk" if ok else "statusBad"
        if label.objectName() != name:
            label.setObjectName(name)
            label.style().unpolish(label)
            label.style().polish(label)

    def set_chat(self, chat: Optional[ChatConfig]) -> None:
        previous_id = self._chat.chat_id if self._chat is not None else ""
        switching = (chat.chat_id if chat is not None else "") != previous_id
        self._chat = chat

        has_chat = chat is not None
        self._scroll.setVisible(has_chat)
        self._empty.setVisible(not has_chat)
        self._scan_button.setVisible(has_chat)

        if chat is None:
            self._title.setText("No chat selected")
            self._subtitle.setText("")
            self._avatar.setText("—")
            self._avatar.setStyleSheet(
                f"background: {theme.BORDER}; color: {theme.TEXT_MUTED}; border-radius: 20px;"
            )
            self._loaded_webhook = ""
            return

        if switching:
            self._set_feedback("", "")
            self._external_id_feedback.setText("")
            self._external_id_feedback.setVisible(False)

        self._title.setText(chat.chat_name or "(unnamed)")
        self._subtitle.setText(
            ("Group · " if chat.is_group else "")
            + ("automation ON" if chat.automation_enabled else "automation OFF")
            + (" · webhook configured" if (chat.webhook_url or "").strip() else " · no webhook")
        )
        if switching:
            self._avatar.setText(theme.initials(chat.chat_name))
            self._avatar.setStyleSheet(
                f"background: {theme.avatar_color(chat.chat_name)}; color: #ffffff;"
                f"border-radius: 20px; font-weight: 700;"
            )

        self._enabled.blockSignals(True)
        self._enabled.setChecked(chat.automation_enabled)
        self._enabled.blockSignals(False)

        self._sync_webhook_field(chat, switching)
        self._sync_external_id_field(chat, switching)

        self._set_value("last_poll", _format_time(chat.last_poll_utc))
        incoming = _truncate(chat.last_incoming_text)
        if chat.last_incoming_sender and incoming != "—":
            incoming = f"{chat.last_incoming_sender}: {incoming}"
        if chat.last_incoming_utc:
            incoming += f"\n{_format_time(chat.last_incoming_utc)}"
        self._set_value("last_incoming", incoming)

        outgoing = _truncate(chat.last_outgoing_text)
        if chat.last_outgoing_utc:
            outgoing += f"\n{_format_time(chat.last_outgoing_utc)}"
        self._set_value("last_outgoing", outgoing)

        self._set_value("webhook_status", chat.last_webhook_status or "—")
        relay = chat.last_relay_status or "—"
        if chat.last_relay_utc and relay != "—":
            relay += f"\n{_format_time(chat.last_relay_utc)}"
        self._set_value("relay_status", relay)
        response = _truncate(chat.last_webhook_response)
        if chat.last_webhook_utc and response != "—":
            response += f"\n{_format_time(chat.last_webhook_utc)}"
        self._set_value("last_webhook", response)

        self._set_value("retries", str(chat.webhook_retry_count))
        stored = f"{chat.messages_stored}"
        if not chat.seeded:
            stored += "  (backlog not baselined yet)"
        self._set_value("stored", stored)
        self._set_value("error", chat.last_error or "—")

        error_style = "statusBad" if chat.last_error else "fieldValue"
        error_label = self._values["error"]
        if error_label.objectName() != error_style:
            error_label.setObjectName(error_style)
            error_label.style().unpolish(error_label)
            error_label.style().polish(error_label)

        if self._storage_values["chat_id"].text() != chat.chat_id:
            self._storage_values["chat_id"].setText(chat.chat_id)

    def _sync_webhook_field(self, chat: ChatConfig, switching: bool) -> None:
        """Keep the URL field showing this chat's webhook.

        Clicking a different chat always reloads it. Re-rendering the same chat
        reloads it only when the field is untouched, so a URL being typed
        survives the once-a-second refresh — and an edit saved from elsewhere
        (or a value the engine changed) still lands."""
        stored = chat.webhook_url or ""
        if switching or self._webhook.text() == self._loaded_webhook:
            if self._webhook.text() != stored:
                self._webhook.setText(stored)
            self._loaded_webhook = stored

    def _sync_external_id_field(self, chat: ChatConfig, switching: bool) -> None:
        """Same rule as the webhook field: always reload on a different chat,
        preserve unsaved typing while re-rendering the same one."""
        stored = chat.external_id or ""
        if switching or self._external_id.text() == self._loaded_external_id:
            if self._external_id.text() != stored:
                self._external_id.setText(stored)
            self._loaded_external_id = stored

    def _on_external_id_edited(self, _text: str) -> None:
        if self._external_id_feedback.text():
            self._external_id_feedback.setText("")
            self._external_id_feedback.setVisible(False)

    def _save_external_id(self) -> None:
        if self._chat is None:
            return
        value = self._external_id.text().strip()
        self._loaded_external_id = value
        self._external_id.setText(value)
        self.external_id_saved.emit(self._chat.chat_id, value)
        self._external_id_feedback.setText("Saved." if value else "Contact ID cleared.")
        self._external_id_feedback.setVisible(True)
        self._external_id_feedback.setObjectName("statusOk")
        self._external_id_feedback.style().unpolish(self._external_id_feedback)
        self._external_id_feedback.style().polish(self._external_id_feedback)

    def _set_value(self, key: str, text: str) -> None:
        """Write a status value only when it actually changed.

        The panel re-renders once a second so relative times stay honest, and
        `setText` on a QLabel clears any selection inside it. Skipping the
        no-op write means text someone is trying to copy — a webhook response,
        an error — survives the next tick."""
        label = self._values[key]
        if label.text() != text:
            label.setText(text)

    def _set_feedback(self, text: str, kind: str) -> None:
        self._feedback.setText(text)
        # Hidden when empty: a blank label still occupies a line, leaving a gap
        # under the field that reads as a layout mistake.
        self._feedback.setVisible(bool(text))
        name = {"ok": "statusOk", "bad": "statusBad"}.get(kind, "fieldValueMuted")
        self._feedback.setObjectName(name)
        self._feedback.style().unpolish(self._feedback)
        self._feedback.style().polish(self._feedback)

    # -- events ------------------------------------------------------------

    def _on_webhook_edited(self, _text: str) -> None:
        # Typing clears stale feedback: a "Saved" from thirty seconds ago
        # sitting under a field being edited is actively misleading.
        if self._feedback.text():
            self._set_feedback("", "")

    def _on_enabled_toggled(self, checked: bool) -> None:
        if self._chat is not None:
            self.automation_toggled.emit(self._chat.chat_id, checked)

    def _save_webhook(self) -> None:
        if self._chat is None:
            return
        url = self._webhook.text().strip()
        valid, problem = validate_webhook_url(url)
        if not valid:
            self._set_feedback(problem, "bad")
            return
        self._loaded_webhook = url
        self._webhook.setText(url)
        self.webhook_saved.emit(self._chat.chat_id, url)
        self._set_feedback("Saved." if url else "Webhook cleared.", "ok")
        self._feedback_timer.start(4000)

    def report_save_failed(self, problem: str) -> None:
        """Called by the window when the engine could not store the change, so
        a failure is never mistaken for the success message above."""
        self._set_feedback(f"Not saved — {problem}", "bad")
        self._feedback_timer.stop()

    def restyle(self) -> None:
        """Re-apply the inline styles that don't come from the stylesheet, after
        a light/dark change."""
        chat = self._chat
        if chat is None:
            self._avatar.setStyleSheet(
                f"background: {theme.BORDER}; color: {theme.TEXT_MUTED}; border-radius: 20px;"
            )
        else:
            self._avatar.setStyleSheet(
                f"background: {theme.avatar_color(chat.chat_name)}; color: #ffffff;"
                f"border-radius: 20px; font-weight: 700;"
            )
