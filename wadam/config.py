"""Configuration — loaded once from `.env`, validated, then frozen.

There is no settings window and no in-app configuration. Everything the
application needs comes from a `.env` file sitting next to the project folder
(or from a path in the `WADAM_ENV_FILE` environment variable). Startup order is
strictly: load → validate → launch, and a validation failure is shown to the
user as a startup error screen rather than a traceback.

Parsing is python-dotenv's, with the equivalent parser below as a fallback when
that package isn't installed. Validation is entirely ours: dotenv reads a file,
it doesn't know that `DATABASE_NAME=admin` will put application collections in
MongoDB's own system database.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from wadam.constants import (
    DATABASE_NAME,
    CHAT_NAME_PLACEHOLDER,
    DEFAULT_WEBHOOK_TEMPLATE,
    PHONE_PLACEHOLDER,
    WEBHOOK_TEMPLATE_EXAMPLE,
    POLL_INTERVAL_SECONDS,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Where the application keeps the files it OWNS: `.env`, the JSON mirror,
    the capability cache and the diagnostic log.

    Beside the executable when frozen, the project root when run from source.

    `PROJECT_ROOT` cannot be used for this. Inside a PyInstaller one-file build
    `__file__` lives in `sys._MEIPASS`, a temp directory **deleted when the
    process exits** — so the packaged application wrote its backup, its
    capability cache and its logs into a folder that disappeared on every quit.
    Measured: two orphaned `_MEI*/backup/` folders on this machine, each a
    complete mirror nobody could ever read back."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT

_TRUE_VALUES = {"1", "true", "yes", "on"}

#: MongoDB's own databases. Collections landing in one of these are hard to
#: find and worse to clean up, which is why DATABASE_NAME was fixed for a while.
_RESERVED_DATABASES = {"admin", "local", "config"}
#: Characters MongoDB forbids in a database name, plus the space.
_INVALID_DB_CHARS = re.compile(r'[/\. "$*<>:|?]')


def parse_env_text(text: str) -> dict[str, str]:
    """Parse `.env` content into a mapping. Blank lines and `#` comments are
    skipped; `export KEY=value` is accepted; surrounding single/double quotes
    are stripped; everything after the FIRST `=` is the value (so a webhook URL
    containing `=` in its query string survives intact)."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    """Read a `.env` file into a mapping.

    Uses python-dotenv when it is installed, and the parser above when it is
    not. Both understand the same file, so the fallback is not a degraded mode —
    it just means a missing optional dependency cannot stop the application from
    starting, which for a Windows desktop tool that people install by
    double-clicking is worth the twenty lines."""
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        return {k: (v or "") for k, v in dotenv_values(path, encoding="utf-8-sig").items()}
    except ImportError:
        return parse_env_text(path.read_text(encoding="utf-8-sig"))


@dataclass(frozen=True)
class Settings:
    """The effective configuration. Immutable for the life of the process."""

    mongodb_uri: str = ""
    #: Which database on the cluster. Configurable because one paid cluster
    #: often serves more than one deployment, and mixing a staging run in with
    #: real messages is not a mistake worth leaving available.
    database_name: str = DATABASE_NAME
    json_backup_folder: Path = field(default_factory=lambda: app_dir() / "backup")
    json_autosave_interval: float = 15.0

    #: The one webhook setting. `{phone_number}` is substituted per chat.
    webhook_template: str = DEFAULT_WEBHOOK_TEMPLATE
    default_webhook: str = ""
    webhook_api_key: str = ""
    webhook_timeout: float = 20.0
    webhook_max_retries: int = 3

    whatsapp_window_title: str = "WhatsApp"
    # Paste is faster and does not drop characters, but borrows the
    # clipboard for a moment. Off = per-character input, clipboard untouched.
    sender_use_clipboard: bool = True
    log_level: str = "INFO"

    # The relay: GET each automated chat's webhook and send what comes back.
    #
    # ON by default, because it is the outbound path this product ships with.
    # First-run setup writes exactly two keys — MONGODB_URI and WEBHOOK_URL —
    # and the send API needs an API_PORT that nothing writes, so with the relay
    # off a freshly installed build has NO way to send at all. That is what a
    # built EXE was doing: reading a two-line .env, listening on nothing, and
    # polling nothing.
    #
    # Set RELAY_ENABLED=false to turn it off.
    relay_enabled: bool = True
    relay_poll_interval: float = 3.0

    # Inbound send API. Off unless a port is set; a token is required whenever
    # it is on, and the bind address defaults to loopback.
    api_host: str = "127.0.0.1"
    api_port: int = 0
    api_token: str = ""
    api_send_timeout: float = 60.0

    env_path: Optional[Path] = None
    # Non-fatal problems worth telling the user about at startup (e.g. someone
    # set POLL_INTERVAL, which does nothing).
    warnings: tuple[str, ...] = ()

    @property
    def poll_interval_seconds(self) -> int:
        """Fixed. Exposed as a property so nothing downstream is tempted to
        look for a configurable one."""
        return POLL_INTERVAL_SECONDS

    def redacted(self) -> dict[str, object]:
        """The settings as they're mirrored into `settings.json` — credentials
        removed, because that file is meant to be opened and read by a human."""
        return {
            "mongodb_uri": _redact_uri(self.mongodb_uri),
            "database_name": self.database_name,
            "json_backup_folder": str(self.json_backup_folder),
            "json_autosave_interval": self.json_autosave_interval,
            "webhook_template": self.webhook_template,
            "default_webhook": self.default_webhook,
            "webhook_api_key": "***" if self.webhook_api_key else "",
            "webhook_timeout": self.webhook_timeout,
            "webhook_max_retries": self.webhook_max_retries,
            "whatsapp_window_title": self.whatsapp_window_title,
            "log_level": self.log_level,
            "sender_use_clipboard": self.sender_use_clipboard,
            "poll_interval_seconds": self.poll_interval_seconds,
            "relay_enabled": self.relay_enabled,
            "relay_poll_interval": self.relay_poll_interval,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "api_token": "***" if self.api_token else "",
            "api_send_timeout": self.api_send_timeout,
            "env_path": str(self.env_path) if self.env_path else None,
        }


class ConfigError(Exception):
    """Raised when `.env` is missing or invalid. Carries every problem found,
    not just the first — a startup screen that reveals one error at a time is
    the worst way to configure an app."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def _redact_uri(uri: str) -> str:
    """`mongodb+srv://user:pass@host/` → `mongodb+srv://user:***@host/`."""
    if "://" not in uri or "@" not in uri:
        return uri
    scheme, _, rest = uri.partition("://")
    credentials, _, host = rest.rpartition("@")
    if ":" in credentials:
        user, _, _pw = credentials.partition(":")
        credentials = f"{user}:***"
    return f"{scheme}://{credentials}@{host}"


