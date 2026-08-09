"""The outgoing pipeline: queue → send → verify → delivered.

Every message this application sends — a webhook reply, a relayed message, a
send-API request — goes through here. Nothing calls the sender directly any
more, and that separation is the point:

    produce                     deliver
    ───────                     ───────
    webhook reply ─┐
    relay message ─┼─▶ enqueue ─▶ [queue] ─▶ worker ─▶ send ─▶ verify ─▶ done
    API request  ──┘   (persist)   durable    one at    UIA     read
                                              a time            back

**Producing and delivering are different jobs with different failure modes.**
A webhook that answers is a success even if WhatsApp is locked; a locked
session is a reason to wait, not a reason to lose the reply. Splitting them
means a message is safe on disk the instant it exists, and delivery becomes a
separate, retryable concern.

**The queue is durable and ordered.** It lives in MongoDB and the JSON mirror,
so a crash loses nothing; each message carries a per-chat sequence, so two
replies to one conversation arrive in the order they were produced.

**Restart is not the same as retry.** A message that was `QUEUED` when the
process died is safe to send — nothing was attempted. One that was `SENDING` or
`VERIFYING` is *ambiguous*: it may already be on someone's screen. Those are
verified against the chat rather than re-sent, because a duplicate message is
worse than a late one.
"""

from __future__ import annotations

import logging
import time
from wadam.domain.models import (
    OutgoingMessage,
    OutgoingStatus,
    StoredMessage,
    outgoing_key_for,
    utcnow,
)
from wadam.engine import send_guard
from wadam.storage.repository import Repository
from wadam.whatsapp.verifier import (
    SendVerifier,
    Verification,
    VerificationResult,
    normalise,
)

logger = logging.getLogger(__name__)


def _consecutive_by_chat(messages):
    """Group a queue slice into runs of the same chat, order preserved.

    Deliberately consecutive rather than grouped-by-key: reordering the queue to
    put all of one chat's messages together would break the guarantee that
    messages leave in the order they were produced."""
    groups: list = []
    for message in messages:
        if groups and groups[-1][0] == message.chat_id:
            groups[-1][1].append(message)
        else:
            groups.append((message.chat_id, [message]))
    return groups


