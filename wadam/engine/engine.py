"""The automation engine — one polling loop, one worker, no user intervention.

    ┌─ poll loop, every 3 s ──────────────────────────────────────────┐
    │  find window → read chat list → discover/sync → detect change   │
    │  → read the open conversation (when it's worth reading)         │
    │  → enqueue work                                                 │
    └────────────────────────────┬────────────────────────────────────┘
                                 │ queue
    ┌────────────────────────────▼────────────────────────────────────┐
    │  worker: open chat → read messages → persist → pipeline         │
    └─────────────────────────────────────────────────────────────────┘

**Why two of them.** Opening a chat, calling a webhook and sending a reply take
seconds and involve real foreground changes. If the poll did that work it would
no longer be a three-second poll. So the loop only does cheap accessibility
reads and hands anything expensive to a single worker, which runs jobs one at a
time — a second concurrent send is exactly how a message ends up in the wrong
conversation.

**What gets opened.** Only chats with automation enabled are ever opened;
everything else is tracked from the sidebar row alone, which costs nothing and
disturbs nothing. Switching chats is visible to the user, so the application
does it only when it has a reason.

**The global ON/OFF is a bulk action, not a master switch.** Pressing it writes
`automation_enabled` to every chat. Afterwards each chat can be toggled
individually and that individual choice stands — which is what "each chat can
still override individually" requires. A master gate would silently veto a chat
switched on after a global OFF.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from wadam import constants
from wadam.config import Settings
from wadam.domain.models import (
    AutomationLog,
    ChatConfig,
    MessageStatus,
    OutgoingStatus,
    PollState,
    StoredMessage,
    message_key_for,
    outgoing_key_for,
    phone_digits,
    utcnow,
)
from wadam.domain.webhook_url import webhook_url_for
from wadam.engine.delivery import DeliveryService
from wadam.engine.discovery import ChatDiscovery
from wadam.engine.pipeline import MessagePipeline
from wadam.engine.metrics import Metrics, MetricsSnapshot
from wadam.engine.relay import RelayService
from wadam.engine.webhook import RelayMessage, WebhookClient, WebhookOutcome
from wadam.storage.repository import Repository
from wadam.whatsapp.reader import WhatsAppMessage, WhatsAppReader
from wadam.whatsapp import capabilities as win_caps
from wadam.whatsapp import session as win_session
from wadam.whatsapp import sender
from wadam.whatsapp.sender import WhatsAppSender
from wadam.whatsapp.sta_thread import StaAutomationThread
from wadam.whatsapp.verifier import SendVerifier

logger = logging.getLogger(__name__)


@dataclass
class EngineSnapshot:
    """Everything the UI renders, captured at one instant. The UI never reaches
    into the engine or the database — it redraws from these."""

    chats: list[ChatConfig] = field(default_factory=list)
    logs: list[AutomationLog] = field(default_factory=list)
    global_automation: bool = False
    whatsapp_found: bool = False
    cycle_count: int = 0
    last_cycle_ms: int = 0
    last_cycle_utc: Optional[Any] = None
    queued_jobs: int = 0
    busy_with: str = ""
    mongo_status: str = ""
    mongo_ok: bool = False
    json_status: str = ""
    json_ok: bool = False
    last_error: str = ""
    # Windows session / desktop / UIA preconditions, refreshed each cycle.
    session_rows: list = field(default_factory=list)   # (label, value, health)
    send_blocked_reason: str = ""
    metrics: Optional[MetricsSnapshot] = None
    queue_depth: int = 0
    capability_summary: str = ""


@dataclass(frozen=True)
class SendOutcome:
    """The result of an explicitly requested send (as opposed to an automated
    reply, whose outcome is recorded on the message itself)."""

    ok: bool
    strategy: str = ""
    message_key: str = ""
    error: str = ""
    outgoing_id: str = ""
    queued: bool = False


@dataclass
class _Job:
    # "drain" carries no work of its own: the worker drains the outgoing queue
    # after every job, so queueing one is how an API send wakes the drainer
    # without waiting for the next poll tick.
    kind: str          # "scan" | "process" | "resume" | "relay" | "drain"
    chat_id: str
    message: Optional[StoredMessage] = None
    relay: Optional[RelayMessage] = None


class AutomationEngine:
    def __init__(self, settings: Settings, repository: Repository,
                 on_snapshot: Callable[[EngineSnapshot], None]) -> None:
        self._settings = settings
        self._repo = repository
        self._on_snapshot = on_snapshot

        self._sta = StaAutomationThread()
        self._reader = WhatsAppReader(self._sta, settings.whatsapp_window_title)
        self.metrics = Metrics()
        self._sender = WhatsAppSender(self._reader, self._sta,
                                      use_clipboard=settings.sender_use_clipboard,
                                      metrics=self.metrics)
        self._webhook = WebhookClient(
            api_key=settings.webhook_api_key,
            timeout=settings.webhook_timeout,
            max_retries=settings.webhook_max_retries,
        )
        self._discovery = ChatDiscovery(repository, settings)
        # Verification reads the conversation back; it goes through the same
        # opener the worker uses so it sees exactly what WhatsApp is showing.
        self._verifier = SendVerifier(self._read_for_verification)
        self._delivery = DeliveryService(repository, self._sender, self._verifier,
                                         asyncio.to_thread, self.metrics)
        self._pipeline = MessagePipeline(repository, self._webhook, self._sender,
                                         asyncio.to_thread, self._delivery, self.metrics)
        self._relay = RelayService(repository, self._webhook, asyncio.to_thread,
                                   self._delivery)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = concurrent.futures.Future()
        self._stop = asyncio.Event()
        self._queue: "asyncio.Queue[_Job]" = asyncio.Queue()
        self._queued_chats: set[str] = set()
        self._hwnd: Optional[int] = None
        self._busy_with = ""
        self._draining = False
        self._last_error = ""
        self._active_chat_name = ""
        # When each chat's webhook was last GETted, by chat id (monotonic).
        self._relay_polled_at: dict[str, float] = {}
        self._session_state = win_session.probe(settings.whatsapp_window_title)
        self._capability_summary = "not probed yet"
        self._capabilities = None
        self._capability_store = win_caps.CapabilityStore(
            settings.json_backup_folder / "capabilities.json")
        # Session changes arrive as events; the polled probe stays as the
        # authority, this just stops it being up to a cycle out of date.
        self._session_watcher = win_session.SessionWatcher(self._on_session_change)

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """The engine's whole life. Runs on its own event loop, on its own
        thread; the UI never calls into it except through `submit`."""
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        if not self._ready.done():
            self._ready.set_result(True)

        self._session_watcher.start()
        worker = asyncio.create_task(self._worker(), name="wadam-worker")
        # The relay is its own task, not part of the cycle: it is network I/O
        # against someone else's server, and a slow endpoint must not be able to
        # stretch a three-second poll.
        relay = (asyncio.create_task(self._relay_loop(), name="wadam-relay")
                 if self._settings.relay_enabled else None)
        try:
            await self._recover_incomplete()
            await self._startup_scan()
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    await self._cycle()
                    self._last_error = ""
                except Exception as ex:  # noqa: BLE001 - a cycle must never kill the loop
                    self._last_error = f"{type(ex).__name__}: {ex}"
                    logger.exception("Polling cycle failed")
                    self._repo.log("ERROR", "poll.failed", message=self._last_error)
                elapsed = time.monotonic() - started
                # Bookkeeping and the UI snapshot are guarded SEPARATELY from
                # the cycle itself. A failure in telemetry has no business
                # stopping the automation — and that is not hypothetical: a
                # method called on the wrong object in the once-every-200-cycles
                # log pruning took the whole engine down ten minutes into a run,
                # from outside the guard above.
                try:
                    await self._record_cycle(int(elapsed * 1000))
                    self.publish()
                except Exception:  # noqa: BLE001
                    logger.exception("Cycle bookkeeping failed")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=max(0.0, constants.POLL_INTERVAL_SECONDS - elapsed),
                    )
                except asyncio.TimeoutError:
                    pass  # the normal path — the interval elapsed
        finally:
            for task in (worker, relay):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self._session_watcher.stop()
            self._sta.dispose()

    def _on_session_change(self, event_name: str, resumes: bool) -> None:
        """Called from the watcher's message pump — a different thread.

        Only touches plain attributes and re-probes, both of which are safe
        off-loop; anything needing the event loop is scheduled onto it."""
        self._session_state = win_session.probe(self._settings.whatsapp_window_title)
        self._repo.log("INFO" if resumes else "WARNING", "session.changed",
                       message=f"Windows session event: {event_name} — sending "
                               f"{'may resume' if resumes else 'is held'}.")
        if not resumes:
            self.metrics.record_session_hold()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.publish)

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        try:
            return bool(self._ready.result(timeout=timeout))
        except Exception:  # noqa: BLE001
            return False

    def request_stop(self) -> None:
        """Callable from any thread."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)

    # -- polling -----------------------------------------------------------

    async def _recover_incomplete(self) -> None:
        """Pick up work that was in flight when the process last stopped.

        This is what "persistent queues survive restarts" means here: the queue
        is not a separate durable structure, it is reconstructed from the state
        each message was persisted in. What happens to each one is decided by
        what the outside world has already seen, never by optimism —
        see `MessageStatus`.

        * `PENDING` — stored, webhook provably not called. Safe to process.
        * `AWAITING_SEND` — a reply exists but the send is unconfirmed. Queued
          for a *verified* resume: read the chat first, send only if the reply
          isn't already there.
        * `DISPATCHING` — the webhook call was in flight. It may already have
          been delivered and may already have caused a side effect at the other
          end, so it is marked `INTERRUPTED` and left for a person. This is
          deliberately the least convenient option: a duplicate webhook call is
          worse than a missed reply.
        """
        try:
            incomplete = await asyncio.to_thread(self._repo.incomplete_messages)
        except Exception as ex:  # noqa: BLE001
            logger.exception("Could not load incomplete work")
            self._repo.log("ERROR", "recovery.failed", message=str(ex))
            return
        if not incomplete:
            return

        resumed = interrupted = 0
        for message in incomplete:
            chat = self._repo.get_chat(message.chat_id)
            if chat is None:
                continue  # its chat was deleted while we were down
            if message.status == MessageStatus.PENDING:
                self._enqueue(_Job("process", chat.chat_id, message))
                resumed += 1
            elif message.status == MessageStatus.AWAITING_SEND and message.reply_text:
                self._enqueue(_Job("resume", chat.chat_id, message))
                resumed += 1
            elif message.status == MessageStatus.DISPATCHING:
                message.status = MessageStatus.INTERRUPTED
                message.error = ("The application stopped while this message's webhook call was "
                                 "in flight. It was not retried, because the endpoint may already "
                                 "have received it.")
                await asyncio.to_thread(self._repo.update_message, message)
                self._repo.log(
                    "WARNING", "recovery.interrupted", chat_id=chat.chat_id,
                    chat_name=chat.chat_name, direction=message.direction,
                    webhook_url=chat.webhook_url, error=message.error,
                    message=f"Webhook call was in flight at shutdown for: {message.text[:80]}",
                )
                interrupted += 1

        # Outgoing messages that were mid-flight when the process stopped are
        # verified against the chat, never re-sent blind.
        for queued in await asyncio.to_thread(
                self._repo.outgoing_in_state, OutgoingStatus.AMBIGUOUS):
            try:
                await self._delivery.resume_ambiguous(queued)
            except Exception:  # noqa: BLE001
                logger.exception("Could not resume an in-flight outgoing message")

        await asyncio.to_thread(self._repo.flush_json, True)
        self._repo.log("INFO", "recovery.complete",
                       message=f"Restored {resumed} in-flight message(s); "
                               f"{interrupted} left interrupted (see warnings above).")

    async def _startup_scan(self) -> None:
        """One deep read at startup so the sidebar is populated beyond the
        handful of chats WhatsApp has realized on screen. The ordinary poll
        stays shallow — this is the only place that scrolls the user's list."""
        try:
            hwnd = await self._reader.find_window_async()
            if hwnd is None:
                self._repo.log("WARNING", "whatsapp.missing",
                               message="WhatsApp Desktop is not running — waiting for it.")
                return
            self._hwnd = hwnd
            # What this WhatsApp build can actually be driven with. Cached
            # against the package version, so an update re-probes by itself.
            try:
                self._capabilities = await asyncio.to_thread(
                    self._capability_store.refresh_if_needed, hwnd)
                self._capability_summary = self._capabilities.summary()
                # Tell the sender what the probe found. Without this the probe
                # was written to disk and never read, and every send retried a
                # write path already known to be discarded by this build.
                sender.set_value_pattern_ruled_out(
                    not self._capabilities.value_pattern_write)
                self._repo.log("INFO", "capabilities.probed",
                               message=self._capability_summary)
            except Exception as ex:  # noqa: BLE001 - never block startup on a probe
                logger.warning("capability probe failed: %s", ex)
                self._capability_summary = f"probe failed: {ex}"
            rows = await self._reader.read_chat_rows_deep_async(hwnd)
            result = await asyncio.to_thread(self._discovery.sync, rows)
            self._repo.log("INFO", "startup.scan",
                           message=f"Startup scan found {len(result.seen)} chats "
                                   f"({len(result.new)} new).")
            await asyncio.to_thread(self._repo.flush_json, True)
        except Exception as ex:  # noqa: BLE001
            logger.exception("Startup scan failed")
            self._repo.log("ERROR", "startup.scan_failed", message=str(ex))
        self.publish()

    async def _cycle(self) -> None:
        if self._draining:
            # A drain is sending a backlog right now. This cycle's conversation
            # read costs ~2s on the same STA thread, so running it here does not
            # just delay the poll — it delays every message in the batch behind
            # it. Measured: it tripled the cost of a send inside a batch, from
            # ~0.8s to ~4.6s for a single name read. Nothing is missed; the next
            # cycle picks it up as soon as the queue is empty.
            return
        hwnd = self._hwnd
        if hwnd is None or not await self._reader.window_is_alive_async(hwnd):
            previous = self._hwnd
            hwnd = await self._reader.find_window_async()
            if hwnd is not None and previous is not None and hwnd != previous:
                self.metrics.record_reconnect()
                self._repo.log("INFO", "whatsapp.reconnected",
                               message=f"WhatsApp window changed {previous} -> {hwnd}")
            self._hwnd = hwnd
        if hwnd is None:
            return

        rows = await self._reader.read_chat_rows_async(hwnd)
        if not rows:
            return
        result = await asyncio.to_thread(self._discovery.sync, rows)
        changed_ids = {c.chat_id for c in result.changed}

        # The open conversation is the one chat we can read without switching
        # anything. Its NAME is a cheap read; walking its message bubbles is
        # not, so that only happens when the chat is automated or something in
        # it visibly changed.
        active_name = await self._reader.get_active_conversation_name_async(hwnd)
        self._active_chat_name = active_name or ""
        if active_name:
            active = self._find_chat_by_name(active_name)
            if active is not None and (active.automation_enabled or active.chat_id in changed_ids):
                read_started = time.monotonic()
                messages = await self._reader.read_recent_messages_async(
                    hwnd, constants.MESSAGE_READ_LIMIT
                )
                self.metrics.record_read(len(messages),
                                         (time.monotonic() - read_started) * 1000)
                pending = await self._ingest(active, messages)
                for message in pending:
                    self._enqueue(_Job("process", active.chat_id, message))

        # Automated chats that changed and aren't already on screen need to be
        # opened to be read — that's the expensive path, so it goes to the
        # worker and is deduplicated by chat.
        for chat in result.changed:
            if not chat.automation_enabled:
                continue
            if active_name and chat.chat_name == active_name:
                continue
            self._enqueue(_Job("scan", chat.chat_id))

    async def _record_cycle(self, elapsed_ms: int) -> None:
        state = self._repo.poll_state or PollState()
        state.cycle_count += 1
        state.last_cycle_utc = utcnow()
        state.last_cycle_ms = elapsed_ms
        state.whatsapp_found = self._hwnd is not None
        state.chats_seen = len(self._repo.list_chats())
        state.queued_chats = self._queue.qsize()
        state.last_error = self._last_error
        # Cheap enough to refresh every cycle, and it is how a disconnected RDP
        # session or a locked desktop becomes visible instead of mysterious.
        self._session_state = win_session.probe(self._settings.whatsapp_window_title)
        # Written to MongoDB on the first cycle (so the collection exists and a
        # short run still leaves a trace) and every ten cycles after that, not
        # every three seconds: it is telemetry, and the in-memory copy the UI
        # reads is always current regardless.
        if state.cycle_count == 1 or state.cycle_count % 10 == 0:
            await asyncio.to_thread(self._repo.save_poll_state, state)
        if state.cycle_count % 200 == 0:
            await asyncio.to_thread(self._repo.prune_logs)

    def _find_chat_by_name(self, name: str) -> Optional[ChatConfig]:
        from wadam.domain.models import chat_id_for
        from wadam.whatsapp.name_rules import chat_names_match

        chat = self._repo.get_chat(chat_id_for(name))
        if chat is not None:
            return chat
        # The compose box can render a slightly different form of the name than
        # the sidebar row does (truncation, a trailing status). Fall back to the
        # truncation-tolerant comparison rather than treating it as a new chat.
        for candidate in self._repo.list_chats():
            if chat_names_match(name, candidate.chat_name):
                return candidate
        return None

    def _enqueue(self, job: _Job) -> None:
        if job.kind == "scan":
            if job.chat_id in self._queued_chats:
                return
            self._queued_chats.add(job.chat_id)
        self._queue.put_nowait(job)

    # -- worker ------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            chat = self._repo.get_chat(job.chat_id)
            try:
                if chat is None:
                    continue  # deleted while queued
                self._busy_with = chat.chat_name
                self.publish()
                if job.kind == "scan":
                    await self._scan_chat(chat)
                elif job.kind == "process" and job.message is not None:
                    await self._pipeline.process(chat, job.message)
                elif job.kind == "resume" and job.message is not None:
                    await self._resume_send(chat, job.message)
                elif job.kind == "relay" and job.relay is not None:
                    # Re-checked here, not just at queue time: another message
                    # for the same chat may have been delivered while this one
                    # waited, and the endpoint may have offered it twice.
                    send, reason = self._relay.should_send(chat, job.relay)
                    if send:
                        await self._relay.enqueue(chat, job.relay)
                    else:
                        self._relay.note_skipped(chat, job.relay, reason)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - one bad job must not stop the worker
                logger.exception("Job %s for %s failed", job.kind, job.chat_id)
                self._repo.log("ERROR", "job.failed", chat_id=job.chat_id,
                               chat_name=chat.chat_name if chat else "",
                               message=f"{job.kind}: {type(ex).__name__}: {ex}")
            finally:
                if job.kind == "scan":
                    self._queued_chats.discard(job.chat_id)
                self._queue.task_done()
                self._busy_with = ""
                # Anything the job produced is now in the durable queue; drain
                # it here so producing and delivering stay separate concerns.
                try:
                    await self._drain_outgoing()
                except Exception:  # noqa: BLE001
                    logger.exception("Draining the outgoing queue failed")
                self.publish()

    async def _scan_chat(self, chat: ChatConfig) -> None:
        """Open a chat, read it, persist what's new, and run anything that
        needs running. This is the only path that switches the user's open
        conversation."""
        read_started = time.monotonic()
        _hwnd, messages = await self._sender.open_and_read_async(
            chat.chat_name, constants.MESSAGE_READ_LIMIT
        )
        self.metrics.record_read(len(messages), (time.monotonic() - read_started) * 1000)
        if not messages:
            return
        pending = await self._ingest(chat, messages)
        for message in pending:
            await self._pipeline.process(chat, message)

    async def _read_for_verification(self, chat_name: str):
        """Read the open conversation for the verifier.

        Uses the plain reader rather than the opener: by the time verification
        runs, the send has just left this chat open, so no switching is needed
        and none should happen — re-opening would be another interruption for
        no gain."""
        hwnd = self._hwnd
        if hwnd is None:
            return None
        active = await self._reader.get_active_conversation_name_async(hwnd)
        if not active:
            return None
        from wadam.whatsapp.name_rules import chat_names_match

        if active.strip().lower() != chat_name.strip().lower()                 and not chat_names_match(chat_name, active):
            return None          # a different chat is open — cannot verify from here
        return await self._reader.read_recent_messages_async(hwnd, constants.MESSAGE_READ_LIMIT)

    # -- outgoing queue ----------------------------------------------------

    async def _drain_outgoing(self) -> None:
        """Deliver everything the queue owes, oldest first.

        Runs on the worker, so sends stay serialized against chat scans and
        against each other — the ordering guarantee comes from the queue's
        per-chat sequence, and the safety from there being exactly one drainer."""
        pending = [m for m in await asyncio.to_thread(self._repo.pending_outgoing)
                   if m.status not in OutgoingStatus.AMBIGUOUS]  # resolved at startup
        if not pending or self._stop.is_set():
            return

        # The whole backlog goes out as one run: one foreground change, one
        # census read per chat, one verification read per chat. Draining message
        # by message made a burst of twenty take twenty times the setup.
        self._busy_with = pending[0].chat_name
        self._draining = True
        self.publish()
        try:
            await self._delivery.deliver_batch(pending)
        except Exception as ex:  # noqa: BLE001 - one bad batch must not stall the queue
            logger.exception("Delivering the outgoing queue failed")
            self._repo.log("ERROR", "outgoing.error", chat_id=pending[0].chat_id,
                           chat_name=pending[0].chat_name, error=str(ex),
                           message="Unexpected failure while delivering.")
        finally:
            self._draining = False
            self._busy_with = ""

    # -- relay -------------------------------------------------------------

    async def _relay_loop(self) -> None:
        """GET each automated chat's webhook and queue whatever it offers.

        Fetches run concurrently (bounded) because they are independent network
        calls against someone else's server; the *sends* they produce go to the
        worker, which runs them one at a time like every other send.

        Each chat is polled at most once per `RELAY_POLL_INTERVAL`, tracked per
        chat rather than globally, so adding a chat doesn't slow the others and
        a chat added mid-run starts on its own schedule instead of all of them
        firing on the same tick."""
        semaphore = asyncio.Semaphore(4)

        async def fetch(chat: ChatConfig):
            async with semaphore:
                return chat, await self._relay.poll(chat)

        while not self._stop.is_set():
            try:
                now = time.monotonic()
                due = [
                    chat for chat in self._repo.list_chats()
                    if self._relay.is_eligible(chat)
                    and (now - self._relay_polled_at.get(chat.chat_id, 0.0))
                    >= self._settings.relay_poll_interval
                ]
                if due:
                    for chat in due:
                        self._relay_polled_at[chat.chat_id] = now
                    for chat, poll in await asyncio.gather(*(fetch(c) for c in due)):
                        await self._handle_relay_poll(chat, poll)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad poll must not end the loop
                logger.exception("Relay poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _handle_relay_poll(self, chat: ChatConfig, poll) -> None:
        self.metrics.record_relay_poll(len(poll.messages) if poll.ok else 0)
        if not poll.ok:
            await self._relay.record_poll(chat, poll)
            self._repo.log("WARNING", "relay.poll_failed", chat_id=chat.chat_id,
                           chat_name=chat.chat_name, webhook_url=chat.webhook_url,
                           error=poll.error, message="Relay poll failed.")
            return
        if not poll.messages:
            await self._relay.record_poll(chat, poll)
            return
        for message in poll.messages:
            send, reason = self._relay.should_send(chat, message)
            if not send:
                self._relay.note_skipped(chat, message, reason)
                continue
            self._queue.put_nowait(_Job("relay", chat.chat_id, relay=message))

    async def _resume_send(self, chat: ChatConfig, message: StoredMessage) -> None:
        """Finish a reply whose send was never confirmed, without risking a
        duplicate.

        The conversation is read first and the reply text looked for among what
        we sent. Only its absence justifies sending it again — "I don't know"
        is answered by waiting, not by sending."""
        _hwnd, messages = await self._sender.open_and_read_async(
            chat.chat_name, constants.MESSAGE_READ_LIMIT
        )
        if not messages:
            await self._pipeline.resume_send(chat, message, already_sent=None)
            return
        wanted = " ".join((message.reply_text or "").split())
        already = any(
            not m.is_incoming and " ".join((m.text or "").split()) == wanted
            for m in messages
        )
        # The conversation was read, so the messages in it are worth keeping
        # regardless of the outcome.
        await self._ingest(chat, messages)
        await self._pipeline.resume_send(chat, message, already_sent=already)

    # -- ingestion ---------------------------------------------------------

    async def _ingest(self, chat: ChatConfig, messages: list[WhatsAppMessage]) -> list[StoredMessage]:
        """Persist every message we haven't seen, and return the incoming ones
        that should go through the pipeline.

        The first read of a chat records its whole visible backlog as `seeded`
        and returns nothing: those messages existed before this application was
        watching, and answering them would be wrong."""
        seeding = not chat.seeded
        pending: list[StoredMessage] = []
        changed = False

        for message in messages:
            direction = "in" if message.is_incoming else "out"
            key = message_key_for(chat.chat_id, message.sender, message.text,
                                  message.time_text, direction)
            if self._repo.has_message(key):
                continue
            if not message.is_incoming and self._repo.recently_originated(chat.chat_id, message.text):
                # Our own message, read back out of WhatsApp. It is already
                # stored from when we sent it; storing the bubble too would
                # double every automated reply in the record.
                continue

            if seeding:
                status = MessageStatus.SEEDED
            elif message.is_incoming and chat.automation_enabled:
                status = MessageStatus.PENDING
            else:
                status = MessageStatus.IGNORED

            stored = StoredMessage(
                message_key=key,
                chat_id=chat.chat_id,
                chat_name=chat.chat_name,
            phone_number=chat.phone_number,
                sender=message.sender or ("You" if not message.is_incoming else chat.chat_name),
                text=message.text,
                direction=direction,
                media_kind=message.media_kind,
                media_note=message.media_note,
                time_text=message.time_text,
                status=status,
            )
            if not await asyncio.to_thread(self._repo.save_message, stored):
                continue  # a duplicate the unique index caught

            changed = True
            if message.is_incoming:
                chat.last_incoming_text = message.text
                chat.last_incoming_sender = stored.sender
                chat.last_incoming_utc = stored.detected_at
            else:
                chat.last_outgoing_text = message.text
                chat.last_outgoing_utc = stored.detected_at
            if status == MessageStatus.PENDING:
                pending.append(stored)

        if seeding:
            chat.seeded = True
            changed = True
            self._repo.log("INFO", "chat.seeded", chat_id=chat.chat_id, chat_name=chat.chat_name,
                           message=f"Baseline recorded ({len(messages)} existing messages stored, "
                                   f"none automated).")
        if changed:
            await asyncio.to_thread(self._repo.save_chat, chat)
            await asyncio.to_thread(self._repo.flush_json, True)
        return pending

    # -- snapshots ---------------------------------------------------------

    def publish(self) -> None:
        status = self._repo.status()
        state = self._repo.poll_state
        chats = self._repo.list_chats()
        # Derived every time rather than stored: a pending count that survived a
        # restart while the work behind it did not would be a lie on screen.
        pending = self._repo.pending_counts()
        for chat in chats:
            chat.pending_count = pending.get(chat.chat_id, 0)
        snapshot = EngineSnapshot(
            chats=chats,
            logs=self._repo.recent_logs(limit=250),
            global_automation=self._repo.app_state.global_automation_enabled,
            whatsapp_found=self._hwnd is not None,
            cycle_count=state.cycle_count if state else 0,
            last_cycle_ms=state.last_cycle_ms if state else 0,
            last_cycle_utc=state.last_cycle_utc if state else None,
            queued_jobs=self._queue.qsize(),
            busy_with=self._busy_with,
            mongo_status=status.get("mongodb", ""),
            mongo_ok=status.get("mongodb_ok") == "yes",
            json_status=status.get("json", ""),
            json_ok=status.get("json_ok") == "yes",
            last_error=self._last_error,
            session_rows=list(self._session_state.summary()),
            send_blocked_reason=self._session_state.send_blocked_reason,
            metrics=self.metrics.snapshot(self._repo.queue_depth(),
                                          len(self._repo.needs_review())),
            queue_depth=self._repo.queue_depth(),
            capability_summary=self._capability_summary,
        )
        try:
            self._on_snapshot(snapshot)
        except Exception:  # noqa: BLE001 - a UI problem must not stop the engine
            logger.exception("Snapshot delivery failed")

    # -- commands (called from the UI thread) ------------------------------

    def submit(self, coroutine_factory: Callable[[], Any]) -> "concurrent.futures.Future":
        """Run a coroutine on the engine's loop from another thread. Returns a
        concurrent Future the caller may ignore or wait on."""
        if self._loop is None or not self._loop.is_running():
            future: "concurrent.futures.Future" = concurrent.futures.Future()
            future.set_exception(RuntimeError("The engine is not running."))
            return future
        return asyncio.run_coroutine_threadsafe(coroutine_factory(), self._loop)

    async def set_chat_phone_number(self, chat_id: str, phone_number: str) -> None:
        """Record a chat's number and rebuild its webhook URL from the template.

        Typed in rather than read, because WhatsApp shows a saved contact by
        name and exposes the number nowhere an accessibility client can see it.
        Clearing the field clears the URL too — a chat with no number must have
        no webhook, never a URL with an empty number in it."""
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return
        digits = phone_digits(phone_number)
        chat.phone_number = digits
        chat.webhook_url = webhook_url_for(self._settings.webhook_template,
                                           digits, chat.webhook_override,
                                           chat.chat_name)
        await asyncio.to_thread(self._repo.save_chat, chat)
        # Everything already stored for this chat was recorded before the number
        # was known. Backfilling means one chat has one number across its whole
        # history, rather than a silent split between "before we knew" and
        # "after" that anyone querying the data would have to know about.
        filled = await asyncio.to_thread(self._repo.backfill_phone_number,
                                         chat_id, digits)
        self._repo.log("INFO", "chat.number_set", chat_id=chat_id, chat_name=chat.chat_name,
                       webhook_url=chat.webhook_url,
                       message=f"Number set to {digits or '(cleared)'}"
                               + (f"; backfilled {filled} stored message(s)." if filled
                                  else "."))
        await asyncio.to_thread(self._repo.flush_json, True)
        self.publish()

    async def set_chat_webhook(self, chat_id: str, url: str) -> None:
        """Point one chat somewhere other than the template says.

        Stored as an OVERRIDE, not as the URL itself: `webhook_url` is derived
        and gets rebuilt from the template on every discovery pass, so writing
        the edit there would survive until the next poll and then silently
        revert. Clearing the box removes the override and the chat goes back to
        following the template."""
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return
        wanted = (url or "").strip()
        generated = webhook_url_for(self._settings.webhook_template,
                                    chat.phone_number, "", chat.chat_name)
        # Typing the generated URL back in is not an override, it is agreement.
        chat.webhook_override = "" if wanted == generated else wanted
        chat.webhook_url = webhook_url_for(self._settings.webhook_template,
                                           chat.phone_number,
                                           chat.webhook_override, chat.chat_name)
        await asyncio.to_thread(self._repo.save_chat, chat)
        self._repo.log("INFO", "webhook.set", chat_id=chat_id, chat_name=chat.chat_name,
                       webhook_url=chat.webhook_url,
                       message=("Webhook set to " + chat.webhook_url) if chat.webhook_override
                               else "Webhook back to the default for this chat.")
        await asyncio.to_thread(self._repo.flush_json, True)
        self.publish()

    async def set_chat_automation(self, chat_id: str, enabled: bool) -> None:
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return
        chat.automation_enabled = enabled
        await asyncio.to_thread(self._repo.save_chat, chat)
        self._repo.log("INFO", "automation.toggled", chat_id=chat_id, chat_name=chat.chat_name,
                       message=f"Automation turned {'ON' if enabled else 'OFF'}.")
        if enabled:
            # Read it once now, so the baseline is current rather than whatever
            # was on screen the last time anyone looked.
            self._enqueue(_Job("scan", chat_id))
        await asyncio.to_thread(self._repo.flush_json, True)
        self.publish()

    async def set_webhook(self, chat_id: str, url: str) -> None:
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return
        chat.webhook_url = (url or "").strip()
        await asyncio.to_thread(self._repo.save_chat, chat)
        self._repo.log("INFO", "webhook.configured", chat_id=chat_id, chat_name=chat.chat_name,
                       message=f"Webhook set to {chat.webhook_url or '(empty)'}")
        await asyncio.to_thread(self._repo.flush_json, True)
        self.publish()

    async def set_global_automation(self, enabled: bool) -> None:
        """The top-right ON/OFF: a bulk write across every known chat."""
        chats = self._repo.list_chats()
        for chat in chats:
            chat.automation_enabled = enabled
        await asyncio.to_thread(self._repo.save_chats, chats)
        state = self._repo.app_state
        state.global_automation_enabled = enabled
        await asyncio.to_thread(self._repo.save_app_state, state)
        self._repo.log("INFO", "automation.global",
                       message=f"Automation turned {'ON' if enabled else 'OFF'} for "
                               f"{len(chats)} chat(s).")
        await asyncio.to_thread(self._repo.flush_json, True)
        self.publish()

    async def reset_automation(self, chat_id: str) -> None:
        """Put a chat back to how a freshly discovered one looks — automation
        off, counters and status cleared, backlog re-baselined — while keeping
        its webhook URL and its stored history."""
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return
        chat.automation_enabled = False
        chat.seeded = False
        chat.webhook_retry_count = 0
        chat.last_webhook_status = ""
        chat.last_webhook_response = ""
        chat.last_webhook_utc = None
        chat.last_error = ""
        await asyncio.to_thread(self._repo.save_chat, chat)
        self._repo.log("INFO", "automation.reset", chat_id=chat_id, chat_name=chat.chat_name,
                       message="Automation reset — status cleared and the backlog re-baselined.")
        await asyncio.to_thread(self._repo.flush_json, True)
        self.publish()

    async def delete_chat(self, chat_id: str) -> None:
        cancelled = await asyncio.to_thread(self._repo.cancel_outgoing_for_chat, chat_id)
        if cancelled:
            self._repo.log("INFO", "outgoing.cancelled", chat_id=chat_id,
                           message=f"Cancelled {cancelled} queued message(s) for a deleted chat.")
        await asyncio.to_thread(self._repo.delete_chat, chat_id)
        self.publish()

    async def export_chat(self, chat_id: str, path: Path) -> Path:
        result = await asyncio.to_thread(self._repo.export_chat, chat_id, path)
        self.publish()
        return result

    async def test_webhook(self, chat_id: str) -> WebhookOutcome:
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return WebhookOutcome(ok=False, error="Chat not found.")
        outcome = await self._webhook.probe(chat.webhook_url)
        chat.last_webhook_status = f"test: {outcome.status_text}"
        chat.last_webhook_response = (outcome.reply_text or outcome.body or outcome.error)[:1000]
        chat.last_webhook_utc = utcnow()
        await asyncio.to_thread(self._repo.save_chat, chat)
        self._repo.log("INFO" if outcome.ok else "ERROR", "webhook.tested",
                       chat_id=chat_id, chat_name=chat.chat_name, message=outcome.status_text)
        self.publish()
        return outcome

    async def rescan(self) -> int:
        """Deep-read the whole chat list on demand (the same scan startup does).
        Returns how many chats were seen."""
        hwnd = self._hwnd or await self._reader.find_window_async()
        if hwnd is None:
            return 0
        self._hwnd = hwnd
        rows = await self._reader.read_chat_rows_deep_async(hwnd)
        result = await asyncio.to_thread(self._discovery.sync, rows)
        await asyncio.to_thread(self._repo.flush_json, True)
        self._repo.log("INFO", "chats.rescanned",
                       message=f"Rescan found {len(result.seen)} chats ({len(result.new)} new).")
        self.publish()
        return len(result.seen)

    async def queue_message(self, chat_id: str, text: str,
                            origin: str = "api") -> "SendOutcome":
        """Put an explicitly requested send in the durable queue and return.

        The entry point the inbound API uses. It does NOT wait for WhatsApp:
        a physical send costs seconds, and a caller posting a burst of twenty
        cannot hold twenty HTTP connections open for minutes — that was the
        original design's mistake, and it produced `timeout` responses for
        messages that had in fact been delivered.

        Everything the queue provides applies from here on: the message is on
        disk before this returns, it keeps its place in that chat's order, it
        is retried on transport failure, its delivery is verified against the
        conversation, and a crash mid-send is resolved by reading the chat
        rather than by guessing."""
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return SendOutcome(False, error="Chat not found.")
        message = await self._delivery.enqueue(chat, text, origin=origin)
        # Wake the drainer rather than waiting for the next poll tick.
        self._enqueue(_Job("drain", chat_id))
        self.publish()
        return SendOutcome(True, outgoing_id=message.outgoing_id, queued=True)

    def outgoing_status(self, outgoing_id: str):
        """One queued message's current state, for the API's status lookup."""
        for message in self._repo.all_outgoing():
            if message.outgoing_id == outgoing_id:
                return message
        return None

    async def send_message(self, chat_id: str, text: str, origin: str = "api") -> "SendOutcome":
        """Send `text` to a chat and persist the result — the entry point the
        inbound send API uses.

        It goes through exactly the same sender, the same action lock and the
        same verification as an automated reply, so an API send cannot interleave
        with one and cannot report success without the compose box having
        cleared. Automation being on or off is irrelevant here: this is not the
        engine deciding to answer, it is someone explicitly asking for a
        message to go out."""
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return SendOutcome(False, error="Chat not found.")

        result = await self._sender.send_async(chat.chat_name, text)
        if not result.ok:
            chat.last_error = result.detail
            await asyncio.to_thread(self._repo.save_chat, chat)
            self._repo.log("ERROR", "api.send_failed", chat_id=chat.chat_id,
                           chat_name=chat.chat_name, direction="out", error=result.detail,
                           message=f"Send API could not deliver: {text[:120]}")
            return SendOutcome(False, error=result.detail)

        stored = StoredMessage(
            message_key=outgoing_key_for(chat.chat_id, text),
            chat_id=chat.chat_id,
            chat_name=chat.chat_name,
            phone_number=chat.phone_number,
            sender="You",
            text=text,
            direction="out",
            status=MessageStatus.SENT,
            origin=origin,
        )
        await asyncio.to_thread(self._repo.save_message, stored)
        chat.last_outgoing_text = text
        chat.last_outgoing_utc = utcnow()
        chat.last_error = ""
        await asyncio.to_thread(self._repo.save_chat, chat)
        await asyncio.to_thread(self._repo.flush_json, True)
        self._repo.log("INFO", "api.send", chat_id=chat.chat_id, chat_name=chat.chat_name,
                       direction="out", message=f"Sent via {result.strategy}: {text[:120]}")
        self.publish()
        return SendOutcome(True, strategy=result.strategy, message_key=stored.message_key)

    async def set_external_id(self, chat_id: str, external_id: str) -> None:
        chat = self._repo.get_chat(chat_id)
        if chat is None:
            return
        chat.external_id = (external_id or "").strip()
        await asyncio.to_thread(self._repo.save_chat, chat)
        self._repo.log("INFO", "chat.contact_id", chat_id=chat_id, chat_name=chat.chat_name,
                       message=f"Contact ID set to {chat.external_id or '(empty)'}")
        await asyncio.to_thread(self._repo.flush_json, True)
        self.publish()

    async def scan_chat_now(self, chat_id: str) -> None:
        chat = self._repo.get_chat(chat_id)
        if chat is not None:
            self._enqueue(_Job("scan", chat_id))
            self.publish()
