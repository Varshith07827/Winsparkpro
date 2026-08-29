"""The service — an HTTP listener, and the state the window renders.

This replaces `wadam/engine/engine.py`, which was 1,317 lines of polling loop:
find the WhatsApp window, read the chat list every three seconds, diff it
against MongoDB, decide which chats were worth opening, hand them to a
single-file worker, and hold a lock so two sends could never overlap. All of
that existed because the only way to know a message had arrived was to look.

OpenWA tells us. So there is no loop, no worker queue, and no automation lock —
just a threaded HTTP server whose handler runs the pipeline.

**Why a thread pool is safe now.** winSpark serialized everything because two
concurrent UI-Automation sends would race for the same foreground window and
put a message in the wrong conversation. Sends are HTTP calls now; they are
independent, and OpenWA does its own pacing. The one piece of shared state is
the per-chat cooldown, which takes a lock.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional

from wadam.config import Settings
from wadam.domain.models import ChatConfig
from wadam.engine.directory import Directory
from wadam.engine.guards import Cooldown
from wadam.engine.metrics import Metrics, MetricsSnapshot
from wadam.engine.pipeline import MessagePipeline, Outcome
from wadam.engine.webhook import WebhookClient
from wadam.openwa import OpenWAClient, inbound
from wadam.storage.media import MediaStore
from wadam.storage.repository import Repository

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
HEALTH_PATHS = {"/health", "/health/", "/status", "/status/"}
WEBHOOK_PATHS = {"", "/hook", "/webhook", "/openwa"}


@dataclass(frozen=True)
class EngineSnapshot:
    """Everything the window renders, read in one go so the UI never has to
    ask the repository a question mid-paint."""

    chats: List[ChatConfig] = field(default_factory=list)
    contact_count: int = 0
    listening: bool = False
    listen_address: str = ""
    session_status: str = "unknown"
    session_phone: str = ""
    openwa_ok: bool = False
    mongo_status: str = ""
    mongo_ok: bool = False
    json_status: str = ""
    json_ok: bool = False
    metrics: MetricsSnapshot = field(default_factory=MetricsSnapshot)


class AutomationService:
    """Owns the listener, the pipeline, and the snapshot the UI subscribes to."""

    def __init__(self, settings: Settings, repository: Repository,
                 on_snapshot: Optional[Callable[[EngineSnapshot], None]] = None) -> None:
        self._settings = settings
        self._repo = repository
        self._on_snapshot = on_snapshot
        self._metrics = Metrics()
        self._client = OpenWAClient(
            settings.openwa_url, settings.openwa_api_key, settings.openwa_session_id,
        )
        # Created here rather than lazily on the first photo: the outbox has to
        # exist for someone to put a file in it before anything has ever been
        # received, and a directory that appears only after the first inbound
        # image is a directory nobody can find when they need it.
        self._media = MediaStore(settings.media_folder, settings.media_max_bytes)
        self._media.prepare()

        self._pipeline = MessagePipeline(
            repository=repository,
            client=self._client,
            webhook=WebhookClient(
                timeout=settings.webhook_timeout,
                max_retries=settings.webhook_max_retries,
                api_key=settings.webhook_api_key,
            ),
            cooldown=Cooldown(settings.cooldown_seconds),
            metrics=self._metrics,
            answer_groups=settings.answer_groups,
            default_webhook=settings.default_webhook,
            media=self._media,
        )
        self.directory = Directory(repository, self._client)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._session: dict = {}

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._server is not None:
            return

        _refuse_if_port_is_taken(self._settings.webhook_host, self._settings.webhook_port)

        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(
            (self._settings.webhook_host, self._settings.webhook_port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="wadam-webhook", daemon=True)
        self._thread.start()
        logger.info("listening for OpenWA deliveries on %s", self.listen_address)
        if not self._settings.webhook_secret:
            logger.warning(
                "no WEBHOOK_SECRET — anything that can reach this port can make your "
                "account send messages. Acceptable on loopback, nowhere else.")
        self.publish()

    def stop(self, timeout: float = 5.0) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server, self._thread = None, None
        logger.info("stopped listening")

    @property
    def listen_address(self) -> str:
        return f"http://{self._settings.webhook_host}:{self._settings.webhook_port}/hook"

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def media(self) -> MediaStore:
        """The media directory. Exposed so the send API and the window read
        and write media through the same confinement rule the pipeline does."""
        return self._media

    @property
    def client(self) -> OpenWAClient:
        return self._client

    @property
    def pipeline(self) -> MessagePipeline:
        return self._pipeline

    # ── the one thing the UI can change ───────────────────────────────

    def set_chat_automation(self, chat_id: str, enabled: bool) -> None:
        """Turn a chat's automation on or off.

        The only write the window performs. winSpark's tick box also *deleted*
        everything the chat had stored when switched off; that behaviour is
        gone — turning automation off should stop replies, not destroy the
        history you turned it off in order to read.
        """
        chat = self._repo.get_chat(chat_id)
        if chat is None or chat.automation_enabled == enabled:
            return
        chat.automation_enabled = enabled
        self._repo.save_chat(chat)
        self._repo.log("INFO", "automation.toggled", chat_id=chat_id, chat_name=chat.chat_name,
                       message=f"Automation turned {'on' if enabled else 'off'}.")
        self.publish()

    def set_chat_webhook(self, chat_id: str, url: str) -> None:
        """Point a chat at its own endpoint. Empty means "use the default".

        No validation beyond the scheme: an endpoint that is not up yet is a
        normal state while someone is setting one up, and refusing to store the
        URL until it answers would make it impossible to configure ahead of
        time. A URL that never answers shows as a failure on the chat instead.
        """
        chat = self._repo.get_chat(chat_id)
        if chat is None or chat.webhook_url == url:
            return
        chat.webhook_url = url
        self._repo.save_chat(chat)
        self.publish()

    # ── snapshots ─────────────────────────────────────────────────────

    def refresh_session(self) -> None:
        """Ask OpenWA how the session is. Called on a timer by the UI."""
        self._session = self._client.session_status()

    def sync_directory(self) -> dict:
        """Pull the chat list and address book from OpenWA.

        Slow — a first run resolves a phone number per chat at roughly a second
        each — so callers run it off the GUI thread.
        """
        summary = self.directory.sync()
        self.publish()
        return summary

    def snapshot(self) -> EngineSnapshot:
        status = self._repo.status()
        session_status = str(self._session.get("status") or "unknown")
        return EngineSnapshot(
            chats=self._repo.list_chats(),
            contact_count=self._repo.contact_count(),
            listening=self.running,
            listen_address=self.listen_address,
            session_status=session_status,
            session_phone=str(self._session.get("phone") or ""),
            openwa_ok=session_status == "ready",
            mongo_status=status.get("mongodb", ""),
            mongo_ok=status.get("mongodb_ok") == "yes",
            json_status=status.get("json", ""),
            json_ok=status.get("json_ok") == "yes",
            metrics=self._metrics.snapshot(),
        )

    def publish(self) -> None:
        if self._on_snapshot is not None:
            self._on_snapshot(self.snapshot())

    # ── the delivery path ─────────────────────────────────────────────

    def handle_delivery(self, body: bytes, signature: Optional[str]) -> tuple[int, dict]:
        """Process one webhook delivery. Returns (http_status, response_body)."""
        if not inbound.verify_signature(body, signature, self._settings.webhook_secret):
            self._metrics.record_rejected()
            logger.warning("rejected a delivery with a bad or missing signature")
            return 401, {"ok": False, "error": "invalid signature"}

        self._metrics.record_delivery()

        payload = inbound.parse_body(body)
        if payload is None:
            # Malformed on arrival; sending it again will not fix it.
            return 200, {"ok": True, "action": "ignored", "reason": "unparseable body"}

        msg = inbound.parse_delivery(payload)
        if msg is None:
            return 200, {"ok": True, "action": "ignored", "reason": "no message in payload"}

        outcome: Outcome = self._pipeline.process(msg)
        self.publish()
        return 200, outcome.as_response()


def _refuse_if_port_is_taken(host: str, port: int, wait_seconds: float = 20.0) -> None:
    """Raise if something already answers on this port.

    `http.server` sets `allow_reuse_address = 1`, so on Windows a second
    instance binds the SAME port without any error and WhatsApp's deliveries
    are split between the two unpredictably. That is not theoretical: a window
    left running from before a code change kept answering alongside a freshly
    started service, replied out of the old build, and every symptom pointed at
    the new code being broken. Nothing logged a conflict, because as far as
    either process was concerned there wasn't one.

    So the check is a *connection*, not a bind — a bind would succeed and prove
    nothing.

    It waits rather than refusing immediately, because the commonest reason
    the port is busy is entirely legitimate: `systemctl restart` starts the new
    process while the old one is still letting go, and a guard that gives up
    instantly turns every restart into a coin toss. A second instance running
    indefinitely still fails, which is the case worth failing for.
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.monotonic() + wait_seconds
    waited = False

    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            if probe.connect_ex((probe_host, port)) != 0:
                if waited:
                    logger.info("%s:%d came free", probe_host, port)
                return
        if time.monotonic() >= deadline:
            raise OSError(
                f"something is still listening on {probe_host}:{port} after "
                f"{wait_seconds:g}s. Another copy of this application is probably "
                f"running — stop it first. Two listeners on one port do not "
                f"conflict here, they silently share the traffic."
            )
        if not waited:
            logger.info("%s:%d is busy — waiting up to %gs for it to come free "
                        "(a restart usually needs a moment)", probe_host, port, wait_seconds)
            waited = True
        time.sleep(0.5)


def _make_handler(service: AutomationService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "wadam/2.0"

        def log_message(self, fmt: str, *args) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def _respond(self, status: int, payload: dict) -> None:
            import json

            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            if self.path.split("?")[0] in HEALTH_PATHS:
                snapshot = service.snapshot()
                self._respond(200, {
                    "ok": True,
                    "listening": snapshot.listening,
                    "session": snapshot.session_status,
                    "mongo": snapshot.mongo_status,
                    "deliveries": snapshot.metrics.deliveries,
                    "replies": snapshot.metrics.replies_sent,
                    "send_failures": snapshot.metrics.send_failures,
                })
            else:
                self._respond(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            if self.path.split("?")[0].rstrip("/") not in WEBHOOK_PATHS:
                self._respond(404, {"ok": False, "error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._respond(400, {"ok": False, "error": "bad Content-Length"})
                return

            if length > MAX_BODY_BYTES:
                self._respond(413, {"ok": False, "error": "body too large"})
                return

            body = self.rfile.read(length) if length else b""
            status, payload = service.handle_delivery(
                body, self.headers.get(inbound.SIGNATURE_HEADER))
            self._respond(status, payload)

    return Handler
