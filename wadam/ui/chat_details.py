"""The right-hand panel: a chat's name and the address its messages go to.

That is the whole panel, and the restraint is the point. It replaced a
six-card configuration screen carrying activity history, session health,
operations counters, storage status and a row of destructive buttons — none of
which a user of this tool has to understand, and all of which are still
available in the logs.

The webhook URL is **shown, not edited**. It is built from the global template
and the chat's phone number, so editing it here would create a per-chat value
that silently stops tracking the template. A chat whose number could not be
resolved says so in words rather than showing a broken URL.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from wadam.domain.models import ChatConfig
from wadam.domain.webhook_url import describe_missing


class ChatDetailsPanel(QWidget):
    """Name, number, webhook. Nothing else."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("configPanel")
        self._chat: Optional[ChatConfig] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setObjectName("panelHeader")
        header.setFixedHeight(60)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(24, 0, 20, 0)
        self._title = QLabel("")
        self._title.setObjectName("panelTitle")
        header_row.addWidget(self._title, 1)
        outer.addWidget(header)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(24, 22, 24, 24)
        body.setSpacing(6)

        body.addWidget(self._label("Webhook"))
        self._webhook = QLabel("")
        self._webhook.setObjectName("fieldValue")
        self._webhook.setWordWrap(True)
        # Selectable so the URL can be copied into a browser or a curl command,
        # which is the only thing anyone wants to do with it.
        self._webhook.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.addWidget(self._webhook)

        self._note = QLabel("")
        self._note.setObjectName("fieldHint")
        self._note.setWordWrap(True)
        body.addWidget(self._note)

        body.addStretch(1)
        outer.addWidget(self._body, 1)

        self._empty = QLabel(
            "Select a chat to see where its messages are sent.\n\n"
            "Tick a chat in the list to turn its automation on."
        )
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setObjectName("fieldValueMuted")
        outer.addWidget(self._empty, 1)

        self.set_chat(None)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def set_chat(self, chat: Optional[ChatConfig]) -> None:
        self._chat = chat
        self._body.setVisible(chat is not None)
        self._empty.setVisible(chat is None)
        if chat is None:
            self._title.setText("")
            return

        self._title.setText(chat.chat_name)
        url = (chat.webhook_url or "").strip()
        self._webhook.setText(url or "—")
        self._note.setText(describe_missing(chat.phone_number) if not url else "")
        self._note.setVisible(bool(self._note.text()))

    def current_chat_id(self) -> str:
        return self._chat.chat_id if self._chat else ""
