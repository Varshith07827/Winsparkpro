"""What happens to one inbound message.

    OpenWA ──▶ signature ──▶ known chat? ──▶ automation on? ──▶ store
                                                                  │
                                          reply wanted? ◀──────────┘
                                                │
                                          cooldown ──▶ send ──▶ store

Every check answers HTTP 200 except a bad signature. A 4xx or 5xx tells OpenWA
the delivery failed and earns a retry, and there is nothing to retry about a
message that was correctly ignored — it would be ignored again, three more
times. A bad signature is the one case where repeating the request verbatim
really is wrong.

**A failed send also answers 200.** A retry would re-run the decision and could
deliver twice. This is not theoretical: on the first live message through this
architecture, OpenWA 0.7.2 returned HTTP 500 for a message it had *already*
delivered, and a retrying client would have sent four copies. A duplicate is
worse than a miss — the same judgment winSpark made when it refused to retry an
UNVERIFIED send.

Persistence still comes before decisions: the message is stored the moment it
is understood, so a crash anywhere leaves a record of how far it got.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from wadam.domain.models import ChatConfig, MessageStatus, StoredMessage, utcnow
from wadam.engine.guards import Cooldown
from wadam.engine.webhook import WebhookClient, build_payload
from wadam.openwa import InboundMessage, OpenWAClient, SendError

logger = logging.getLogger(__name__)

@dataclass
class Outcome:
    """What the pipeline did, for the HTTP response and the metrics."""

    action: str  # replied | skipped | send_failed | ignored
    reason: str = ""
    chat_id: str = ""

    @property
    def ok(self) -> bool:
        return self.action != "send_failed"

    def as_response(self) -> dict:
        body = {"ok": self.ok, "action": self.action}
        if self.reason:
            body["reason"] = self.reason
        if self.chat_id:
            body["chatId"] = self.chat_id
        return body


class MessagePipeline:
    """Turns a delivered message into a stored record and, sometimes, a reply."""

    def __init__(self, repository, client: OpenWAClient, webhook: WebhookClient,
                 cooldown: Cooldown, metrics=None, answer_groups: bool = False,
                 default_webhook: str = "") -> None:
        self._repo = repository
        self._client = client
        self._webhook = webhook
        self._cooldown = cooldown
        self._metrics = metrics
        self._answer_groups = answer_groups
        self._default_webhook = default_webhook

    def process(self, msg: InboundMessage) -> Outcome:
        """Handle one inbound message. Never raises."""
        if msg.is_outgoing:
            return Outcome("skipped", "outgoing message")

        chat = self._ensure_chat(msg)

        stored = self._store_incoming(msg, chat)
        if stored is None:
            return Outcome("skipped", "duplicate delivery", msg.chat_id)

        if self._metrics:
            self._metrics.record_received()

        # COLLECTED, not left PENDING. Both of these are decisions — the
        # message was stored and deliberately not answered — and a status that
        # never advances makes every such message look like one the process
        # died halfway through.
        if not chat.automation_enabled:
            self._finish(stored, MessageStatus.COLLECTED)
            return Outcome("skipped", "automation off for this chat", msg.chat_id)

        if msg.is_group and not self._answer_groups:
            self._finish(stored, MessageStatus.COLLECTED)
            return Outcome("skipped", "group chat", msg.chat_id)

        url = chat.webhook_url or self._default_webhook
        if not url:
            self._finish(stored, MessageStatus.COLLECTED)
            return Outcome("skipped", "no webhook configured for this chat", msg.chat_id)

        outcome = self._webhook.call(url, build_payload(chat, stored))
        self._record_call(chat, stored, outcome)

        if not outcome.ok:
            logger.warning("webhook for %s failed: %s", chat.chat_name, outcome.error)
            self._finish(stored, MessageStatus.FAILED, error=outcome.error)
            return Outcome("webhook_failed", outcome.error, msg.chat_id)

        answer = outcome.reply_text
        if not answer:
            # "Seen, don't answer" is a successful outcome. Most messages in a
            # live chat do not want an answer, and an endpoint forced to invent
            # one for every message will eventually say something stupid.
            self._finish(stored, MessageStatus.COLLECTED)
            return Outcome("skipped", "endpoint sent no reply", msg.chat_id)

        # Asked last, and only for a message actually about to be answered: a
        # cooldown consumed by a message the reply function ignored would
        # silence the next one that mattered.
        if not self._cooldown.allow(msg.chat_id):
            remaining = self._cooldown.remaining(msg.chat_id)
            self._finish(stored, MessageStatus.COLLECTED)
            return Outcome("skipped", f"cooldown, {remaining:.0f}s remaining", msg.chat_id)

        return self._send_reply(msg, chat, stored, answer)

    # ── the pieces ────────────────────────────────────────────────────

    def _ensure_chat(self, msg: InboundMessage) -> ChatConfig:
        """Find the chat, or register it the first time it is seen.

        Discovery used to be a three-second scrape of the sidebar. It is now a
        side effect of a message arriving — cheaper, and more accurate: the
        chat id comes from OpenWA and is durable, so a renamed contact stays
        the same chat instead of silently becoming a new one.

        A new chat arrives with automation OFF. winSpark defaulted it ON
        because discovery found every chat in the sidebar whether it had ever
        spoken or not; here a chat only appears once someone has messaged it,
        and answering a stranger automatically on first contact is exactly the
        behaviour that gets a number restricted.
        """
        chat = self._repo.get_chat(msg.chat_id)
        if chat is not None:
            if msg.chat_name and chat.chat_name != msg.chat_name:
                chat.chat_name = msg.chat_name
                self._repo.save_chat(chat)
            return chat

        chat = ChatConfig(
            chat_id=msg.chat_id,
            chat_name=msg.chat_name or msg.chat_id,
            automation_enabled=False,
        )
        self._repo.save_chat(chat)
        logger.info("registered new chat %s (%s), automation off", chat.chat_name, chat.chat_id)
        return chat

    def _store_incoming(self, msg: InboundMessage, chat: ChatConfig) -> Optional[StoredMessage]:
        """Persist the message. None if it was already stored.

        The key is WhatsApp's own message id, so deduplication survives a
        restart — the old content hash could not tell two people genuinely
        sending "ok" a minute apart from one message read twice.
        """
        stored = StoredMessage(
            message_key=msg.message_id or f"in:{msg.chat_id}:{utcnow().timestamp()}",
            chat_id=msg.chat_id,
            chat_name=chat.chat_name,
            sender=msg.sender,
            text=msg.text,
            direction="in",
            media_kind=msg.media_kind,
            status=MessageStatus.PENDING,
        )
        return stored if self._repo.save_message(stored) else None

    def _send_reply(self, msg: InboundMessage, chat: ChatConfig,
                    stored: StoredMessage, answer: str) -> Outcome:
        try:
            self._client.send_text(msg.chat_id, answer)
        except SendError as error:
            logger.error("send to %s failed: %s", msg.chat_id, error)
            self._finish(stored, MessageStatus.FAILED, error=str(error))
            if self._metrics:
                self._metrics.record_send(False)
            return Outcome("send_failed", str(error), msg.chat_id)

        stored.reply_text = answer
        self._finish(stored, MessageStatus.REPLIED)
        self.record_outgoing(chat, answer, origin="reply")
        if self._metrics:
            self._metrics.record_send(True)
        logger.info("replied to %s: %s", chat.chat_name, answer[:80])
        return Outcome("replied", "", msg.chat_id)

    def record_outgoing(self, chat: ChatConfig, text: str, origin: str) -> None:
        """Store a message this application sent, and update the chat's preview.

        Public because the send API sends messages the pipeline never saw an
        inbound half for, and those belong in the same history.
        """
        self._repo.save_message(StoredMessage(
            message_key=f"out:{chat.chat_id}:{utcnow().timestamp()}",
            chat_id=chat.chat_id,
            chat_name=chat.chat_name,
            text=text,
            direction="out",
            origin=origin,
            status=MessageStatus.REPLIED,
        ))
        chat.last_message_preview = text
        self._repo.save_chat(chat)

    def _record_call(self, chat: ChatConfig, stored: StoredMessage, outcome) -> None:
        """Keep what the endpoint said, on the chat and on the message.

        Written whether the call succeeded or not: "the endpoint answered 502
        three times" is exactly what someone debugging a silent chat needs, and
        it is invisible if only successes are kept.
        """
        chat.last_webhook_status = outcome.status_text
        chat.last_webhook_response = (outcome.reply_text or outcome.body or outcome.error)[:1000]
        chat.last_webhook_utc = utcnow()
        chat.webhook_retry_count = max(0, outcome.attempts - 1)
        chat.last_error = "" if outcome.ok else f"webhook: {outcome.error}"
        self._repo.save_chat(chat)
        if self._metrics:
            self._metrics.record_webhook(outcome.ok, outcome.duration_ms)

    def _finish(self, stored: StoredMessage, status: str, error: str = "") -> None:
        stored.status = status
        if error:
            stored.error = error
        self._repo.update_message(stored)
