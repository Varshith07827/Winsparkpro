"""The inbound send API — an HTTP listener that sends WhatsApp messages.

    your server ──POST {"id": "9423", "message": "Hello"}──▶ this app
                                                              │
                                                              ▼
                                             resolve id → chat → UIA send
                                                              │
                ◀──── 200 {"ok": true, "chat": "Varshith"} ────┘

This is a **remote send capability for someone's personal WhatsApp account**,
which is why the defaults are the shape they are:

* **Off unless a port is configured.** Nothing listens by default.
* **A token is required off loopback.** The line is drawn at reachability:
  127.0.0.1 cannot be reached from another machine at all, so an
  unauthenticated listener there exposes this machine only to itself. Bound
  anywhere else, the token is the only thing between the network and someone's
  WhatsApp account, and `config.py` refuses to start without one.
* **Bound to 127.0.0.1 unless told otherwise.** The default cannot be reached
  from another machine at all. Binding publicly is a deliberate act, and the
  startup screen says so.

Built on the standard library's `ThreadingHTTPServer`: one POST handler and a
health check do not justify a web framework, and this keeps the dependency list
at seven packages.

**The response is not sent until the message is.** A request blocks on the HTTP
call to OpenWA and only then returns 200, so the caller learns whether the
gateway accepted the message rather than merely that it was queued.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from wadam import constants

logger = logging.getLogger(__name__)

# Paths that mean "send this message". Several, because the caller has usually
# already written their integration against one of them.
SEND_PATHS = {"/", "/send", "/wam", "/wam/", "/send/"}
HEALTH_PATHS = {"/health", "/health/", "/status", "/status/"}

# The message text is read from the first of these present, mirroring the
# leniency of the outbound response parser. `msg` is in the list because it is
# what people actually type: the first hand-written call against this API used
# it and got "Missing message" back, which is a poor way to greet a caller who
# did nothing wrong.
_TEXT_KEYS = ("message", "msg", "text", "reply", "body")
_ID_KEYS = ("id", "chat", "chat_id", "contact", "to")

# How many sends may be in flight at once. Sends are serialized downstream by
# the automation lock anyway; this only stops an enthusiastic caller from
# parking hundreds of blocked threads on the queue.
MAX_CONCURRENT_SENDS = 8

_MAX_BODY_BYTES = 256 * 1024


@dataclass
class SendResponse:
    """What the handler writes back. Constructed by the host, not in here, so
    all the policy lives in one place."""

    status: int
    payload: dict[str, Any] = field(default_factory=dict)


class SendApiServer:
    """Owns the socket and the thread. The actual sending is a callback, so this
    class knows nothing about WhatsApp, MongoDB or Qt."""

    def __init__(self, host: str, port: int, token: str,
                 send: Callable[[str, str], SendResponse],
                 status: Optional[Callable[[str], SendResponse]] = None) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._send = send
        self._status = status
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_SENDS)
        self._requests = 0
        self._last_error = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Bind and serve. Raises OSError if the port is taken — that is a
        startup error, not something to swallow: a listener the caller thinks
        is running but isn't would fail silently forever."""
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="wadam-send-api", daemon=True
        )
        self._thread.start()
        logger.info("Send API listening on http://%s:%d/", self._host, self._port)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- state -------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def request_count(self) -> int:
        return self._requests

    @property
    def status_text(self) -> str:
        if not self.running:
            return "disabled"
        return f"listening on {self._host}:{self._port} · {self._requests} request(s)"

    # -- request handling --------------------------------------------------

    @property
    def authentication_required(self) -> bool:
        return bool(self._token)

    def authorized(self, headers) -> bool:
        """Bearer token, or `X-API-Token`. Deliberately not a query parameter:
        URLs end up in access logs, browser history and error reports, and a
        token that sends WhatsApp messages does not belong in any of them.

        **An empty token disables authentication.** Configuration refuses that
        combination unless the listener is bound to loopback, where nothing off
        this machine can reach it."""
        if not self._token:
            return True
        supplied = ""
        authorization = headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied:
            supplied = (headers.get("X-API-Token") or "").strip()
        if not supplied:
            return False
        # Constant-time: the token is a shared secret and this endpoint is
        # reachable by whoever can route to it.
        import hmac

        return hmac.compare_digest(supplied, self._token)

    def handle_status(self, outgoing_id: str) -> SendResponse:
        """Look up one queued message. Authenticated exactly like a send: it
        returns message text and chat names, so it is not health-check data."""
        if self._status is None:
            return SendResponse(501, {"ok": False, "code": "unsupported",
                                      "error": "Status lookup is not available."})
        if not outgoing_id:
            return SendResponse(400, {"ok": False, "code": "bad_request",
                                      "error": "Missing message id."})
        return self._status(outgoing_id)

    def handle_send(self, body: bytes) -> SendResponse:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as ex:
            return SendResponse(400, {"ok": False, "code": "bad_json",
                                      "error": f"Body is not valid JSON: {ex}"})
        if not isinstance(parsed, dict):
            return SendResponse(400, {"ok": False, "code": "bad_json",
                                      "error": "Body must be a JSON object."})

        identifier = _first_string(parsed, _ID_KEYS)
        text = _first_string(parsed, _TEXT_KEYS)
        if not identifier:
            return SendResponse(400, {"ok": False, "code": "missing_id",
                                      "error": 'Missing "id" — the chat to send to.'})
        if not text:
            return SendResponse(400, {"ok": False, "code": "missing_message",
                                      "error": 'Missing "message" — nothing to send.'})

        if not self._semaphore.acquire(blocking=False):
            return SendResponse(503, {
                "ok": False, "code": "busy",
                "error": f"{MAX_CONCURRENT_SENDS} sends are already in flight; retry shortly.",
            })
        try:
            self._requests += 1
            return self._send(identifier, text)
        finally:
            self._semaphore.release()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "app": constants.APP_NAME,
            "version": constants.APP_VERSION,
            "requests": self._requests,
        }


