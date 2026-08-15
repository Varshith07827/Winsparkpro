"""The persistence facade. Every write in the application goes through here,
and every write lands in both stores:

    caller ──▶ Repository ──▶ MongoDB   (primary, authoritative)
                          └─▶ JSON      (mirror, coalesced, atomic)

Nothing bypasses it. There is no code path that processes a message "in memory
only" and persists it later — the pipeline's contract is that a message is on
disk before the webhook is called, and the webhook response is on disk before a
reply is sent.

**Why an in-memory ring buffer feeds the JSON mirror.** The mirror could be
rebuilt by querying MongoDB on every flush, but then the backup is only
writable while the primary is healthy — precisely backwards for a backup. So
the repository keeps the newest N messages / webhooks / logs in memory (seeded
from MongoDB at startup, or from the mirror itself during recovery), and JSON
is written from that. A MongoDB outage degrades to "JSON keeps recording,
MongoDB catches up", not "both stop".

Every method here is synchronous and internally locked. The engine runs on an
asyncio loop and calls them through `asyncio.to_thread`, so a slow database
never stalls the three-second poll.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from wadam import constants
from wadam.constants import POLL_TOUCH_INTERVAL
from wadam.config import Settings
from wadam.domain.models import (
    ApplicationState,
    AutomationLog,
    ChatConfig,
    MessageStatus,
    OutgoingMessage,
    OutgoingStatus,
    PollState,
    StoredMessage,
    WebhookRecord,
    utcnow,
)
from wadam.storage.json_backup import AutosaveTimer, JsonBackupStore
from wadam.storage.mongo import MongoStore, strip_object_id

logger = logging.getLogger(__name__)


class Repository:
    def __init__(self, settings: Settings, mongo: MongoStore, backup: JsonBackupStore) -> None:
        self._settings = settings
        self._mongo = mongo
        self._backup = backup
        self._lock = threading.RLock()

        self._chats: dict[str, ChatConfig] = {}
        self._config_baseline: dict[str, dict] = {}
        self._messages: deque[StoredMessage] = deque(maxlen=constants.JSON_MESSAGE_LIMIT)
        self._message_keys: set[str] = set()
        self._webhooks: deque[WebhookRecord] = deque(maxlen=constants.JSON_WEBHOOK_LIMIT)
        self._logs: deque[AutomationLog] = deque(maxlen=constants.JSON_LOG_LIMIT)
        self._outgoing: dict[str, OutgoingMessage] = {}
        self._app_state = ApplicationState(version=constants.APP_VERSION)
        self._poll_state = PollState()
        self._autosave = AutosaveTimer(self.flush_json, settings.json_autosave_interval or 15.0)
        self._recovered_from_json = False
        # Epoch, so the first poll of a run always writes.
        self._last_poll_written = datetime(1970, 1, 1, tzinfo=timezone.utc)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Warm the cache from MongoDB, restore from the JSON mirror if the
        primary came up empty, then start the autosave timer."""
        self._load_from_mongo()
        self._load_local_only()
        if not self._chats:
            self._recover_from_json()
        self._migrate_legacy_chats()
        self._drop_retired_collections()
        self._app_state.run_count += 1
        self._app_state.started_at = utcnow()
        self._app_state.version = constants.APP_VERSION
        self.save_app_state(self._app_state)
        self._backup.set_section(constants.JSON_SETTINGS, self._settings.redacted())
        self._rebuild_all_sections()
        self._backup.flush(force=True)
        self._autosave.start()
        self.log("INFO", "app.started",
                 message=f"{constants.APP_NAME} {constants.APP_VERSION} started "
                         f"({len(self._chats)} chats known)")

    def stop(self) -> None:
        self._app_state.last_shutdown_at = utcnow()
        try:
            self.save_app_state(self._app_state)
        except Exception:  # noqa: BLE001 - shutting down anyway
            logger.warning("Could not record shutdown state", exc_info=True)
        self._autosave.stop()
        self._rebuild_all_sections()
        self._backup.flush(force=True)

    @property
    def recovered_from_json(self) -> bool:
        """True when MongoDB was empty and the mirror repopulated the cache —
        the disaster-recovery path actually firing, worth telling the user."""
        return self._recovered_from_json

    def _load_from_mongo(self) -> None:
        try:
            for document in self._mongo.chat_configs.find({}):
                chat = ChatConfig.from_document(strip_object_id(document))
                if chat.chat_id:
                    self._chats[chat.chat_id] = chat
                    self._config_baseline[chat.chat_id] = {
                        field: getattr(chat, field) for field in self.CONFIG_FIELDS}
            for document in (self._mongo.messages.find({})
                             .sort("detected_at", -1).limit(constants.JSON_MESSAGE_LIMIT)):
                message = StoredMessage.from_document(strip_object_id(document))
                self._messages.appendleft(message)
                self._message_keys.add(message.message_key)
            for document in (self._mongo.webhooks.find({})
                             .sort("created_at", -1).limit(constants.JSON_WEBHOOK_LIMIT)):
                self._webhooks.appendleft(WebhookRecord.from_document(strip_object_id(document)))
            for document in self._mongo.outgoing.find({}):
                queued = OutgoingMessage.from_document(strip_object_id(document))
                if queued.outgoing_id:
                    self._outgoing[queued.outgoing_id] = queued
            state = self._mongo.application_state.find_one({"_id": constants.SINGLETON_ID})
            if state:
                self._app_state = ApplicationState.from_document(strip_object_id(state))
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Could not load state from MongoDB: %s", ex)

    def _load_local_only(self) -> None:
        """Warm the things MongoDB no longer stores, from the JSON mirror.

        Logs and poll counters are diagnostics. They were in MongoDB because
        everything was, and that cost a billable write per log line and one per
        ten cycles forever. The mirror already held both, so this is where they
        come back from — and the window shows the same history it always did."""
        payload = self._backup.read_section(constants.JSON_LOGS)
        if isinstance(payload, list):
            for document in payload[-constants.JSON_LOG_LIMIT:]:
                try:
                    self._logs.append(AutomationLog.from_document(document))
                except Exception:  # noqa: BLE001 - a bad line is not worth failing over
                    continue
        state = self._backup.read_section(constants.JSON_APP_STATE)
        if isinstance(state, dict) and isinstance(state.get("poll_state"), dict):
            try:
                self._poll_state = PollState.from_document(state["poll_state"])
            except Exception:  # noqa: BLE001
                pass

    def _drop_retired_collections(self) -> None:
        """Remove `automation_logs` and `poll_state` from an existing database.

        They are written locally now, so what is in MongoDB is a frozen partial
        copy — worse than nothing, because the next person to query the database
        would find a log that silently stopped."""
        for name in constants.RETIRED_COLLECTIONS:
            try:
                if name not in self._mongo.database.list_collection_names():
                    continue
                self._mongo.database.drop_collection(name)
                self.log("INFO", "collection.retired",
                         message=f"Dropped {name!r} from MongoDB — it is kept locally now.")
            except Exception as ex:  # noqa: BLE001 - never block startup
                logger.warning("Could not drop the retired collection %s: %s", name, ex)

    def _migrate_legacy_chats(self) -> None:
        """Retire the four-digit `external_id`, and re-arm the panel probe for
        every chat an older build had already probed.

        Two things need doing, and one legacy key marks both.

        `external_id` no longer exists on `ChatConfig`, so `from_document`
        already ignores it — nothing breaks. But the key stays in MongoDB
        forever otherwise, and a stored field nothing reads is a trap for the
        next person querying `wa_events`.

        The second is not cosmetic. `is_group` used to be guessed from the
        sidebar preview and is now read from the info panel — but only when a
        chat is probed, and `phone_probed_at` stops a chat being probed twice.
        So a chat an older build probed carries a probe marker and NO panel
        verdict, and would keep the guess forever. Measured here: a chat named
        "Novus Tech Group" was stored with `is_group=False` and a phone number,
        with no way to tell whether a panel ever said so.

        Clearing the marker costs one panel open per chat, once, and settles it
        from the only reliable source."""
        try:
            legacy = list(self._mongo.chat_configs.find(
                {"external_id": {"$exists": True}}, {"chat_id": 1}))
            if not legacy:
                return
            self._mongo.chat_configs.update_many(
                {"external_id": {"$exists": True}},
                {"$unset": {"external_id": ""}, "$set": {"phone_probed_at": None}})
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001 - never block startup
            self._mongo.note_failure(ex)
            logger.error("Retiring the legacy contact id failed: %s", ex)
            return

        for document in legacy:
            chat = self._chats.get(document.get("chat_id") or "")
            if chat is not None:
                chat.phone_probed_at = None
        self.log("INFO", "chats.migrated",
                 message=f"Retired the four-digit contact id on {len(legacy)} chat(s), "
                         f"and re-armed the info-panel probe so each one's group "
                         f"status comes from the panel rather than a sidebar guess.")

    def _recover_from_json(self) -> None:
        """MongoDB had no chats. If the mirror does, this is a fresh/emptied
        database and the backup is the surviving copy — load it and write it
        back so the primary is whole again."""
        payload = self._backup.read_section(constants.JSON_CHATS)
        if not isinstance(payload, list) or not payload:
            return
        restored = 0
        for document in payload:
            try:
                chat = ChatConfig.from_document(document)
            except Exception:  # noqa: BLE001
                continue
            if not chat.chat_id:
                continue
            self._chats[chat.chat_id] = chat
            restored += 1
        if not restored:
            return
        self._recovered_from_json = True
        logger.warning("MongoDB held no chats — restored %d from the JSON mirror", restored)
        for chat in self._chats.values():
            self._mongo_upsert_chat(chat)
        self.log("WARNING", "recovery.from_json",
                 message=f"MongoDB was empty; restored {restored} chats from the JSON backup.")

    # -- chats -------------------------------------------------------------

    def list_chats(self) -> list[ChatConfig]:
        with self._lock:
            return list(self._chats.values())

    def get_chat(self, chat_id: str) -> Optional[ChatConfig]:
        with self._lock:
            return self._chats.get(chat_id)

    def save_chat(self, chat: ChatConfig, *, mirror: bool = True) -> ChatConfig:
        chat.updated_at = utcnow()
        with self._lock:
            self._chats[chat.chat_id] = chat
        self._mongo_upsert_chat(chat)
        if mirror:
            self._mark_chats_dirty()
        return chat

    def save_chats(self, chats: Iterable[ChatConfig]) -> None:
        """Bulk path for a polling cycle: one round-trip for every chat that
        changed, instead of one per chat."""
        batch = list(chats)
        if not batch:
            return
        now = utcnow()
        with self._lock:
            for chat in batch:
                chat.updated_at = now
                self._chats[chat.chat_id] = chat
        try:
            from pymongo import UpdateOne

            self._mongo.chat_configs.bulk_write(
                [UpdateOne({"chat_id": c.chat_id}, {"$set": c.to_document()}, upsert=True)
                 for c in batch],
                ordered=False,
            )
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Bulk chat save failed: %s", ex)
        self._mark_chats_dirty()

    def touch_last_poll(self, chat_ids: list[str], when: datetime) -> None:
        """Record "seen this cycle" for every chat in the sidebar — one Mongo
        update for the whole list, not one per chat. The JSON mirror picks it
        up on its next coalesced flush."""
        if not chat_ids:
            return
        with self._lock:
            for chat_id in chat_ids:
                chat = self._chats.get(chat_id)
                if chat is not None:
                    chat.last_poll_utc = when

        # In memory always; to MongoDB rarely.
        #
        # This is "when did we last look at this chat" — telemetry, worth
        # roughly nothing after a restart, and it was a write on a 3-second
        # timer whether or not anything happened. Measured: half of the entire
        # idle cost of the application, 864,000 writes a month on a cluster
        # where nobody had sent a message.
        #
        # A stale minute in a field nobody makes decisions from is not worth
        # paying for. The JSON mirror still gets it on every flush, so the
        # number on screen is as live as it ever was.
        if (when - self._last_poll_written).total_seconds() < POLL_TOUCH_INTERVAL:
            self._mark_chats_dirty()
            return
        self._last_poll_written = when
        try:
            self._mongo.chat_configs.update_many(
                {"chat_id": {"$in": chat_ids}}, {"$set": {"last_poll_utc": when}}
            )
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
        self._mark_chats_dirty()

    def delete_chat(self, chat_id: str) -> None:
        """Remove a chat and everything belonging to it. If the chat still
        exists in WhatsApp it will be rediscovered on the next poll — with a
        clean configuration, which is the point of the button."""
        with self._lock:
            chat = self._chats.pop(chat_id, None)
            self._messages = deque(
                (m for m in self._messages if m.chat_id != chat_id),
                maxlen=constants.JSON_MESSAGE_LIMIT,
            )
            self._message_keys = {m.message_key for m in self._messages}
            self._webhooks = deque(
                (w for w in self._webhooks if w.chat_id != chat_id),
                maxlen=constants.JSON_WEBHOOK_LIMIT,
            )
            self._outgoing = {k: v for k, v in self._outgoing.items() if v.chat_id != chat_id}
        try:
            self._mongo.chat_configs.delete_one({"chat_id": chat_id})
            self._mongo.messages.delete_many({"chat_id": chat_id})
            self._mongo.outgoing.delete_many({"chat_id": chat_id})
            self._mongo.webhooks.delete_many({"chat_id": chat_id})
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Deleting chat %s from MongoDB failed: %s", chat_id, ex)
        self.log("INFO", "chat.deleted", chat_id=chat_id,
                 chat_name=chat.chat_name if chat else "",
                 message="Chat and its stored messages were deleted.")
        self._rebuild_all_sections()
        self._backup.flush(force=True)

    #: What an outside editor owns. Everything else on a ChatConfig is runtime
    #: state this process is actively mutating — `seeded`, the last_* fields,
    #: the counters — and copying those back over a live object would undo
    #: whatever happened since the last save.
    CONFIG_FIELDS = ("automation_enabled", "webhook_url", "webhook_override",
                     "phone_number")

    def reload_chat_config(self) -> list[str]:
        """Re-read the configuration fields of every chat from MongoDB.

        The in-memory chats were loaded once at startup, so editing a webhook
        or an automation flag directly in the database did nothing until a
        restart — and the next `save_chat` wrote the stale value back over it.
        A poll loop that never re-reads its own configuration is only
        configurable through the window that happens to be running.

        Returns the ids that actually changed, so a caller can log a real edit
        without narrating every quiet pass.
        """
        try:
            documents = list(self._mongo.chat_configs.find(
                {}, {field: 1 for field in ("chat_id",) + self.CONFIG_FIELDS}))
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            return []

        changed: list[str] = []
        with self._lock:
            for document in documents:
                chat = self._chats.get(document.get("chat_id") or "")
                if chat is None:
                    continue          # discovery owns creation, not this
                baseline = self._config_baseline.setdefault(chat.chat_id, {})
                for field in self.CONFIG_FIELDS:
                    if field not in document:
                        continue
                    # The baseline moves whether or not memory did: this is now
                    # what the database says, so a later save has nothing new to
                    # tell it unless something changes the value locally.
                    baseline[field] = document[field]
                    if getattr(chat, field) != document[field]:
                        setattr(chat, field, document[field])
                        if chat.chat_id not in changed:
                            changed.append(chat.chat_id)
        if changed:
            self._mark_chats_dirty()
        return changed

    def purge_chat_records(self, chat_id: str) -> dict:
        """Delete everything a chat produced, and keep the chat itself.

        `delete_chat` above also removes the ChatConfig, which is right for a
        chat that is gone. This is for one that is still in the sidebar and has
        simply been switched off: the row stays, its history does not.

        Keeping the row is not a detail. A discovered chat now arrives with
        automation ON, so deleting the config here would have it rediscovered on
        the next poll and switched straight back on — unticking a box would turn
        it into a tick.

        Returns what was destroyed, per collection, so the caller can say so
        rather than report a silent success."""
        counts = self.chat_record_counts(chat_id)
        with self._lock:
            self._messages = deque(
                (m for m in self._messages if m.chat_id != chat_id),
                maxlen=constants.JSON_MESSAGE_LIMIT,
            )
            # Rebuilt, not discarded: the key set is the fast duplicate check
            # for every OTHER chat too.
            self._message_keys = {m.message_key for m in self._messages}
            self._webhooks = deque(
                (w for w in self._webhooks if w.chat_id != chat_id),
                maxlen=constants.JSON_WEBHOOK_LIMIT,
            )
            self._outgoing = {k: v for k, v in self._outgoing.items()
                              if v.chat_id != chat_id}
        try:
            self._mongo.messages.delete_many({"chat_id": chat_id})
            self._mongo.outgoing.delete_many({"chat_id": chat_id})
            self._mongo.webhooks.delete_many({"chat_id": chat_id})
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Purging chat %s in MongoDB failed: %s", chat_id, ex)
        self._rebuild_all_sections()
        self._backup.flush(force=True)
        return counts

    def chat_record_counts(self, chat_id: str) -> dict:
        """How much a chat has stored, without touching any of it. The UI asks
        before it offers to delete, so the confirmation can name a number."""
        return {
            "messages": self.message_count(chat_id),
            "webhooks": self._count(self._mongo.webhooks, chat_id, self._webhooks),
            "outgoing": self._count(self._mongo.outgoing, chat_id,
                                    self._outgoing.values()),
        }

    def _count(self, collection, chat_id: str, fallback) -> int:
        """How many records a chat has. MongoDB is the authority; the in-memory
        copy is capped and would undercount, so it is only the fallback."""
        try:
            return int(collection.count_documents({"chat_id": chat_id}))
        except Exception:  # noqa: BLE001
            with self._lock:
                return sum(1 for record in list(fallback) if record.chat_id == chat_id)

    def _mongo_upsert_chat(self, chat: ChatConfig) -> None:
        try:
            self._mongo.chat_configs.update_one(
                {"chat_id": chat.chat_id}, {"$set": self._writable(chat)}, upsert=True
            )
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Saving chat %s to MongoDB failed: %s", chat.chat_name, ex)

    def _writable(self, chat: ChatConfig) -> dict:
        """The document to write, minus config fields this process has nothing
        new to say about.

        Every save wrote the whole chat, so a routine one — a relay poll
        recording its status, an ingest updating a counter — carried a full copy
        of the configuration with it and stamped it over whatever was in the
        database. An edit made anywhere else survived only until the next such
        save, which is roughly three seconds.

        Reloading more often does not fix that; it only narrows the window.
        The rule that does: **do not write back a config field you did not
        change.** A field still equal to what this process last read or wrote is
        one it has no opinion about, so it is left out of the `$set` entirely
        and the database keeps whoever's value is there. A field that differs is
        a genuine local edit and is written.

        No call site had to be classified as config or runtime, which matters:
        there are twenty-odd of them and a misclassification would silently
        drop somebody's change in one direction or the other."""
        document = chat.to_document()
        baseline = self._config_baseline.get(chat.chat_id)
        if baseline is None:
            # First write for this chat — discovery creating it, and it owns
            # the initial configuration.
            self._config_baseline[chat.chat_id] = {
                field: getattr(chat, field) for field in self.CONFIG_FIELDS}
            return document
        for field in self.CONFIG_FIELDS:
            current = getattr(chat, field)
            if current == baseline.get(field):
                document.pop(field, None)     # nothing new to say
            else:
                baseline[field] = current     # a real local edit
        return document

    # -- messages ----------------------------------------------------------

    def has_message(self, message_key: str) -> bool:
        with self._lock:
            return message_key in self._message_keys

    def save_message(self, message: StoredMessage) -> bool:
        """Persist a message. Returns False when it was already stored — the
        in-memory key set is the fast check, MongoDB's unique index is the
        authority."""
        with self._lock:
            if message.message_key in self._message_keys:
                return False
            self._message_keys.add(message.message_key)
            if len(self._messages) == self._messages.maxlen:
                evicted = self._messages[0]
                self._message_keys.discard(evicted.message_key)
            self._messages.append(message)
            chat = self._chats.get(message.chat_id)
            if chat is not None:
                chat.messages_stored += 1
        stored = True
        try:
            self._mongo.messages.insert_one(message.to_document())
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            if type(ex).__name__ == "DuplicateKeyError":
                # The unique index caught what the in-memory set couldn't (an
                # eviction, or a second process). Not an error — just not new.
                stored = False
            else:
                self._mongo.note_failure(ex)
                logger.error("Saving message to MongoDB failed: %s", ex)
        self._mark_messages_dirty()
        return stored

    def update_message(self, message: StoredMessage) -> None:
        """Persist a change to a message that is already stored.

        The in-memory copy is replaced too, not just the MongoDB document. They
        are not always the same object: recovery reads incomplete messages back
        out of MongoDB and gets fresh instances, so without this the ring buffer
        — and therefore the JSON mirror and everything the UI shows — would keep
        reporting the state the message was in before it was resolved."""
        with self._lock:
            for index, existing in enumerate(self._messages):
                if existing.message_key == message.message_key:
                    if existing is not message:
                        self._messages[index] = message
                    break
        try:
            self._mongo.messages.update_one(
                {"message_key": message.message_key}, {"$set": message.to_document()}, upsert=True
            )
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
        self._mark_messages_dirty()

    def incomplete_messages(self) -> list[StoredMessage]:
        """Messages that were mid-flight when the process last stopped.

        Queried from MongoDB (the source of truth) rather than the in-memory
        ring buffer, because a message stuck in an incomplete state is exactly
        the kind that might be older than the buffer's window. The buffer is
        the fallback for when the primary is unreachable — recovering some is
        better than recovering none."""
        try:
            documents = list(self._mongo.messages.find(
                {"status": {"$in": list(MessageStatus.INCOMPLETE)}}
            ).sort("detected_at", 1))
            self._mongo.note_success()
            return [StoredMessage.from_document(strip_object_id(d)) for d in documents]
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Could not query incomplete messages from MongoDB: %s", ex)
            with self._lock:
                return [m for m in self._messages if m.status in MessageStatus.INCOMPLETE]

    def recently_originated(self, chat_id: str, text: str, within_seconds: float = 600.0) -> bool:
        """Did THIS application send this text to this chat a moment ago?

        Everything we send is read back out of WhatsApp by a later poll, as an
        outgoing bubble indistinguishable from one the user typed. Without this
        check each sent message is stored twice — once when sent, once when
        seen — inflating `messages_stored` and putting doubles in the mirror.

        Matched on normalized text within a window rather than on a key,
        because the read-back carries the bubble's clock label and the send
        did not. The trade: a message the user types by hand that happens to
        repeat an automated one within the window is not separately recorded."""
        wanted = " ".join((text or "").split())
        if not wanted:
            return False
        cutoff = utcnow().timestamp() - within_seconds
        with self._lock:
            for message in reversed(self._messages):
                if message.chat_id != chat_id or message.direction != "out":
                    continue
                if not message.origin:
                    continue  # read back from WhatsApp, not one of ours
                if message.detected_at and message.detected_at.timestamp() < cutoff:
                    break  # the deque is oldest-first, so nothing older matches
                if " ".join((message.text or "").split()) == wanted:
                    return True
        return False

    def has_relay_id(self, chat_id: str, external_id: str) -> bool:
        """Has this chat already been sent the relay message with this id?

        Checked against MongoDB rather than the in-memory buffer, because an id
        the endpoint keeps offering may be older than the buffer's window — and
        answering "no" from a short memory would re-send it."""
        if not external_id:
            return False
        try:
            found = self._mongo.messages.find_one(
                {"chat_id": chat_id, "origin": "relay", "external_ref": external_id}
            )
            self._mongo.note_success()
            if found is not None:
                return True
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Relay dedup lookup failed: %s", ex)
        with self._lock:
            return any(m.chat_id == chat_id and m.origin == "relay"
                       and m.external_ref == external_id for m in self._messages)

    def messages_for(self, chat_id: str, limit: int = 200) -> list[StoredMessage]:
        with self._lock:
            rows = [m for m in self._messages if m.chat_id == chat_id]
        return rows[-limit:]

    def message_count(self, chat_id: str) -> int:
        try:
            return int(self._mongo.messages.count_documents({"chat_id": chat_id}))
        except Exception:  # noqa: BLE001
            with self._lock:
                return sum(1 for m in self._messages if m.chat_id == chat_id)

    # -- outgoing queue ----------------------------------------------------

    def enqueue_outgoing(self, message: OutgoingMessage) -> OutgoingMessage:
        """Persist a message BEFORE anything is attempted.

        This is what makes the queue survive a crash: by the time a worker
        touches it, the intent to send is already on disk in both stores. The
        per-chat sequence is assigned here so ordering is decided at enqueue
        time, not by whatever order workers happen to pick things up."""
        with self._lock:
            message.sequence = self._next_sequence(message.chat_id)
            self._outgoing[message.outgoing_id] = message
        try:
            self._mongo.outgoing.insert_one(message.to_document())
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Could not persist an outgoing message: %s", ex)
        self._mark_outgoing_dirty()
        return message

    def _next_sequence(self, chat_id: str) -> int:
        existing = [m.sequence for m in self._outgoing.values() if m.chat_id == chat_id]
        return (max(existing) + 1) if existing else 1

    def update_outgoing(self, message: OutgoingMessage) -> None:
        message.updated_at = utcnow()
        with self._lock:
            self._outgoing[message.outgoing_id] = message
        try:
            self._mongo.outgoing.update_one(
                {"outgoing_id": message.outgoing_id}, {"$set": message.to_document()}, upsert=True)
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
        self._mark_outgoing_dirty()

    def pending_outgoing(self) -> list[OutgoingMessage]:
        """Everything still owed, oldest first within each chat.

        Sorted by (chat sequence, creation) so a chat's messages keep their
        order even though several chats are drained from one queue."""
        with self._lock:
            pending = [m for m in self._outgoing.values()
                       if m.status not in OutgoingStatus.FINAL]
        return sorted(pending, key=lambda m: (m.created_at or utcnow(), m.sequence))

    def backfill_phone_number(self, chat_id: str, phone_number: str) -> int:
        """Stamp a newly-known number onto everything already stored for a chat.

        WhatsApp does not expose a saved contact's number, so a chat can run for
        days before anyone types one in. Without this, its history would be
        split into messages that carry the number and messages that do not —
        the kind of gap that is invisible until someone queries the collection
        and quietly gets half the rows.

        Returns how many stored messages were updated."""
        if not chat_id:
            return 0
        number = (phone_number or "").strip()
        updated = 0
        with self._lock:
            for message in self._messages:
                if message.chat_id == chat_id and message.phone_number != number:
                    message.phone_number = number
                    updated += 1
        if updated:
            try:
                self._mongo.messages.update_many(
                    {"chat_id": chat_id},
                    {"$set": {"phone_number": number}},
                )
                self._mongo.note_success()
            except Exception as ex:  # noqa: BLE001 - the mirror still has it
                self._mongo.note_failure(ex)
                logger.error("Could not backfill phone numbers in MongoDB: %s", ex)
            self._mark_messages_dirty()
        return updated

    def pending_counts(self) -> dict:
        """Per chat, how many messages are still mid-flight.

        Counts an incoming message from the moment it is stored until its
        automation round trip finishes, plus anything still owed from the
        outgoing queue. This is the only number the simplified UI shows, so it
        has to mean something a user can act on: if it is not falling, the
        automation is stuck."""
        counts: dict = {}
        with self._lock:
            for message in self._messages:
                if message.status in MessageStatus.INCOMPLETE:
                    counts[message.chat_id] = counts.get(message.chat_id, 0) + 1
            for outgoing in self._outgoing.values():
                if outgoing.status not in OutgoingStatus.FINAL:
                    counts[outgoing.chat_id] = counts.get(outgoing.chat_id, 0) + 1
        return counts

    def all_outgoing(self) -> list[OutgoingMessage]:
        """Every queued message, finished or not — what a status lookup needs,
        since the interesting answers (delivered, failed, unverified) are all
        terminal states that `pending_outgoing` deliberately excludes."""
        with self._lock:
            return list(self._outgoing.values())

    def outgoing_in_state(self, states) -> list[OutgoingMessage]:
        with self._lock:
            return [m for m in self._outgoing.values() if m.status in states]

    def needs_review(self) -> list:
        """Messages that left the compose box but were never found in the chat.

        A first-class state, not an error bucket. The system deliberately does
        not guess about these — retrying risks a duplicate — so they are
        surfaced for a person to resolve. An operator who never looks at this
        number is the one failure mode the design cannot cover."""
        return self.outgoing_in_state((OutgoingStatus.UNVERIFIED,))

    def queue_depth(self) -> int:
        with self._lock:
            return sum(1 for m in self._outgoing.values()
                       if m.status not in OutgoingStatus.FINAL)

    def cancel_outgoing_for_chat(self, chat_id: str) -> int:
        """A deleted chat cannot be sent to. Cancelled, not silently dropped."""
        cancelled = 0
        for message in self.outgoing_in_state(
                OutgoingStatus.RESUMABLE + OutgoingStatus.AMBIGUOUS):
            if message.chat_id == chat_id:
                message.status = OutgoingStatus.CANCELLED
                message.error = "the chat was deleted while this was queued"
                self.update_outgoing(message)
                cancelled += 1
        return cancelled

    def _mark_outgoing_dirty(self) -> None:
        with self._lock:
            payload = [m.to_document() for m in self._outgoing.values()]
        self._backup.set_section(constants.JSON_OUTGOING, payload)

    # -- webhooks ----------------------------------------------------------

    def save_webhook(self, record: WebhookRecord) -> None:
        with self._lock:
            self._webhooks.append(record)
        try:
            self._mongo.webhooks.insert_one(record.to_document())
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
            logger.error("Saving webhook record to MongoDB failed: %s", ex)
        self._mark_webhooks_dirty()

    def webhooks_for(self, chat_id: str, limit: int = 50) -> list[WebhookRecord]:
        with self._lock:
            rows = [w for w in self._webhooks if w.chat_id == chat_id]
        return rows[-limit:]

    # -- logs --------------------------------------------------------------

    def log(self, level: str, event: str, chat_id: str = "", chat_name: str = "",
            message: str = "", direction: str = "", webhook_url: str = "",
            response: str = "", retry_count: int = 0, error: str = "",
            correlation_id: str = "") -> AutomationLog:
        entry = AutomationLog(
            level=level.upper(), event=event, chat_id=chat_id, chat_name=chat_name,
            message=message, direction=direction, correlation_id=correlation_id,
            webhook_url=webhook_url,
            response=(response or "")[:500], retry_count=retry_count, error=(error or "")[:500],
        )
        with self._lock:
            self._logs.append(entry)
        logger.log(getattr(logging, entry.level, logging.INFO), "%s %s", event, message)
        # LOCAL ONLY. This used to also insert into MongoDB, which made every
        # log line a billable write and stored it three times over: the ring
        # buffer above feeds logs.json, and the line before this one writes
        # wadam.log. A diagnostic trail is worth keeping and is not worth
        # paying a cloud provider to keep for you.
        self._mark_logs_dirty()
        self._mark_logs_dirty()
        return entry

    def trace(self, correlation_id: str) -> list[AutomationLog]:
        """Every log line about one message, oldest first.

        The thing an operator actually wants when a reply did not arrive: the
        whole story of that one message, not everything that happened to the
        chat around it."""
        if not correlation_id:
            return []
        with self._lock:
            return [e for e in self._logs if e.correlation_id == correlation_id]

    def recent_logs(self, limit: int = 200, chat_id: str = "") -> list[AutomationLog]:
        with self._lock:
            rows = [entry for entry in self._logs if not chat_id or entry.chat_id == chat_id]
        return rows[-limit:]

    # -- singletons --------------------------------------------------------

    @property
    def app_state(self) -> ApplicationState:
        return self._app_state

    @property
    def poll_state(self) -> PollState:
        return self._poll_state

    def save_app_state(self, state: ApplicationState) -> None:
        state.updated_at = utcnow()
        self._app_state = state
        try:
            self._mongo.application_state.update_one(
                {"_id": constants.SINGLETON_ID}, {"$set": state.to_document()}, upsert=True
            )
            self._mongo.note_success()
        except Exception as ex:  # noqa: BLE001
            self._mongo.note_failure(ex)
        self._mark_state_dirty()

    def save_poll_state(self, state: PollState) -> None:
        """Cycle counters and timings. LOCAL ONLY.

        Written on a timer, read by nothing but the status bar, and meaningless
        after a restart — a MongoDB write for it was ~86,000 billable
        operations a month to remember how many times a loop had run."""
        state.updated_at = utcnow()
        self._poll_state = state
        self._mark_state_dirty()

    # -- JSON mirror -------------------------------------------------------

    def _mark_chats_dirty(self) -> None:
        with self._lock:
            chats = [c.to_document() for c in self._chats.values()]
            automation = {
                "global_automation_enabled": self._app_state.global_automation_enabled,
                "updated_at": utcnow(),
                "chats": [
                    {
                        "chat_id": c.chat_id,
                        "chat_name": c.chat_name,
                        "automation_enabled": c.automation_enabled,
                        "webhook_url": c.webhook_url,
                        "last_poll_utc": c.last_poll_utc,
                        "webhook_retry_count": c.webhook_retry_count,
                        "messages_stored": c.messages_stored,
                    }
                    for c in self._chats.values()
                ],
            }
            webhook_config = [
                {"chat_id": c.chat_id, "chat_name": c.chat_name, "webhook_url": c.webhook_url,
                 "enabled": c.automation_enabled}
                for c in self._chats.values()
            ]
        self._backup.set_section(constants.JSON_CHATS, chats)
        self._backup.set_section(constants.JSON_AUTOMATION, automation)
        # webhooks.json carries BOTH halves of "webhooks": the per-chat
        # configuration and the call history. Configuration first — it's the
        # part a human opens the file to read.
        self._backup.set_section(constants.JSON_WEBHOOKS, {
            "configurations": webhook_config,
            "calls": [w.to_document() for w in self._webhooks],
        })

    def _mark_messages_dirty(self) -> None:
        with self._lock:
            payload = [m.to_document() for m in self._messages]
        self._backup.set_section(constants.JSON_MESSAGES, payload)

    def _mark_webhooks_dirty(self) -> None:
        self._mark_chats_dirty()

    def _mark_logs_dirty(self) -> None:
        with self._lock:
            payload = [entry.to_document() for entry in self._logs]
        self._backup.set_section(constants.JSON_LOGS, payload)

    def _mark_state_dirty(self) -> None:
        self._backup.set_section(constants.JSON_APP_STATE, {
            "application_state": self._app_state.to_document(),
            "poll_state": self._poll_state.to_document(),
        })

    def _rebuild_all_sections(self) -> None:
        self._mark_chats_dirty()
        self._mark_outgoing_dirty()
        self._mark_messages_dirty()
        self._mark_logs_dirty()
        self._mark_state_dirty()

    def flush_json(self, force: bool = False) -> None:
        self._backup.flush(force=force)

    # -- export ------------------------------------------------------------

    def export_chat(self, chat_id: str, path: Path) -> Path:
        """Write one chat's configuration, messages and webhook history to a
        standalone JSON file through the same atomic save."""
        chat = self.get_chat(chat_id)
        payload: dict[str, Any] = {
            "exported_at": utcnow(),
            "application": f"{constants.APP_NAME} {constants.APP_VERSION}",
            "chat": chat.to_document() if chat else None,
            "messages": [m.to_document() for m in self.messages_for(chat_id, limit=100000)],
            "webhooks": [w.to_document() for w in self.webhooks_for(chat_id, limit=100000)],
        }
        self._backup.export_document(path, payload)
        self.log("INFO", "chat.exported", chat_id=chat_id,
                 chat_name=chat.chat_name if chat else "", message=f"Exported to {path}")
        return path

    # -- status ------------------------------------------------------------

    def status(self) -> dict[str, str]:
        return {
            "mongodb": self._mongo.status_text,
            "json": self._backup.status_text,
            "mongodb_ok": "yes" if self._mongo.connected else "no",
            "json_ok": "yes" if self._backup.healthy else "no",
        }
