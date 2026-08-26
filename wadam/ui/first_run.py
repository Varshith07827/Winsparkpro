"""The first-run setup window.

Four questions, one button. It appears when there is no usable configuration
and never again — which is the entire configuration experience the product has.

Deliberately *not* a settings screen. There is no way back into it from the
running application.

It asks for one more thing than it used to, and the swap is worth naming: the
old second question was a webhook URL *template* with a `{phone_number}`
placeholder, because every chat got its own derived URL and the application
both POSTed to it and polled it for outbound messages. There is one webhook
now, registered against the session inside OpenWA, so what has to be asked
instead is where OpenWA is and which session to use.

Both connections are tested before the window closes. Writing an unusable
connection string to `.env` and failing on the next screen is the version of
this that wastes someone's afternoon.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
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
_OPENWA_PLACEHOLDER = "http://localhost:2785"


def env_text(mongodb_uri: str, openwa_url: str, openwa_api_key: str,
             openwa_session_id: str, webhook_secret: str,
             database_name: str = constants.DATABASE_NAME) -> str:
    """The `.env` this writes.

    Live keys for everything required, and some documented-but-commented ones.
    A setting nobody can discover may as well not exist — `API_PORT` is the
    difference between "curl reaches this app" and "curl reaches nothing", and
    a reader who has never seen the source has no way to learn the name. Those
    are written commented, so they change nothing until someone means them to.
    """
    return (
        "# WhatsApp Automation Manager — configuration\n"
        "# Written by the first-run setup.\n"
        "\n"
        "# Where chats and messages are stored.\n"
        f"MONGODB_URI={mongodb_uri}\n"
        "\n"
        "# Which database on that server. One cluster often holds more than one\n"
        "# deployment — a staging run and real messages should not share a\n"
        "# database. MongoDB's own (admin, local, config) are refused.\n"
        f"DATABASE_NAME={database_name}\n"
        "\n"
        "# --- OpenWA, the transport -------------------------------------------\n"
        "# The gateway this application sends through and receives from.\n"
        f"OPENWA_URL={openwa_url}\n"
        f"OPENWA_API_KEY={openwa_api_key}\n"
        "# The session's UUID, not its name.\n"
        f"OPENWA_SESSION_ID={openwa_session_id}\n"
        "\n"
        "# --- the webhook this application listens on ---------------------------\n"
        "# Register this address in OpenWA for the message.received event:\n"
        "#   http://host.docker.internal:8765/hook\n"
        "# `host.docker.internal`, not `localhost` — OpenWA resolves the URL from\n"
        "# inside its container, where `localhost` is the container itself. That\n"
        "# is also why the bind address below is 0.0.0.0 rather than 127.0.0.1.\n"
        "# OpenWA's SSRF guard blocks private addresses by default; allow this one\n"
        "# with SSRF_ALLOWED_HOSTS=host.docker.internal in OpenWA's own .env.\n"
        "WEBHOOK_HOST=0.0.0.0\n"
        "WEBHOOK_PORT=8765\n"
        "# Use this same value as the webhook's `secret` in OpenWA, so a delivery\n"
        "# can be proven to have come from it.\n"
        f"WEBHOOK_SECRET={webhook_secret}\n"
        "\n"
        "# Per-chat quiet period, in seconds. Bounds an automation answering an\n"
        "# automation. 0 disables it.\n"
        "# COOLDOWN_SECONDS=60\n"
        "\n"
        "# Answer group chats too. A bot in a group is louder than a bot in a DM.\n"
        "# ANSWER_GROUPS=false\n"
        "\n"
        "# --- the inbound send API ----------------------------------------------\n"
        "# Lets something POST {\"id\", \"message\"} in to send a message. Off until\n"
        "# a port is set. Loopback-bound by default, where a token is optional;\n"
        "# bind it anywhere else and API_TOKEN becomes mandatory.\n"
        "# API_PORT=8766\n"
        "# API_HOST=127.0.0.1\n"
        "# API_TOKEN=\n"
        "\n"
        "# DEBUG / INFO / WARNING / ERROR\n"
        "# LOG_LEVEL=INFO\n"
    )


def check_mongodb(uri: str, timeout_ms: int = 4000) -> str:
    """"" when the server answers, otherwise the problem in plain words."""
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:  # pragma: no cover - pymongo is a hard dependency
        return "pymongo is not installed."

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)
        client.admin.command("ping")
        client.close()
    except PyMongoError as ex:
        return f"MongoDB did not answer: {ex}"
    except Exception as ex:  # noqa: BLE001
        return f"MongoDB could not be reached: {ex}"
    return ""


def check_openwa(url: str, api_key: str, session_id: str, timeout: float = 6.0) -> str:
    """"" when OpenWA answers and the session exists, otherwise the problem.

    Checked here rather than at first message, because "the session id is
    wrong" is invisible at runtime: deliveries arrive, replies are attempted,
    and every one fails with a 404 nobody is watching for.
    """
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/sessions",
        headers={"x-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            rows = json.loads(response.read().decode("utf-8", "replace") or "[]")
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403):
            return "OpenWA refused the API key."
        return f"OpenWA answered {ex.code}."
    except urllib.error.URLError as ex:
        return f"OpenWA could not be reached at {url}: {ex.reason}"
    except (ValueError, json.JSONDecodeError):
        return f"{url} answered, but not with a session list. Is that an OpenWA instance?"

    rows = rows if isinstance(rows, list) else rows.get("data", [])
    ids = [str(r.get("id")) for r in rows]
    if not ids:
        return "OpenWA has no sessions. Create and link one first."
    if session_id not in ids:
        listed = ", ".join(
            "{} ({})".format(r.get("name") or "unnamed", r.get("id")) for r in rows[:3]
        )
        return f"No session with that id. OpenWA currently has: {listed}"
    return ""


def validate(mongodb_uri: str, openwa_url: str, api_key: str, session_id: str) -> str:
    """"" when the set is usable, otherwise the first problem in plain words."""
    uri = (mongodb_uri or "").strip()
    if not uri:
        return "Enter your MongoDB connection string."
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        return "A MongoDB connection string starts with mongodb:// or mongodb+srv://."

    url = (openwa_url or "").strip()
    if not url:
        return "Enter the OpenWA address."
    if not url.startswith(("http://", "https://")):
        return "The OpenWA address should start with http:// or https://."

    if not (api_key or "").strip():
        return "Enter the OpenWA API key — it is in OpenWA's data/.api-key file."
    if not (session_id or "").strip():
        return "Enter the OpenWA session id. It is the session's UUID, not its name."
    return ""


class FirstRunDialog(QDialog):
    """Asks for the things the application cannot work out for itself."""

    def __init__(self, env_path: Path, mongodb_uri: str = "",
                 openwa_url: str = _OPENWA_PLACEHOLDER) -> None:
        super().__init__()
        self._env_path = env_path
        self.mongodb_uri = mongodb_uri

        self.setWindowTitle(f"{constants.APP_NAME} — setup")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(10)

        heading = QLabel("Two connections, then it runs itself.")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(heading)

        subtitle = QLabel(
            "Where to store messages, and which OpenWA session to send them through.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        layout.addWidget(self._label("MongoDB connection string"))
        self._mongo = QLineEdit(mongodb_uri)
        self._mongo.setPlaceholderText(_MONGO_PLACEHOLDER)
        self._mongo.textChanged.connect(self._clear_feedback)
        layout.addWidget(self._mongo)

        layout.addWidget(self._label("OpenWA address"))
        self._url = QLineEdit(openwa_url)
        self._url.setPlaceholderText(_OPENWA_PLACEHOLDER)
        self._url.textChanged.connect(self._clear_feedback)
        layout.addWidget(self._url)

        layout.addWidget(self._label("OpenWA API key"))
        self._key = QLineEdit()
        self._key.setPlaceholderText("owa_k1_…  (in OpenWA's data/.api-key)")
        self._key.textChanged.connect(self._clear_feedback)
        layout.addWidget(self._key)

        layout.addWidget(self._label("OpenWA session id"))
        self._session = QLineEdit()
        self._session.setPlaceholderText("the session's UUID, not its name")
        self._session.textChanged.connect(self._clear_feedback)
        layout.addWidget(self._session)

        hint = QLabel(
            "After this, register a webhook in OpenWA for message.received pointing at "
            "http://host.docker.internal:8765/hook — the .env written here explains why "
            "that host and not localhost."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(hint)

        self._feedback = QLabel("")
        self._feedback.setWordWrap(True)
        self._feedback.setVisible(False)
        layout.addWidget(self._feedback)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda: self.done(CANCEL))
        buttons.addWidget(cancel)
        self._start = QPushButton("Start")
        self._start.setDefault(True)
        self._start.clicked.connect(self._on_start)
        buttons.addWidget(self._start)
        layout.addLayout(buttons)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: 600;")
        return label

    def _clear_feedback(self, _text: str = "") -> None:
        self._feedback.setVisible(False)

    def _set_feedback(self, text: str) -> None:
        self._feedback.setText(text)
        self._feedback.setStyleSheet(f"color: {theme.DANGER};")
        self._feedback.setVisible(True)

    def _on_start(self) -> None:
        uri = self._mongo.text().strip()
        url = self._url.text().strip().rstrip("/")
        key = self._key.text().strip()
        session = self._session.text().strip()

        problem = validate(uri, url, key, session)
        if problem:
            self._set_feedback(problem)
            return

        self._start.setEnabled(False)
        self._start.setText("Checking…")
        try:
            problem = check_mongodb(uri) or check_openwa(url, key, session)
        finally:
            self._start.setEnabled(True)
            self._start.setText("Start")
        if problem:
            self._set_feedback(problem)
            return

        self.mongodb_uri = uri
        try:
            write_env(self._env_path, uri, url, key, session)
        except OSError as ex:
            self._set_feedback(f"Could not write {self._env_path}: {ex}")
            return
        self.done(START)


def write_env(env_path: Path, mongodb_uri: str, openwa_url: str,
              openwa_api_key: str, openwa_session_id: str) -> Path:
    """Write `.env`, generating the webhook secret so nobody has to think of one."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    env_path.write_text(
        env_text(mongodb_uri, openwa_url, openwa_api_key, openwa_session_id, secret),
        encoding="utf-8",
    )
    return env_path


def needs_setup(env_path: Path) -> bool:
    """True when there is no configuration worth trying to load."""
    if not env_path.is_file():
        return True
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return True
    keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    return not {"MONGODB_URI", "OPENWA_API_KEY", "OPENWA_SESSION_ID"} <= keys
