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

from wadam.constants import DATABASE_NAME

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Bind addresses that cannot be reached from another machine.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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

    # --- OpenWA, the transport -------------------------------------------
    #: Where the OpenWA gateway is. This process talks to it over HTTP; there
    #: is no WhatsApp Desktop and no UI Automation any more.
    openwa_url: str = "http://localhost:2785"
    openwa_api_key: str = ""
    #: The session's UUID, not its name. `GET /api/sessions` lists them.
    #: Optional. With one session on the instance it is discovered; with
    #: several, startup asks which.
    openwa_session_id: str = ""

    # --- the webhook this process listens on ------------------------------
    #: OpenWA POSTs here when a message arrives. Bound to 0.0.0.0 by default
    #: because OpenWA usually runs in Docker and reaches the host as
    #: `host.docker.internal` — a loopback bind is unreachable from there.
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8765
    #: Optional. Generated once and stored when unset, then handed to OpenWA
    #: directly, so the two ends cannot disagree about it.
    webhook_secret: str = ""

    #: Register (and keep current) this application's webhook in OpenWA at
    #: startup. Off only if something else owns that registration.
    register_webhook: bool = True
    #: The address OpenWA should deliver to. Defaults to
    #: `host.docker.internal:<port>` — OpenWA resolves it from inside its
    #: container, where `localhost` is the container itself.
    webhook_public_url: str = ""

    # --- the outbound webhook ---------------------------------------------
    #: Given to a chat that has no URL of its own. Without it, and without a
    #: per-chat URL, an incoming message is stored and nothing is dispatched.
    default_webhook: str = ""
    #: Sent as `Authorization: Bearer …` to the endpoint.
    webhook_api_key: str = ""
    webhook_timeout: float = 20.0
    #: Retries cover transport failures, 5xx and 429. A 4xx fails immediately.
    webhook_max_retries: int = 3

    #: Per-chat quiet period. Bounds an automation-answering-automation loop.
    cooldown_seconds: float = 60.0
    #: Answer group chats. Off by default: a bot in a group is louder than a
    #: bot in a DM, and easier to turn on than to live down.
    answer_groups: bool = False

    log_level: str = "INFO"

    # Inbound send API — the second way to send, for something that wants to
    # push a message in rather than answer one. Off unless a port is set.
    api_host: str = "127.0.0.1"
    api_port: int = 0
    api_token: str = ""
    api_send_timeout: float = 60.0

    env_path: Optional[Path] = None
    # Non-fatal problems worth telling the user about at startup.
    warnings: tuple[str, ...] = ()

    def redacted(self) -> dict[str, object]:
        """The settings as they're mirrored into `settings.json` — credentials
        removed, because that file is meant to be opened and read by a human."""
        return {
            "mongodb_uri": _redact_uri(self.mongodb_uri),
            "database_name": self.database_name,
            "json_backup_folder": str(self.json_backup_folder),
            "json_autosave_interval": self.json_autosave_interval,
            "openwa_url": self.openwa_url,
            "openwa_api_key": "***" if self.openwa_api_key else "",
            "openwa_session_id": self.openwa_session_id,
            "webhook_host": self.webhook_host,
            "webhook_port": self.webhook_port,
            "webhook_secret": "***" if self.webhook_secret else "(generated)",
            "register_webhook": self.register_webhook,
            "webhook_public_url": self.webhook_public_url,
            "default_webhook": self.default_webhook,
            "webhook_api_key": "***" if self.webhook_api_key else "",
            "webhook_timeout": self.webhook_timeout,
            "webhook_max_retries": self.webhook_max_retries,
            "cooldown_seconds": self.cooldown_seconds,
            "answer_groups": self.answer_groups,
            "log_level": self.log_level,
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
        "MONGODB_URI", "DATABASE_NAME",
        "JSON_BACKUP_FOLDER", "JSON_AUTOSAVE_INTERVAL",
        "OPENWA_URL", "OPENWA_API_KEY", "OPENWA_SESSION_ID",
        "DEFAULT_WEBHOOK", "WEBHOOK_API_KEY", "WEBHOOK_TIMEOUT", "WEBHOOK_MAX_RETRIES",
        "WEBHOOK_HOST", "WEBHOOK_PORT", "WEBHOOK_SECRET",
        "REGISTER_WEBHOOK", "WEBHOOK_PUBLIC_URL",
        "COOLDOWN_SECONDS", "ANSWER_GROUPS", "LOG_LEVEL",
        "API_HOST", "API_PORT", "API_TOKEN", "API_SEND_TIMEOUT",
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

    # --- OpenWA, the transport --------------------------------------------
    openwa_url = (values.get("OPENWA_URL") or "http://localhost:2785").strip().rstrip("/")
    if not openwa_url.startswith(("http://", "https://")):
        problems.append(f"OPENWA_URL must be an http:// or https:// URL (got {openwa_url!r}).")

    openwa_api_key = (values.get("OPENWA_API_KEY") or "").strip()
    if not openwa_api_key:
        problems.append(
            "OPENWA_API_KEY is required. It is in OpenWA's data/.api-key file, "
            "or on its dashboard's API keys page."
        )

    # Not required: with exactly one session on the instance it is discovered
    # at startup, and with several the startup error names them.
    openwa_session_id = (values.get("OPENWA_SESSION_ID") or "").strip()

    folder_raw = (values.get("JSON_BACKUP_FOLDER") or "backup").strip() or "backup"
    folder = Path(folder_raw)
    if not folder.is_absolute():
        folder = app_dir() / folder

    autosave = _as_float(values, "JSON_AUTOSAVE_INTERVAL", 15.0, problems, minimum=0.0)
    log_level = (values.get("LOG_LEVEL") or "INFO").strip().upper() or "INFO"
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        problems.append(f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL (got {log_level!r}).")
        log_level = "INFO"

    # --- the webhook this process listens on -------------------------------
    webhook_host = (values.get("WEBHOOK_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    webhook_port = _as_int(values, "WEBHOOK_PORT", 8765, problems, minimum=1)
    if webhook_port > 65535:
        problems.append(f"WEBHOOK_PORT must be between 1 and 65535 (got {webhook_port}).")
        webhook_port = 8765

    # Not required: generated once and stored when unset, then given to OpenWA
    # directly. It used to be a value pasted into two places by hand, and when
    # they disagreed every delivery was refused with a 401 that looked like a
    # bug in this application.
    webhook_secret = (values.get("WEBHOOK_SECRET") or "").strip()
    if webhook_secret and len(webhook_secret) < 16:
        problems.append(
            f"WEBHOOK_SECRET is only {len(webhook_secret)} characters. Use at least 16, "
            f"or leave it unset and one will be generated."
        )

    register_raw = (values.get("REGISTER_WEBHOOK") or "").strip().lower()
    register_webhook = register_raw not in {"0", "false", "no", "off"}
    webhook_public_url = (values.get("WEBHOOK_PUBLIC_URL") or "").strip()
    if webhook_public_url and not webhook_public_url.startswith(("http://", "https://")):
        problems.append("WEBHOOK_PUBLIC_URL must be an http:// or https:// URL.")

    default_webhook = (values.get("DEFAULT_WEBHOOK") or "").strip()
    if default_webhook and not default_webhook.startswith(("http://", "https://")):
        problems.append("DEFAULT_WEBHOOK must be an http:// or https:// URL.")
    webhook_timeout = _as_float(values, "WEBHOOK_TIMEOUT", 20.0, problems, minimum=1.0)
    webhook_max_retries = _as_int(values, "WEBHOOK_MAX_RETRIES", 3, problems, minimum=0)

    cooldown_seconds = _as_float(values, "COOLDOWN_SECONDS", 60.0, problems, minimum=0.0)
    answer_groups = (values.get("ANSWER_GROUPS") or "").strip().lower() in _TRUE_VALUES

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
        if api_host not in _LOOPBACK_HOSTS:
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

    if problems:
        raise ConfigError(problems)

    return Settings(
        mongodb_uri=mongodb_uri,
        database_name=database_name,
        json_backup_folder=folder,
        json_autosave_interval=autosave,
        openwa_url=openwa_url,
        openwa_api_key=openwa_api_key,
        openwa_session_id=openwa_session_id,
        webhook_host=webhook_host,
        webhook_port=webhook_port,
        webhook_secret=webhook_secret,
        register_webhook=register_webhook,
        webhook_public_url=webhook_public_url,
        default_webhook=default_webhook,
        webhook_api_key=(values.get("WEBHOOK_API_KEY") or "").strip(),
        webhook_timeout=webhook_timeout,
        webhook_max_retries=webhook_max_retries,
        cooldown_seconds=cooldown_seconds,
        answer_groups=answer_groups,
        log_level=log_level,
        api_host=api_host,
        api_port=api_port,
        api_token=api_token,
        api_send_timeout=api_send_timeout,
        env_path=path,
        warnings=tuple(warnings),
    )
