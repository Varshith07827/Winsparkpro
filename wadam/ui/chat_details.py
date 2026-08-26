"""The right-hand panel: a chat, and what has actually been said in it.

Nothing here can be typed into, and that is a change worth stating. The panel
used to carry one editable field — the contact's phone number — because
WhatsApp Desktop would not give it up: it shows a saved contact by name and
exposes the number nowhere readable (measured — zero phone-shaped strings
across every accessible name in the window). Without a number there was no
webhook URL, so the choice was one small field or a chat that could never
forward anything.

OpenWA supplies the identity, so the field has nothing left to do. The webhook
went the same way: there is one webhook now, registered against the session in
OpenWA, not a URL derived per chat from a template.

What replaces them is the thing an operator actually wants when they click a
chat — the messages, in order, with which ones this application answered.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from wadam.domain.models import ChatConfig, MessageStatus, StoredMessage
from wadam.ui import theme

#: How many messages the transcript renders. The repository holds far more; a
#: panel that painted ten thousand bubbles would stall the window on a click.
TRANSCRIPT_LIMIT = 200


class ChatDetailsPanel(QWidget):
    """A chat's name, its identity, and its recent messages. Read-only."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatDetails")
        self._chat_id = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        self._name = QLabel("No chat selected")
        self._name.setObjectName("detailsTitle")
        self._name.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(self._name)

        self._identity = QLabel("")
        self._identity.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self._identity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._identity)

        self._state = QLabel("")
        self._state.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self._state)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {theme.BORDER};")
        layout.addWidget(line)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._transcript = QWidget()
        self._transcript_layout = QVBoxLayout(self._transcript)
        self._transcript_layout.setContentsMargins(0, 0, 0, 0)
        self._transcript_layout.setSpacing(6)
        self._transcript_layout.addStretch(1)
        self._scroll.setWidget(self._transcript)
        layout.addWidget(self._scroll, 1)

        self._empty = QLabel("Select a chat to see its messages.")
        self._empty.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self._empty)

        self.set_chat(None, [])

    # ── rendering ─────────────────────────────────────────────────────

    def set_chat(self, chat: Optional[ChatConfig],
                 messages: Optional[List[StoredMessage]] = None) -> None:
        self._clear_transcript()

        if chat is None:
            self._chat_id = ""
            self._name.setText("No chat selected")
            self._identity.setText("")
            self._state.setText("")
            self._empty.setText("Select a chat to see its messages.")
            self._empty.setVisible(True)
            self._scroll.setVisible(False)
            return

        self._chat_id = chat.chat_id
        self._name.setText(chat.chat_name or chat.chat_id)

        # The chat id is shown rather than hidden. It is what the send API
        # addresses, what a support question needs, and — for a LID — the only
        # identity there is, since no phone number can be derived from it.
        identity = chat.chat_id
        if chat.phone_number:
            identity = f"{chat.phone_number}  ·  {chat.chat_id}"
        self._identity.setText(identity)

        state = "automation on" if chat.automation_enabled else "automation off"
        if chat.is_group:
            state += "  ·  group"
        if chat.last_error:
            state += f"  ·  {chat.last_error}"
        self._state.setText(state)

        rows = list(messages or [])
        if not rows:
            self._empty.setText("Nothing stored for this chat yet.")
            self._empty.setVisible(True)
            self._scroll.setVisible(False)
            return

        self._empty.setVisible(False)
        self._scroll.setVisible(True)
        for message in rows[-TRANSCRIPT_LIMIT:]:
            self._transcript_layout.insertWidget(
                self._transcript_layout.count() - 1, _message_row(message))

    def current_chat_id(self) -> str:
        return self._chat_id

    def _clear_transcript(self) -> None:
        while self._transcript_layout.count() > 1:
            item = self._transcript_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


def _message_row(message: StoredMessage) -> QWidget:
    """One line of the transcript: direction, text, and when."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)

    outgoing = message.direction == "out"
    arrow = QLabel("→" if outgoing else "←")
    arrow.setFixedWidth(14)
    arrow.setStyleSheet(
        f"color: {theme.ACCENT if outgoing else theme.TEXT_MUTED}; font-weight: 600;")
    layout.addWidget(arrow)

    body = message.text or (f"[{message.media_kind}]" if message.media_kind else "")
    text = QLabel(body)
    text.setWordWrap(True)
    text.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if message.status == MessageStatus.FAILED:
        # A failed send is the one thing in the transcript worth colouring.
        text.setStyleSheet(f"color: {theme.DANGER};")
    layout.addWidget(text, 1)

    when = QLabel(message.detected_at.strftime("%H:%M") if message.detected_at else "")
    when.setStyleSheet(f"color: {theme.TEXT_MUTED};")
    layout.addWidget(when)

    return row
