"""The startup error screen.

Startup order is `load → validate → launch`, and anything that fails validation
stops the launch and lands here. The screen exists because the alternative —
a traceback in a console the user may not even have open, or a window that
appears and then quietly does nothing — is how a misconfigured `.env` turns
into "the app is broken".

It shows every problem at once, names the file they came from, and offers
Retry: the usual fix is to edit `.env` in another window, and making someone
restart the application to find out whether they got it right is a poor way to
spend their afternoon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from wadam import constants
from wadam.ui import theme

RETRY = 1
QUIT = 0


class StartupErrorDialog(QDialog):
    def __init__(self, title: str, problems: Sequence[str], env_path: Path | None = None,
                 hint: str = "") -> None:
        super().__init__()
        self.setWindowTitle(f"{constants.APP_SHORT_NAME} — startup")
        self.setMinimumSize(660, 420)
        self.setStyleSheet(theme.stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        heading = QLabel(title)
        heading.setObjectName("configTitle")
        layout.addWidget(heading)

        subtitle = QLabel(
            f"Configuration is read from {env_path}" if env_path
            else "The application could not start."
        )
        subtitle.setObjectName("configSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText("\n\n".join(f"• {p}" for p in problems))
        layout.addWidget(detail, 1)

        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("fieldValueMuted")
            hint_label.setWordWrap(True)
            hint_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(hint_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        quit_button = QPushButton("Quit")
        quit_button.clicked.connect(lambda: self.done(QUIT))
        buttons.addWidget(quit_button)
        retry_button = QPushButton("Retry")
        retry_button.setObjectName("primary")
        retry_button.setDefault(True)
        retry_button.clicked.connect(lambda: self.done(RETRY))
        buttons.addWidget(retry_button)
        layout.addLayout(buttons)


class StartupWarningDialog(QDialog):
    """Non-fatal problems worth saying out loud before the window appears —
    a setting that does nothing, a database that came up empty and was restored
    from the backup. Startup continues either way."""

    def __init__(self, warnings: Sequence[str]) -> None:
        super().__init__()
        self.setWindowTitle(f"{constants.APP_SHORT_NAME} — starting")
        self.setMinimumWidth(560)
        self.setStyleSheet(theme.stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        heading = QLabel("Starting with warnings")
        heading.setObjectName("configTitle")
        layout.addWidget(heading)

        for warning in warnings:
            label = QLabel(f"• {warning}")
            label.setWordWrap(True)
            layout.addWidget(label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok = QPushButton("Continue")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
