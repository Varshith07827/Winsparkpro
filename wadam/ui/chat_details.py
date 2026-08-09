"""The right-hand panel: a chat's name and the address its messages go to.

That is the whole panel, and the restraint is the point. It replaced a
six-card configuration screen carrying activity history, session health,
operations counters, storage status and a row of destructive buttons — none of
which a user of this tool has to understand, and all of which are still
available in the logs.

The webhook URL is **shown, not edited**. It is built from the global template
and the chat's phone number, so editing it here would create a per-chat value
that silently stops tracking the template. The number itself IS editable, and
is the only editable thing in the product — see the class docstring for why.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from wadam.domain.models import ChatConfig, phone_digits
from wadam.domain.webhook_url import validate_webhook_url


class ChatDetailsPanel(QWidget):
    """Name, number, webhook. Nothing else.

    The number is the one thing here that can be typed, and only because
    WhatsApp will not give it up: it shows a saved contact by name and exposes
    the number nowhere readable (measured — zero phone-shaped strings across
    every accessible name in the window). Without it there is no webhook URL,
    so the choice is one small field or a chat that can never forward anything.

    The reference implementation reached the same conclusion from the other
    direction: winSpark asks for "contact name or phone number" and uses
    WhatsApp's own search to bind a typed number to a saved contact. Same
    trade, same reason."""

    #: (chat_id, phone_number) — saved immediately, no button.
    phone_saved = Signal(str, str)
    #: (chat_id, url) — an empty url means "follow the global template".
    webhook_saved = Signal(str, str)

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

        body.addWidget(self._label("Phone number"))
        self._phone = QLineEdit()
        self._phone.setPlaceholderText("15551234567")
        self._phone.setMaximumWidth(320)
        self._phone.editingFinished.connect(self._save_phone)
        body.addWidget(self._phone)

        self._phone_hint = QLabel("")
        self._phone_hint.setObjectName("fieldHint")
        self._phone_hint.setWordWrap(True)
        body.addWidget(self._phone_hint)
        body.addSpacing(16)

        body.addWidget(self._label("Webhook"))
        # Filled in for you, but yours to change. Editing stores an OVERRIDE;
        # clearing the box hands the chat back to the global template.
        self._webhook = QLineEdit()
        self._webhook.editingFinished.connect(self._save_webhook)
        body.addWidget(self._webhook)

        self._webhook_hint = QLabel("")
        self._webhook_hint.setObjectName("fieldHint")
        self._webhook_hint.setWordWrap(True)
        body.addWidget(self._webhook_hint)

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
        switching = chat is None or self._chat is None or chat.chat_id != self._chat.chat_id
        self._chat = chat
        self._body.setVisible(chat is not None)
        self._empty.setVisible(chat is None)
        if chat is None:
            self._title.setText("")
            return

        self._title.setText(chat.chat_name)
        # Reload the field ONLY when the chat actually changed. The panel is
        # re-rendered once a second, and overwriting the box on every pass made
        # it impossible to type a number at all — the same mistake, in the same
        # place, as the webhook field that used to show the previous chat's URL.
        # Focus is not a safe proxy here: clicking away mid-edit would still eat
        # the value.
        if switching:
            self._phone.setText(chat.phone_number or "")

        url = (chat.webhook_url or "").strip()
        if switching:
            self._webhook.setText(url)
        self._webhook_hint.setText(
            "Custom address for this chat. Clear the box to go back to the default."
            if chat.webhook_override else ""
        )
        self._webhook_hint.setVisible(bool(self._webhook_hint.text()))
        # One explanation, under the field that fixes it. Saying the same thing
        # twice on one screen reads as two different problems.
        self._phone_hint.setText(
            "" if chat.phone_number else
            "WhatsApp does not show the number for a saved contact, so this "
            "chat has no webhook yet. Type the number once — digits only, "
            "including the country code."
        )
        self._phone_hint.setVisible(bool(self._phone_hint.text()))

    def _save_phone(self) -> None:
        """Persist the typed number. Digits only — a number pasted as
        "+91 94231 55555" is the same number as "919423155555" and the webhook
        URL must not contain spaces either way."""
        if self._chat is None:
            return
        digits = phone_digits(self._phone.text())
        if digits == (self._chat.phone_number or ""):
            return
        self._phone.setText(digits)
        self.phone_saved.emit(self._chat.chat_id, digits)

    def _save_webhook(self) -> None:
        """Persist an edited webhook, or reject it without losing what was typed.

        An invalid URL is left in the box with the reason underneath rather than
        being silently reverted — retyping a long URL because the application
        quietly threw it away is a miserable way to spend a minute."""
        if self._chat is None:
            return
        text = self._webhook.text().strip()
        if text == (self._chat.webhook_url or "").strip():
            return
        ok, problem = validate_webhook_url(text)
        if not ok:
            self._webhook_hint.setText(problem)
            self._webhook_hint.setVisible(True)
            return
        self.webhook_saved.emit(self._chat.chat_id, text)

    def current_chat_id(self) -> str:
        return self._chat.chat_id if self._chat else ""
