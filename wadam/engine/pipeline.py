"""The message processing pipeline.

    message detected
        ↓
    save MongoDB          ← the caller does this before we are entered
        ↓
    write JSON
        ↓
    mark DISPATCHING  →  save  →  write JSON     ← the crash-safety point
        ↓
    webhook
        ↓
    receive response
        ↓
    save response  →  write JSON
        ↓
    mark AWAITING_SEND  →  save  →  write JSON
        ↓
    send message (UI Automation)
        ↓
    verify
        ↓
    mark REPLIED  →  save  →  write JSON

**Persistence is never skipped and nothing is processed only in memory.** Each
transition reaches MongoDB and the JSON mirror *before* the next step runs, so a
crash anywhere leaves a record of exactly how far the message got — and, more
usefully, of what the outside world has already seen.

That last part is what makes recovery possible without guessing. `DISPATCHING`
is written before the webhook call and `AWAITING_SEND` before the send, so on
restart the engine can tell "the endpoint definitely has not seen this" from
"it might have". See `MessageStatus` for the full table and
`AutomationEngine._recover_incomplete` for what is done with each state.

Two things this pipeline will not do:

* **Send a reply it has not verified.** A send whose compose box did not clear
  is recorded as `reply_failed`, and no outgoing message is claimed.
* **Retry a webhook it cannot prove was never delivered.** Losing an automatic
  reply is recoverable; sending someone's customer two of them is not.
"""

from __future__ import annotations

import logging
from typing import Optional

from wadam.domain.models import (
    ChatConfig,
    MessageStatus,
    StoredMessage,
    WebhookRecord,
    outgoing_key_for,
    utcnow,
)
from wadam.engine.webhook import WebhookClient, build_payload, optional_reply
from wadam.storage.repository import Repository
from wadam.whatsapp.sender import WhatsAppSender

logger = logging.getLogger(__name__)