def _first_string(payload: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # An id sent unquoted — {"id": 9423} — is the commonest integration
            # slip, and refusing it would teach nobody anything.
            return str(value)
    return ""


def _make_handler(server: SendApiServer):
    class _Handler(BaseHTTPRequestHandler):
        server_version = f"{constants.APP_SHORT_NAME}/{constants.APP_VERSION}"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > _MAX_BODY_BYTES:
                # Not drained: we are declining to read it, which is the point.
                # The client may see a reset instead of this response, and that
                # is the correct trade for refusing to buffer arbitrary data.
                self._respond(413, {"ok": False, "code": "too_large",
                                    "error": "Request body is too large."})
                return

            # The body is read BEFORE the path and auth checks, even though
            # those may reject the request without ever looking at it. Replying
            # while unread data sits in the socket makes Windows reset the
            # connection, so the caller gets "connection aborted" instead of the
            # 401 or 404 explaining what they got wrong — which is a miserable
            # thing to debug from the far end.
            body = self.rfile.read(length) if length > 0 else b""

            path = self.path.split("?", 1)[0]
            if path not in SEND_PATHS:
                self._respond(404, {"ok": False, "code": "not_found",
                                    "error": f"No endpoint at {path}. POST to / or /send."})
                return
            if not server.authorized(self.headers):
                self._respond(401, {
                    "ok": False, "code": "unauthorized",
                    "error": "Missing or incorrect token. Send it as "
                             "'Authorization: Bearer <API_TOKEN>'.",
                })
                return
            if not body:
                self._respond(400, {"ok": False, "code": "empty_body",
                                    "error": "Request body is empty."})
                return
            try:
                response = server.handle_send(body)
                if not isinstance(response, SendResponse):
                    raise TypeError(
                        f"send callback returned {type(response).__name__}, expected SendResponse"
                    )
            except Exception as ex:  # noqa: BLE001 - never leak a traceback to the caller
                logger.exception("Send API request failed")
                response = SendResponse(500, {"ok": False, "code": "internal",
                                              "error": f"{type(ex).__name__}: {ex}"})
            # Inside the guard as well: a malformed response object used to
            # raise HERE, past the except, so the caller got a dropped
            # connection instead of an error they could read.
            self._respond(response.status, response.payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
            path = self.path.split("?", 1)[0]
            if path in HEALTH_PATHS:
                # Deliberately unauthenticated and deliberately empty of detail:
                # it answers "is this listening?" and nothing else. No chat
                # names, no counts that would describe someone's conversations.
                self._respond(200, server.health())
                return

            marker = "/status/"
            if marker in path:
                # Authenticated: unlike /health this returns a chat name and the
                # message text.
                if not server.authorized(self.headers):
                    self._respond(401, {
                        "ok": False, "code": "unauthorized",
                        "error": "Missing or incorrect token. Send it as "
                                 "'Authorization: Bearer <API_TOKEN>'.",
                    })
                    return
                outgoing_id = path.rsplit(marker, 1)[1].strip("/")
                response = server.handle_status(outgoing_id)
                self._respond(response.status, response.payload)
                return

            self._respond(405, {"ok": False, "code": "method_not_allowed",
                                "error": "Use POST to send a message, GET "
                                         "/wam/status/<id> for one message, or "
                                         "GET /health."})

        def _respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The caller gave up while we were sending. The WhatsApp message
                # went out regardless, which is why this is a debug line and not
                # an error.
                logger.debug("Send API client disconnected before the response")

        def log_message(self, fmt: str, *args) -> None:
            logger.debug("send-api %s", fmt % args)

    return _Handler