class DeliveryService:
    def __init__(self, repository: Repository, sender, verifier: SendVerifier,
                 to_thread, metrics=None) -> None:
        self._repo = repository
        self._sender = sender
        self._verifier = verifier
        self._to_thread = to_thread
        self._metrics = metrics

    # -- producing ---------------------------------------------------------

    async def enqueue(self, chat, text: str, origin: str,
                      source_message_key: str = "",
                      external_ref: str = "") -> OutgoingMessage:
        """Put a message in the queue. Persisted before it is returned, so from
        this moment on it cannot be lost by a crash."""
        message = OutgoingMessage(
            chat_id=chat.chat_id, chat_name=chat.chat_name, text=text,
            origin=origin, source_message_key=source_message_key,
            external_ref=external_ref,
        )
        await self._to_thread(self._repo.enqueue_outgoing, message)
        await self._to_thread(self._repo.flush_json, True)
        self._repo.log("INFO", "outgoing.queued", chat_id=chat.chat_id,
                       chat_name=chat.chat_name, direction="out",
                       correlation_id=message.outgoing_id,
                       message=f"Queued ({origin}): {text[:100]}")
        if self._metrics:
            self._metrics.record_queued()
        return message

    # -- delivering --------------------------------------------------------

    async def deliver_batch(self, messages: list) -> list:
        """Drain a backlog as one run rather than one message at a time.

        Same guarantees, far less overhead. Per-message delivery pays, for every
        message: a session probe, a wait for the desktop to go quiet, a
        foreground change and its restore, a pre-send census read and a
        post-send verification read — the two reads costing about two seconds
        each. Twenty messages paid that twenty times.

        Here the batch takes the foreground once, and each chat gets **one**
        census read before its messages and **one** verification read after
        them.

        What does not change: messages keep their per-chat order, a transport
        failure is still retried, and verification still requires the bubble
        count to have *increased* — including for repeated identical text,
        where sending "OK" three times must produce three new bubbles.
        Consecutive messages are grouped by chat so ordering across chats is
        preserved exactly as the queue produced it."""
        if not messages:
            return []

        delivered: list = []
        async with self._sender.batch() as send:
            for chat_id, group in _consecutive_by_chat(messages):
                chat = self._repo.get_chat(chat_id)
                if chat is None:
                    for message in group:
                        message.status = OutgoingStatus.CANCELLED
                        message.error = "the chat no longer exists"
                        await self._to_thread(self._repo.update_outgoing, message)
                        delivered.append(message)
                    continue
                delivered.extend(await self._deliver_group(chat, group, send))
        return delivered

    async def _deliver_group(self, chat, group: list, send) -> list:
        """One chat's slice of a batch: census once, send each, verify once."""
        before = await self._verifier.census_many(
            chat.chat_name, [m.text for m in group])

        sent: list = []
        results: dict = {}
        for message in group:
            message.attempts += 1
            message.status = OutgoingStatus.SENDING
            await self._to_thread(self._repo.update_outgoing, message)

            try:
                send_guard.check(message.origin, chat_name=chat.chat_name,
                                 text=message.text)
            except send_guard.SendRefused as refused:
                await self._refused(chat, message, str(refused))
                continue
            result = await send(chat.chat_name, message.text)
            results[message.outgoing_id] = result
            logger.debug("batch send %d/%d in %d ms (%s)", len(results), len(group),
                        result.duration_ms, result.strategy or result.detail)
            if not result.ok:
                await self._transport_failed(chat, message, result)
                continue

            message.status = OutgoingStatus.VERIFYING
            await self._to_thread(self._repo.update_outgoing, message)
            sent.append(message)

        if not sent:
            return list(group)

        if before is None:
            # The conversation could not be read, so a new bubble cannot be
            # told apart from one already there. Every message that left the
            # box is UNVERIFIED — never re-sent on a guess.
            for message in sent:
                verification = VerificationResult(
                    Verification.UNREADABLE,
                    "the conversation could not be read before sending, so a new "
                    "bubble cannot be told apart from one that was already there",
                )
                message.verification = verification.status
                if self._metrics:
                    self._metrics.record_verification(verification)
                await self._unverified(chat, message, results[message.outgoing_id],
                                       verification)
            return list(group)

        # How many bubbles of each text should exist once this batch has landed:
        # the baseline plus one per copy actually sent.
        expected = dict(before)
        for message in sent:
            key = normalise(message.text)
            expected[key] = expected.get(key, 0) + 1

        started = time.monotonic()
        seen = await self._verifier.confirm_many(chat.chat_name, expected, before)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        # Enough to tell a LOST message from an UNSEEN one without guessing.
        # A real run produced two identical sends, both reporting transport
        # success, and one bubble. These three numbers separate the cases:
        # before == after means nothing landed at all; after == before + 1 with
        # two sent means WhatsApp accepted one and dropped the other; a later
        # manual read disagreeing with `after` means the READER missed it.
        for key, need in expected.items():
            self._repo.log(
                "INFO", "outgoing.census", chat_id=chat.chat_id,
                chat_name=chat.chat_name, direction="out",
                message=(f"census {key[:40]!r}: before={before.get(key, 0)} "
                         f"expected={need} after={seen.get(key, 0)} "
                         f"sent_this_batch={sum(1 for m in sent if normalise(m.text) == key)}"),
            )

        # Attribute the result per message. For repeated identical text the
        # nth copy needs the count to have reached baseline + n, so a batch that
        # delivered two of three "OK"s marks exactly one as unverified.
        landed: dict = dict(before)
        for message in sent:
            key = normalise(message.text)
            landed[key] = landed.get(key, 0) + 1
            result = results[message.outgoing_id]
            if seen.get(key, 0) >= landed[key]:
                verification = VerificationResult(
                    Verification.VERIFIED, "",
                    elapsed_ms=elapsed_ms,
                    matches_before=before.get(key, 0),
                    matches_after=seen.get(key, 0),
                )
                message.verification = verification.status
                if self._metrics:
                    self._metrics.record_verification(verification)
                await self._delivered(chat, message, result, verification)
            else:
                verification = VerificationResult(
                    Verification.NOT_FOUND,
                    f"expected {landed[key]} copies of this text in the chat, "
                    f"found {seen.get(key, 0)}",
                    elapsed_ms=elapsed_ms,
                    matches_before=before.get(key, 0),
                    matches_after=seen.get(key, 0),
                )
                message.verification = verification.status
                if self._metrics:
                    self._metrics.record_verification(verification)
                await self._unverified(chat, message, result, verification)
        return list(group)

    async def deliver(self, message: OutgoingMessage) -> OutgoingMessage:
        """Take one queued message all the way to delivered-or-failed."""
        chat = self._repo.get_chat(message.chat_id)
        if chat is None:
            message.status = OutgoingStatus.CANCELLED
            message.error = "the chat no longer exists"
            await self._to_thread(self._repo.update_outgoing, message)
            return message

        message.attempts += 1
        message.status = OutgoingStatus.SENDING
        await self._to_thread(self._repo.update_outgoing, message)

        # Census BEFORE the send: verification asks whether a *new* matching
        # bubble appeared, which cannot be answered without knowing how many
        # were there already.
        before = await self._verifier.census(chat.chat_name, message.text)

        try:
            send_guard.check(message.origin, chat_name=chat.chat_name, text=message.text)
        except send_guard.SendRefused as refused:
            return await self._refused(chat, message, str(refused))

        result = await self._sender.send_async(chat.chat_name, message.text)

        if not result.ok:
            return await self._transport_failed(chat, message, result)

        message.status = OutgoingStatus.VERIFYING
        await self._to_thread(self._repo.update_outgoing, message)

        verification = await self._verifier.confirm(chat.chat_name, message.text, before)
        message.verification = verification.status
        if self._metrics:
            self._metrics.record_verification(verification)

        if verification.ok:
            return await self._delivered(chat, message, result, verification)
        return await self._unverified(chat, message, result, verification)

    async def resume_ambiguous(self, message: OutgoingMessage) -> OutgoingMessage:
        """A message that was in flight when the process stopped.

        It is NOT re-sent. The conversation is read and the message is looked
        for; only its absence justifies another attempt. "I don't know" is
        answered by checking, never by sending again."""
        chat = self._repo.get_chat(message.chat_id)
        if chat is None:
            message.status = OutgoingStatus.CANCELLED
            message.error = "the chat no longer exists"
            await self._to_thread(self._repo.update_outgoing, message)
            return message

        # A census of zero means "look for any matching bubble at all".
        verification = await self._verifier.confirm(chat.chat_name, message.text, before=0)
        if verification.ok:
            self._repo.log("INFO", "outgoing.recovered", chat_id=chat.chat_id,
                           chat_name=chat.chat_name, direction="out",
                           correlation_id=message.outgoing_id,
                           message=f"Was already delivered before the restart: "
                                   f"{message.text[:80]}")
            return await self._delivered(chat, message, None, verification)

        self._repo.log("INFO", "outgoing.resuming", chat_id=chat.chat_id,
                       chat_name=chat.chat_name, direction="out",
                       correlation_id=message.outgoing_id,
                       message=f"Not found in the chat after a restart — sending: "
                               f"{message.text[:80]}")
        message.status = OutgoingStatus.QUEUED
        await self._to_thread(self._repo.update_outgoing, message)
        return await self.deliver(message)

    async def _refused(self, chat, message, reason: str):
        """The guard forbade this producer. CANCELLED, not failed: nothing was
        attempted and nothing should be retried."""
        message.status = OutgoingStatus.CANCELLED
        message.error = reason
        await self._to_thread(self._repo.update_outgoing, message)
        self._repo.log("WARNING", "outgoing.refused", chat_id=chat.chat_id,
                       chat_name=chat.chat_name, direction="out",
                       correlation_id=message.outgoing_id, error=reason,
                       message=f"Refused ({message.origin}): {message.text[:80]}")
        return message

    # -- outcomes ----------------------------------------------------------

    async def _delivered(self, chat, message: OutgoingMessage, result,
                         verification) -> OutgoingMessage:
        message.status = OutgoingStatus.DELIVERED
        message.delivered_at = utcnow()
        message.error = ""
        await self._to_thread(self._repo.update_outgoing, message)

        stored = StoredMessage(
            message_key=outgoing_key_for(chat.chat_id, message.text),
            chat_id=chat.chat_id, chat_name=chat.chat_name,
            phone_number=chat.phone_number, sender="You",
            text=message.text, direction="out", status="sent",
            origin=message.origin, external_ref=message.external_ref,
        )
        await self._to_thread(self._repo.save_message, stored)
        chat.last_outgoing_text = message.text
        chat.last_outgoing_utc = utcnow()
        chat.last_error = ""
        await self._to_thread(self._repo.save_chat, chat)
        await self._to_thread(self._repo.flush_json, True)

        strategy = getattr(result, "strategy", "recovered")
        self._repo.log("INFO", "outgoing.delivered", chat_id=chat.chat_id,
                       chat_name=chat.chat_name, direction="out",
                       correlation_id=message.outgoing_id,
                       response=verification.bubble_time,
                       retry_count=max(0, message.attempts - 1),
                       message=f"Delivered via {strategy} — {verification.describe()}: "
                               f"{message.text[:100]}")
        if self._metrics:
            self._metrics.record_sent(getattr(result, "duration_ms", 0))
        return message

    async def _unverified(self, chat, message: OutgoingMessage, result,
                          verification) -> OutgoingMessage:
        """The compose box cleared but no bubble was found.

        Deliberately NOT retried by default. The transport said it left the
        box, so a retry risks a duplicate — the opposite failure and the worse
        one. It is recorded as unverified and surfaced, so a person decides."""
        message.status = OutgoingStatus.UNVERIFIED
        message.error = verification.reason
        await self._to_thread(self._repo.update_outgoing, message)
        chat.last_error = f"unverified send: {verification.reason}"
        await self._to_thread(self._repo.save_chat, chat)
        await self._to_thread(self._repo.flush_json, True)
        self._repo.log("WARNING", "outgoing.unverified", chat_id=chat.chat_id,
                       chat_name=chat.chat_name, direction="out",
                       correlation_id=message.outgoing_id,
                       error=verification.reason,
                       message=f"Sent but not confirmed in the chat — NOT retried, to "
                               f"avoid a duplicate: {message.text[:80]}")
        if self._metrics:
            self._metrics.record_verification_failure()
        return message

    async def _transport_failed(self, chat, message: OutgoingMessage,
                                result) -> OutgoingMessage:
        """Never left the compose box — safe to retry, nothing was delivered."""
        message.error = result.detail
        if message.exhausted:
            message.status = OutgoingStatus.FAILED
            self._repo.log("ERROR", "outgoing.failed", chat_id=chat.chat_id,
                           chat_name=chat.chat_name, direction="out",
                           correlation_id=message.outgoing_id,
                           error=result.detail, retry_count=message.attempts,
                           message=f"Giving up after {message.attempts} attempt(s): "
                                   f"{message.text[:80]}")
        else:
            message.status = OutgoingStatus.QUEUED
            self._repo.log("WARNING", "outgoing.retry", chat_id=chat.chat_id,
                           chat_name=chat.chat_name, direction="out",
                           correlation_id=message.outgoing_id,
                           error=result.detail, retry_count=message.attempts,
                           message=f"Attempt {message.attempts} did not leave the compose "
                                   f"box — requeued: {message.text[:80]}")
        chat.last_error = result.detail
        await self._to_thread(self._repo.update_outgoing, message)
        await self._to_thread(self._repo.save_chat, chat)
        await self._to_thread(self._repo.flush_json, True)
        if self._metrics:
            self._metrics.record_send_failure()
            if message.status == OutgoingStatus.QUEUED:
                self._metrics.record_retry()
        return message