def _as_float(values: dict[str, str], key: str, default: float, problems: list[str],
              minimum: float = 0.0) -> float:
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        problems.append(f"{key} must be a number (got {raw!r}).")
        return default
    if parsed < minimum:
        problems.append(f"{key} must be at least {minimum} (got {parsed}).")
        return default
    return parsed


def _as_int(values: dict[str, str], key: str, default: int, problems: list[str],
            minimum: int = 0) -> int:
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        problems.append(f"{key} must be a whole number (got {raw!r}).")
        return default
    if parsed < minimum:
        problems.append(f"{key} must be at least {minimum} (got {parsed}).")
        return default
    return parsed


def default_env_path() -> Path:
    """Where `.env` lives: beside the executable, or the project root when run
    from source. `WADAM_ENV_FILE` overrides both."""
    override = os.environ.get("WADAM_ENV_FILE")
    if override:
        return Path(override)
    return app_dir() / ".env"


def load_settings(env_path: Optional[Path] = None) -> Settings:
    """Load and validate configuration, or raise ConfigError listing everything
    that's wrong. Real process environment variables win over `.env`, so a
    deployment can override a single value without editing the file."""
    path = env_path or default_env_path()
    values = load_env_file(path)
    # Process environment overrides file values for the keys we know about.
    for key in (
        "MONGODB_URI", "WEBHOOK_URL", "DATABASE_NAME",
        "JSON_BACKUP_FOLDER", "JSON_AUTOSAVE_INTERVAL",
        "DEFAULT_WEBHOOK", "WEBHOOK_API_KEY", "WEBHOOK_TIMEOUT", "WEBHOOK_MAX_RETRIES",
        "WHATSAPP_WINDOW_TITLE", "LOG_LEVEL", "POLL_INTERVAL",
        "API_HOST", "API_PORT", "API_TOKEN", "API_SEND_TIMEOUT",
        "RELAY_ENABLED", "RELAY_POLL_INTERVAL", "SENDER_USE_CLIPBOARD",
    ):
        if os.environ.get(key):
            values[key] = os.environ[key]

    problems: list[str] = []
    warnings: list[str] = []

    if not values:
        problems.append(
            f"No configuration found. Expected a .env file at {path} — "
            f"copy .env.example to .env and fill it in."
        )

    mongodb_uri = (values.get("MONGODB_URI") or "").strip()
    if not mongodb_uri:
        problems.append("MONGODB_URI is required (e.g. mongodb://localhost:27017).")
    elif not mongodb_uri.startswith(("mongodb://", "mongodb+srv://")):
        problems.append("MONGODB_URI must start with mongodb:// or mongodb+srv://.")

    # Configurable, with a default. It was fixed for a while because a
    # configurable name is a way to get it wrong — but one cluster serving more
    # than one deployment needs separate databases, and on a paid cluster that
    # is the difference between a staging run and somebody's real messages.
    #
    # The validation that made it worth fixing stays: MongoDB's own databases
    # are refused outright rather than warned about, because collections landing
    # in `admin` or `local` are a mess to find and a worse one to clean up.
    database_name = (values.get("DATABASE_NAME") or "").strip() or DATABASE_NAME
    if database_name.lower() in _RESERVED_DATABASES:
        problems.append(
            f"DATABASE_NAME cannot be {database_name!r} — that is one of MongoDB's "
            f"own databases. Use something like {DATABASE_NAME!r}."
        )
    elif _INVALID_DB_CHARS.search(database_name):
        problems.append(
            "DATABASE_NAME cannot contain any of / \\ . \" $ * < > : | ? or a space."
        )
    elif len(database_name.encode("utf-8")) > 63:
        problems.append("DATABASE_NAME must be 63 bytes or fewer.")

    webhook_template = (values.get("WEBHOOK_URL")
                        or values.get("DEFAULT_WEBHOOK")
                        or DEFAULT_WEBHOOK_TEMPLATE).strip()
    if not webhook_template:
        # Deliberately a problem rather than a default. There is no sensible
        # guess for where someone else's messages should be sent, and a build
        # that silently pointed at a vendor's URL is what this replaces.
        problems.append(
            "WEBHOOK_URL is not set. It is where messages are sent, and where "
            "outbound messages are fetched from, so there is nothing to fall "
            f"back to. Example: {WEBHOOK_TEMPLATE_EXAMPLE}")
    elif not webhook_template.startswith(("http://", "https://")):
        problems.append("WEBHOOK_URL must be an http:// or https:// URL.")
    elif (PHONE_PLACEHOLDER not in webhook_template
          and CHAT_NAME_PLACEHOLDER not in webhook_template):
        warnings.append(
            f"WEBHOOK_URL has no {PHONE_PLACEHOLDER} or {CHAT_NAME_PLACEHOLDER} "
            f"placeholder, so every chat will use the same URL."
        )

    folder_raw = (values.get("JSON_BACKUP_FOLDER") or "backup").strip() or "backup"
    folder = Path(folder_raw)
    if not folder.is_absolute():
        folder = app_dir() / folder

    autosave = _as_float(values, "JSON_AUTOSAVE_INTERVAL", 15.0, problems, minimum=0.0)
    webhook_timeout = _as_float(values, "WEBHOOK_TIMEOUT", 20.0, problems, minimum=1.0)
    max_retries = _as_int(values, "WEBHOOK_MAX_RETRIES", 3, problems, minimum=0)

    default_webhook = (values.get("DEFAULT_WEBHOOK") or "").strip()
    if default_webhook and not default_webhook.startswith(("http://", "https://")):
        problems.append("DEFAULT_WEBHOOK must be an http:// or https:// URL.")

    log_level = (values.get("LOG_LEVEL") or "INFO").strip().upper() or "INFO"
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        problems.append(f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL (got {log_level!r}).")
        log_level = "INFO"

    # --- relay -------------------------------------------------------------
    # Absent means ON. The two-key .env that first-run setup writes never
    # mentions RELAY_ENABLED, and that file has to produce a working outbound
    # path — so silence cannot mean "no way to send".
    relay_raw = (values.get("RELAY_ENABLED") or "").strip().lower()
    relay_enabled = True if not relay_raw else relay_raw in _TRUE_VALUES
    relay_poll_interval = _as_float(values, "RELAY_POLL_INTERVAL", 3.0, problems, minimum=1.0)

    # --- inbound send API -------------------------------------------------
    api_port = _as_int(values, "API_PORT", 0, problems, minimum=0)
    if api_port > 65535:
        problems.append(f"API_PORT must be between 1 and 65535 (got {api_port}).")
        api_port = 0
    api_host = (values.get("API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    api_token = (values.get("API_TOKEN") or "").strip()
    api_send_timeout = _as_float(values, "API_SEND_TIMEOUT", 60.0, problems, minimum=5.0)

    if api_port:
        # A token is optional on loopback and mandatory off it. The line is
        # drawn at reachability, not at principle: 127.0.0.1 cannot be reached
        # from another machine at all, so the only thing an unauthenticated
        # listener there exposes is this machine to itself. Bind it anywhere
        # else and the token is the only thing between the network and someone's
        # WhatsApp account, so it stops being optional.
        if api_host not in ("127.0.0.1", "localhost", "::1"):
            if not api_token:
                problems.append(
                    f"API_TOKEN is required when API_HOST is not loopback (it is {api_host}). "
                    f"Anything that can reach port {api_port} would be able to send WhatsApp "
                    f"messages as you. Generate one with:  "
                    f"python -c \"import secrets; print(secrets.token_urlsafe(32))\""
                )
            elif len(api_token) < 16:
                problems.append(
                    f"API_TOKEN is only {len(api_token)} characters. Use at least 16 — this "
                    f"token is the only thing standing between the network and your WhatsApp."
                )
            else:
                warnings.append(
                    f"The send API will accept connections from other machines (API_HOST="
                    f"{api_host}). Anything that can reach port {api_port} and holds the token "
                    f"can send WhatsApp messages as you."
                )

    raw_clipboard = (values.get("SENDER_USE_CLIPBOARD") or "").strip().lower()
    sender_use_clipboard = raw_clipboard not in {"0", "false", "no", "off"}

    poll_raw = (values.get("POLL_INTERVAL") or "").strip()
    if poll_raw and poll_raw != str(POLL_INTERVAL_SECONDS):
        warnings.append(
            f"POLL_INTERVAL={poll_raw} in .env is ignored — the poll interval is fixed "
            f"at {POLL_INTERVAL_SECONDS} seconds."
        )

    if problems:
        raise ConfigError(problems)

    return Settings(
        mongodb_uri=mongodb_uri,
        database_name=database_name,
        webhook_template=webhook_template,
        json_backup_folder=folder,
        json_autosave_interval=autosave,
        default_webhook=default_webhook,
        webhook_api_key=(values.get("WEBHOOK_API_KEY") or "").strip(),
        webhook_timeout=webhook_timeout,
        webhook_max_retries=max_retries,
        whatsapp_window_title=(values.get("WHATSAPP_WINDOW_TITLE") or "WhatsApp").strip(),
        log_level=log_level,
        sender_use_clipboard=sender_use_clipboard,
        relay_enabled=relay_enabled,
        relay_poll_interval=relay_poll_interval,
        api_host=api_host,
        api_port=api_port,
        api_token=api_token,
        api_send_timeout=api_send_timeout,
        env_path=path,
        warnings=tuple(warnings),
    )
