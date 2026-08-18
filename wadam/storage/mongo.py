"""MongoDB — the primary datastore.

Connection notes that matter and are easy to get wrong:

* **The database comes from `DATABASE_NAME`, never from the URI path.**
  `mongodb://localhost:27017/admin` is the shape you get by habit — the auth
  database really does belong in a connection string, but as `?authSource=admin`,
  not as the path. Trusting the path is how application collections end up in
  MongoDB's own `admin` database while the configured one sits empty.
* **Atlas and a local server need different timeouts.** A local `mongod` either
  answers instantly or isn't running; Atlas has to resolve an SRV record and
  finish a TLS handshake across the internet first, so a short timeout there is
  a false negative waiting to happen.
* **`mongodb+srv://` requires dnspython** (`pymongo[srv]`), or every Atlas
  connection string fails no matter how correct it is.

Connectivity is required at startup — a store the app can't reach is a startup
error, not something to limp past. A connection lost *during* a run is
different: writes keep mirroring to JSON, the failure is surfaced in the UI,
and the next operation retries.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from wadam import constants

logger = logging.getLogger(__name__)

LOCAL_TIMEOUT_MS = 2000
REMOTE_TIMEOUT_MS = 10000

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}


class MongoUnavailableError(RuntimeError):
    """MongoDB could not be reached. Carries a message written for a person."""


def is_srv_uri(uri: str) -> bool:
    return (uri or "").strip().lower().startswith("mongodb+srv://")


def is_local_uri(uri: str) -> bool:
    if is_srv_uri(uri):
        return False  # SRV is always a DNS-resolved (i.e. remote) deployment
    host_part = (uri or "").split("://", 1)[-1].rsplit("@", 1)[-1]
    host = host_part.split("/", 1)[0].split("?", 1)[0].split(",")[0]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host.strip().lower() in _LOCAL_HOSTS


def uri_uses_tls(uri: str) -> bool:
    """Does this URI actually ask for TLS?

    `mongodb+srv://` turns it on by default — that is how Atlas connects — and
    otherwise it takes an explicit `tls=true` or the older `ssl=true`. A plain
    `mongodb://host:27017` does not, however remote the host is, so nothing
    here should assume otherwise. Either option can also be spelled `=false`,
    which wins over the SRV default."""
    text = (uri or "").strip()
    query = text.split("?", 1)[1].lower() if "?" in text else ""
    options = dict(
        pair.split("=", 1) for pair in query.split("&")
        if "=" in pair
    )
    for key in ("tls", "ssl"):
        if key in options:
            return options[key].strip() in ("true", "1", "yes")
    return is_srv_uri(text)


def timeout_for(uri: str) -> int:
    return LOCAL_TIMEOUT_MS if is_local_uri(uri) else REMOTE_TIMEOUT_MS


class MongoStore:
    """Thin, explicit wrapper over the six collections. Deliberately not a
    generic ORM: every query this application makes is written out here, so the
    access pattern is readable in one file."""

    def __init__(self, uri: str, database_name: str) -> None:
        self._uri = uri
        self._database_name = database_name
        self._client = None
        self._db = None
        self._connected = False
        self._last_error = ""

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Connect and verify with a real round-trip (`ping`). pymongo connects
        lazily, so without the ping a wrong host looks fine until the first
        query — by which point the startup screen is long gone."""
        try:
            from pymongo import MongoClient
        except ImportError as ex:  # pragma: no cover
            raise MongoUnavailableError(
                "pymongo is not installed. Run: pip install -r requirements.txt"
            ) from ex

        kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": timeout_for(self._uri),
            "connectTimeoutMS": timeout_for(self._uri),
            "appname": constants.APP_SHORT_NAME,
        }
        # TLS is the URI's business, not this function's.
        #
        # A CA bundle used to be attached to every non-local URI, on the
        # reasoning that a remote server means TLS. It does not: a plain
        # `mongodb://host:27017` on a private network speaks no TLS at all, and
        # supplying TLS options for a connection that has none is at best noise
        # and at worst a handshake nobody asked for.
        #
        # So the bundle is supplied only when the URI itself asks for TLS —
        # `mongodb+srv://`, which defaults it on, or an explicit `tls=true` /
        # `ssl=true`. Everything else connects exactly as written.
        if uri_uses_tls(self._uri):
            try:
                import certifi

                kwargs["tlsCAFile"] = certifi.where()
            except ImportError:  # pragma: no cover - fall back to the OS trust store
                pass

        try:
            client = MongoClient(self._uri, **kwargs)
            client.admin.command("ping")
        except Exception as ex:  # noqa: BLE001
            self._last_error = str(ex)
            raise MongoUnavailableError(self._friendly_error(ex)) from ex

        self._client = client
        self._db = client[self._database_name]
        self._connected = True
        self._last_error = ""
        self._ensure_indexes()
        logger.info("MongoDB connected — database %r", self._database_name)

    def _friendly_error(self, ex: Exception) -> str:
        text = str(ex)
        if is_srv_uri(self._uri) and "dnspython" in text.lower():
            return (
                "This is a mongodb+srv:// (Atlas) address, which needs dnspython to "
                "resolve. Install it with: pip install \"pymongo[srv]\""
            )
        if "ServerSelectionTimeoutError" in type(ex).__name__ or "timed out" in text.lower():
            where = "on this machine" if is_local_uri(self._uri) else "over the network"
            return (
                f"Could not reach MongoDB {where} within "
                f"{timeout_for(self._uri) / 1000:.0f}s.\n\n{text}"
            )
        if "Authentication failed" in text:
            return f"MongoDB rejected the credentials in MONGODB_URI.\n\n{text}"
        return text

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status_text(self) -> str:
        if self._connected:
            return f"connected · {self._database_name}"
        return f"disconnected — {self._last_error}" if self._last_error else "disconnected"

    def note_failure(self, ex: Exception) -> None:
        """Record a runtime failure so the UI can show it. The store stays
        'connected' in pymongo's eyes — it reconnects on its own — so this is
        about telling the user, not about state management."""
        self._connected = False
        self._last_error = str(ex)

    def note_success(self) -> None:
        self._connected = True
        self._last_error = ""

    # -- collections -------------------------------------------------------

    @property
    def database(self):
        """The database handle, for the few operations that are about the
        database itself rather than a collection in it."""
        if self._db is None:
            raise MongoUnavailableError("MongoDB is not connected.")
        return self._db

    def _collection(self, name: str):
        if self._db is None:
            raise MongoUnavailableError("MongoDB is not connected.")
        return self._db[name]

    @property
    def chat_configs(self):
        return self._collection(constants.COLLECTION_CHAT_CONFIGS)

    @property
    def messages(self):
        return self._collection(constants.COLLECTION_MESSAGES)

    @property
    def webhooks(self):
        return self._collection(constants.COLLECTION_WEBHOOKS)

    @property
    def outgoing(self):
        return self._collection(constants.COLLECTION_OUTGOING)

    @property
    def application_state(self):
        return self._collection(constants.COLLECTION_APPLICATION_STATE)

    def _ensure_indexes(self) -> None:
        """Indexes are created once at startup. The unique key on
        `messages.message_key` is not an optimization — it's the last line of
        defence for deduplication: even if two code paths race to insert the
        same bubble, the database refuses the second one."""
        try:
            self.chat_configs.create_index("chat_id", unique=True)
            self.chat_configs.create_index("chat_name")
            self.messages.create_index("message_key", unique=True)
            self.messages.create_index([("chat_id", 1), ("detected_at", -1)])
            # Sparse: only relayed messages carry an external_ref, and the
            # relay dedup lookup is the only thing that reads it.
            self.messages.create_index(
                [("chat_id", 1), ("origin", 1), ("external_ref", 1)], sparse=True)
            self.webhooks.create_index("webhook_id", unique=True)
            self.outgoing.create_index("outgoing_id", unique=True)
            # The queue is read as "what is pending, oldest first, per chat".
            self.outgoing.create_index([("status", 1), ("chat_id", 1), ("sequence", 1)])
            self.webhooks.create_index([("chat_id", 1), ("created_at", -1)])
        except Exception as ex:  # noqa: BLE001
            # A read-only user or an existing conflicting index shouldn't stop
            # the app — everything still works, just slower and with the
            # dedup guarantee falling back to the in-code check.
            logger.warning("Could not create MongoDB indexes: %s", ex)

    # -- convenience -------------------------------------------------------

    def counts(self) -> dict[str, int]:
        try:
            return {
                "chats": self.chat_configs.estimated_document_count(),
                "messages": self.messages.estimated_document_count(),
                "webhooks": self.webhooks.estimated_document_count(),
            }
        except Exception:  # noqa: BLE001
            return {}

def strip_object_id(document: Optional[dict]) -> Optional[dict]:
    """Drop Mongo's `_id` so a document can be handed straight to a dataclass
    and to `json.dump` (an ObjectId is not JSON-serializable)."""
    if document is None:
        return None
    document.pop("_id", None)
    return document
