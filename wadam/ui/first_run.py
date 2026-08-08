"""The first-run setup window.

Two questions, one button. It appears when there is no usable configuration and
never again — which is the entire configuration experience the product has.

Deliberately *not* a settings screen. There is no way back into it from the
running application, because everything else the program needs is either fixed
(the database name, the three-second poll) or derived (each chat's webhook URL,
built from the template and the chat's number). Adding a second field here is
almost always the wrong answer to whatever question prompted it.

The MongoDB connection is tested before the window closes. Writing an unusable
connection string to `.env` and failing on the next screen is the version of
this that wastes someone's afternoon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from wadam import constants
from wadam.ui import theme

START = 1
CANCEL = 0

_MONGO_PLACEHOLDER = "mongodb://localhost:27017"


def env_text(mongodb_uri: str, webhook_template: str) -> str:
    """The `.env` this writes. Two keys — everything else is fixed or derived."""
    return (
        "# WhatsApp Automation — configuration\n"
        "# Written by the first-run setup. Two settings; everything else is\n"
        "# fixed (database name, 3s polling) or derived (per-chat webhook URLs).\n"
        f"MONGODB_URI={mongodb_uri}\n"
        f"WEBHOOK_URL={webhook_template}\n"
    )


def check_mongodb(uri: str, timeout_ms: int = 4000) -> str:
    """"" if the server answered, otherwise a sentence a non-technical user can
    act on. Never raises and never leaks a driver traceback into the UI."""
    try:
        from pymongo import MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)
        try:
            client.admin.command("ping")
        finally:
            client.close()
        return ""
    except Exception as ex:  # noqa: BLE001 - every failure is the same answer here
        detail = str(ex).split(",")[0][:160]
        return f"Could not connect to MongoDB. {detail}"


def validate(mongodb_uri: str, webhook_template: str) -> str:
    """"" when the pair is usable, otherwise the first problem in plain words."""
    uri = (mongodb_uri or "").strip()
    template = (webhook_template or "").strip()
    if not uri:
        return "Enter your MongoDB connection string."
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        return "A MongoDB connection string starts with mongodb:// or mongodb+srv://."
    if not template:
        return "Enter the webhook URL."
    if not template.startswith(("http://", "https://")):
        return "The webhook URL should start with http:// or https://."
    if constants.PHONE_PLACEHOLDER not in template:
        return (f"Keep {constants.PHONE_PLACEHOLDER} in the webhook URL — it is "
                f"replaced with each chat's phone number.")
    return ""


class FirstRunDialog(QDialog):
    """Asks for the only two things the application cannot work out itself."""

    def __init__(self, env_path: Path,
                 webhook_template: str = constants.DEFAULT_WEBHOOK_TEMPLATE,
                 mongodb_uri: str = "") -> None:
        super().__init__()
        self._env_path = env_path
        self.mongodb_uri = mongodb_uri
        self.webhook_template = webhook_template

        self.setWindowTitle(f"{constants.APP_SHORT_NAME} — setup")
        self.setMinimumWidth(560)
        self.setStyleSheet(theme.stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(6)

        heading = QLabel("Set up WhatsApp Automation")
        heading.setObjectName("configTitle")
        layout.addWidget(heading)

        subtitle = QLabel("Two details, then it runs on its own.")
        subtitle.setObjectName("configSubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(14)

        layout.addWidget(self._label("MongoDB connection string"))
        self._mongo = QLineEdit(mongodb_uri)
        self._mongo.setPlaceholderText(_MONGO_PLACEHOLDER)
        self._mongo.textChanged.connect(self._clear_feedback)
        layout.addWidget(self._mongo)
        layout.addSpacing(12)

        layout.addWidget(self._label("Webhook URL"))
        self._webhook = QLineEdit(webhook_template)
        self._webhook.textChanged.connect(self._clear_feedback)
        layout.addWidget(self._webhook)

        hint = QLabel(f"{constants.PHONE_PLACEHOLDER} is replaced with each "
                      f"chat's phone number.")
        hint.setObjectName("fieldHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(10)
        self._feedback = QLabel("")
        self._feedback.setObjectName("configFeedback")
        self._feedback.setWordWrap(True)
        layout.addWidget(self._feedback)

        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda: self.done(CANCEL))
        buttons.addWidget(cancel)
        self._start = QPushButton("Start")
        self._start.setObjectName("primaryButton")
        self._start.setDefault(True)
        self._start.clicked.connect(self._on_start)
        buttons.addWidget(self._start)
        layout.addLayout(buttons)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def _clear_feedback(self, _text: str = "") -> None:
        self._feedback.setText("")

    def _set_feedback(self, text: str) -> None:
        self._feedback.setText(text)
        self._feedback.setProperty("kind", "error" if text else "")
        self._feedback.style().unpolish(self._feedback)
        self._feedback.style().polish(self._feedback)

    def _on_start(self) -> None:
        uri = self._mongo.text().strip()
        template = self._webhook.text().strip()

        problem = validate(uri, template)
        if problem:
            self._set_feedback(problem)
            return

        self._start.setEnabled(False)
        self._set_feedback("Checking the MongoDB connection…")
        self.repaint()
        problem = check_mongodb(uri)
        self._start.setEnabled(True)
        if problem:
            self._set_feedback(problem)
            return

        self.mongodb_uri = uri
        self.webhook_template = template
        try:
            write_env(self._env_path, uri, template)
        except OSError as ex:
            self._set_feedback(f"Could not save the configuration: {ex}")
            return
        self.done(START)


def write_env(env_path: Path, mongodb_uri: str, webhook_template: str) -> Path:
    """Write `.env` beside the application, creating the folder if needed."""
    env_path = Path(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(env_text(mongodb_uri, webhook_template), encoding="utf-8")
    return env_path


def needs_setup(env_path: Path) -> bool:
    """Is there a configuration worth trying to load?

    A missing file, an empty one, or one with no MongoDB URI all mean the same
    thing to the user: this has not been set up yet."""
    path = Path(env_path)
    if not path.exists():
        return True
    try:
        from wadam.config import parse_env_text

        values = parse_env_text(path.read_text(encoding="utf-8"))
    except OSError:
        return True
    return not (values.get("MONGODB_URI") or "").strip()