class MessagePipeline:
    def __init__(self, repository: Repository, webhook: WebhookClient, sender: WhatsAppSender,
                 to_thread, delivery=None, metrics=None) -> None:
        self._repo = repository
        self._webhook = webhook
        self._sender = sender
        # When present, replies are QUEUED rather than sent inline. Producing a
        # reply and delivering it are different jobs with different failure
        # modes: a webhook that answered is a success even if WhatsApp is
        # locked, and the reply should wait rather than be lost.
        self._delivery = delivery
        self._metrics = metrics
        # Injected rather than imported so every blocking repository call in
        # here is visibly off the event loop.
        self._to_thread = to_thread

    # -- the normal path ---------------------------------------------------

    async def process(self, chat: ChatConfig, message: StoredMessage) -> None:
        """Run one incoming message through webhook → reply → verification.

        The message is already persisted when this is called; everything after
        that happens here, each step persisted in turn."""
        await self._to_thread(self._repo.flush_json, True)

        if not (chat.webhook_url or "").strip():
            await self._finish(chat, message, status=MessageStatus.IGNORED,
                               webhook_status="no webhook configured")
            self._log(chat, "WARNING", "webhook.missing", message,
                      "Automation is on but no webhook URL is configured — message stored only.")
            return

        # The crash-safety point: this is written BEFORE the call, so a message
        # found in this state after a crash is known to be ambiguous rather than
        # assumed safe to retry.
        message.status = MessageStatus.DISPATCHING
        await self._to_thread(self._repo.update_message, message)
        await self._to_thread(self._repo.flush_json, True)

        outcome = await self._call_webhook(chat, message)
        if outcome is None:
            return

        reply = optional_reply(outcome)
        if reply is None:
            # A deliberate silence from the endpoint. Successful, and final.
            await self._finish(chat, message, status=MessageStatus.WEBHOOK_OK,
                               webhook_status=outcome.status_text)
            self._log(chat, "INFO", "webhook.no_reply", message,
                      f"{outcome.status_text} — endpoint returned no reply, nothing sent.")
            return

        message.reply_text = reply
        message.status = MessageStatus.AWAITING_SEND
        await self._to_thread(self._repo.update_message, message)
        await self._to_thread(self._repo.save_chat, chat)
        await self._to_thread(self._repo.flush_json, True)

        await self._send_reply(chat, message, reply, webhook_status=outcome.status_text)

    async def _call_webhook(self, chat: ChatConfig, message: StoredMessage):
        """Call the endpoint and persist everything about the attempt. Returns
        the outcome, or None when it failed and the message is finished."""
        payload = build_payload(chat, message)
        outcome = await self._webhook.call(chat.webhook_url, payload)

        record = WebhookRecord(
            chat_id=chat.chat_id,
            chat_name=chat.chat_name,
            message_key=message.message_key,
            url=chat.webhook_url,
            request=payload,
            status_code=outcome.status_code,
            ok=outcome.ok,
            attempts=outcome.attempts,
            response_body=outcome.body,
            reply_text=outcome.reply_text,
            error=outcome.error,
            duration_ms=outcome.duration_ms,
        )
        await self._to_thread(self._repo.save_webhook, record)
        if self._metrics:
            self._metrics.record_webhook(outcome.ok, outcome.duration_ms)

        message.webhook_id = record.webhook_id
        chat.last_webhook_utc = utcnow()
        chat.last_webhook_status = outcome.status_text
        chat.last_webhook_response = (outcome.reply_text or outcome.body or outcome.error)[:1000]
        # "Attempts spent on the most recent call", not a lifetime total — a
        # lifetime total never goes down, so it never tells you whether the
        # endpoint is healthy *now*.
        chat.webhook_retry_count = max(0, outcome.attempts - 1)

        if not outcome.ok:
            message.error = outcome.error
            await self._finish(chat, message, status=MessageStatus.WEBHOOK_FAILED,
                               webhook_status=outcome.status_text, error=outcome.error)
            self._log(chat, "ERROR", "webhook.failed", message,
                      f"{outcome.status_text} after {outcome.attempts} attempt(s): {outcome.error}",
                      retry_count=max(0, outcome.attempts - 1))
            return None
        return outcome

    async def _send_reply(self, chat: ChatConfig, message: StoredMessage, reply: str,
                          webhook_status: str) -> None:
        if self._delivery is not None:
            await self._delivery.enqueue(chat, reply, origin="webhook_reply",
                                         source_message_key=message.message_key)
            # The reply is durably queued; delivery and verification are the
            # queue's job now. The incoming message's own journey ends here.
            await self._finish(chat, message, status=MessageStatus.REPLIED,
                               webhook_status=webhook_status)
            self._log(chat, "INFO", "reply.queued", message,
                      f"Reply queued for delivery: {reply[:120]}")
            return

        result = await self._sender.send_async(chat.chat_name, reply)

        if not result.ok:
            message.error = result.detail
            await self._finish(chat, message, status=MessageStatus.REPLY_FAILED,
                               webhook_status=webhook_status, error=result.detail)
            self._log(chat, "ERROR", "reply.failed", message, result.detail)
            return

        # The reply we sent is a message in its own right and is stored as one,
        # so the record of the conversation is complete from this side too. The
        # poll will later read the same bubble back out of WhatsApp; ingestion
        # recognises it as ours (Repository.recently_originated) and does not
        # store it a second time.
        outgoing = StoredMessage(
            message_key=outgoing_key_for(chat.chat_id, reply),
            chat_id=chat.chat_id,
            chat_name=chat.chat_name,
            sender="You",
            text=reply,
            direction="out",
            status=MessageStatus.SENT,
            webhook_id=message.webhook_id,
        )
        await self._to_thread(self._repo.save_message, outgoing)

        chat.last_outgoing_text = reply
        chat.last_outgoing_utc = utcnow()
        chat.last_error = ""
        await self._finish(chat, message, status=MessageStatus.REPLIED,
                           webhook_status=webhook_status)
        self._log(chat, "INFO", "reply.sent", message,
                  f"Replied via {result.strategy}: {reply[:120]}")

    # -- recovery ----------------------------------------------------------

    async def resume_send(self, chat: ChatConfig, message: StoredMessage,
                          already_sent: Optional[bool]) -> None:
        """Finish a reply that was persisted but whose send was never confirmed.

        `already_sent` is the caller's verdict after reading the conversation:
        True when the reply text is already in the chat (it did land before the
        crash), False when it is not, None when the chat could not be read. The
        check exists so recovery cannot produce a duplicate outgoing message —
        the reliability requirements forbid that as firmly as duplicate webhook
        calls."""
        if already_sent:
            chat.last_outgoing_text = message.reply_text
            chat.last_outgoing_utc = chat.last_outgoing_utc or utcnow()
            await self._finish(chat, message, status=MessageStatus.REPLIED,
                               webhook_status=chat.last_webhook_status)
            self._log(chat, "INFO", "recovery.already_sent", message,
                      "The reply was already in the chat — marked replied, nothing re-sent.")
            return

        if already_sent is None:
            self._log(chat, "WARNING", "recovery.unverifiable", message,
                      "Could not read the chat to check whether the reply had been sent; "
                      "leaving it for the next cycle rather than risking a duplicate.")
            return

        self._log(chat, "INFO", "recovery.resending", message,
                  f"Resuming an unsent reply from before the restart: {message.reply_text[:80]}")
        await self._send_reply(chat, message, message.reply_text,
                               webhook_status=chat.last_webhook_status)

    # -- shared ------------------------------------------------------------

    async def _finish(self, chat: ChatConfig, message: StoredMessage, *, status: str,
                      webhook_status: str = "", error: str = "") -> None:
        message.status = status
        if error:
            message.error = error
            chat.last_error = error
        if webhook_status:
            chat.last_webhook_status = webhook_status
        await self._to_thread(self._repo.update_message, message)
        await self._to_thread(self._repo.save_chat, chat)
        await self._to_thread(self._repo.flush_json, True)

    def _log(self, chat: ChatConfig, level: str, event: str, message: StoredMessage,
             text: str, retry_count: int = 0) -> None:
        self._repo.log(
            level, event, chat_id=chat.chat_id, chat_name=chat.chat_name, message=text,
            direction=message.direction, correlation_id=message.message_key,
            webhook_url=chat.webhook_url,
            response=message.reply_text, retry_count=retry_count, error=message.error,
        )
