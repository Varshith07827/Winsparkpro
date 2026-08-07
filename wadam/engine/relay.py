"""The relay — winSpark's fetch-webhook model, on this application's plumbing.

    every few seconds:  app ──GET https://your.server/hook?tok──▶ your server
                        app ◀── {"message": "Hello Varshith"} ───┘
                         └─▶ deduped, persisted, sent to the bound chat

It is the mirror image of the outbound webhook, over **the same URL**. A chat's
webhook gets a `POST` when a message arrives, and a `GET` when the relay asks
whether anything is waiting to go out. One URL, two verbs, one thing to
configure.

Pull rather than push, which is the whole appeal: no listening socket, no open
port, no token crossing the network, and it works from behind NAT or a
corporate firewall where nothing can reach the machine WhatsApp runs on.

---

**Deduplication is the hard part**, because a GET is not inherently a
destructive read. If the endpoint keeps returning the same message, the relay
must not keep sending it.

winSpark deduped on an external id when present and otherwise on a SHA-256 of
the content, permanently — so a chat could never be sent the same text twice,
ever. That is safe and slightly wrong: "OK" is a perfectly reasonable thing to
send twice in a day.

The rule here:

1. **An `id` in the response is authoritative.** Seen before → skip, forever.
   This is the only way to send identical text twice on purpose, and the reason
   to include one.
2. **Without an id, only a *consecutive* repeat is suppressed** — the same text
   the relay last sent to that chat. A poll URL is a statement of what is
   pending, so an unchanged answer means "nothing new", while a changed one
   (and later a change back) means a genuinely new message.
3. **An endpoint that has ever answered "nothing waiting" is exempt from rule
   2 entirely, from then on.** Answering empty proves it dequeues, and an
   endpoint that dequeues never shows the same message twice — so everything it
   hands over is new, however the text reads.

Rule 3 exists because rule 2 does real damage to a dequeuing endpoint.
Suppressing a message there does not defer it: the endpoint removed it from its
queue to hand it over, so a message we decline is a message **destroyed**. That
is not hypothetical — it cost four of eight messages in a live run, where every
message carried identical text and two of them were queued 0.8 seconds apart.

So: an endpoint that never reports empty is still protected from re-sending its
one message forever, and an endpoint that does report empty can send "OK" as
many times in a row as it likes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from wadam.domain.models import (
    ChatConfig,
    MessageStatus,
    StoredMessage,
    outgoing_key_for,
    utcnow,
)
from wadam.engine.webhook import RelayMessage, WebhookClient, parse_relay_messages
from wadam.storage.repository import Repository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelayPoll:
    """What one GET of a chat's webhook produced."""

    chat_id: str
    messages: tuple[RelayMessage, ...] = ()
    status: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class RelayService:
    def __init__(self, repository: Repository, webhook: WebhookClient, to_thread,
                 delivery=None) -> None:
        self._repo = repository
        self._webhook = webhook
        self._to_thread = to_thread
        self._delivery = delivery

    # -- polling -----------------------------------------------------------

    def is_eligible(self, chat: ChatConfig) -> bool:
        """A chat is polled when it is automated and has somewhere to poll.
        The same two things that make it answer incoming messages — there is
        one switch per chat, not two."""
        return bool(chat.automation_enabled and (chat.webhook_url or "").strip())

    async def poll(self, chat: ChatConfig) -> RelayPoll:
        outcome = await self._webhook.fetch(chat.webhook_url)
        if not outcome.ok:
            return RelayPoll(chat_id=chat.chat_id, status=outcome.status_text,
                             error=outcome.error or outcome.status_text)
        messages = tuple(m for m in parse_relay_messages(outcome.body) if m.text.strip())
        return RelayPoll(chat_id=chat.chat_id, messages=messages,
                         status=f"{outcome.status_code} OK · "
                                f"{len(messages) or 'nothing'} waiting")

    # -- deduplication -----------------------------------------------------

    def should_send(self, chat: ChatConfig, message: RelayMessage) -> tuple[bool, str]:
        """Returns (send, reason-when-not). See the module docstring for the
        rules and why each one is there."""
        if message.external_id:
            if self._repo.has_relay_id(chat.chat_id, message.external_id):
                return False, f"id {message.external_id} was already relayed"
            return True, ""
        if chat.relay_dequeues:
            # This endpoint has told us "nothing waiting" at least once, so it
            # dequeues, so it never shows the same message twice — anything it
            # hands over is new. Suppressing one here would not defer it, it
            # would DESTROY it: the endpoint has already removed it from its
            # queue by the time we decide.
            return True, ""
        if _same_text(chat.last_relay_text, message.text):
            return False, "identical to the last relayed message and carries no id"
        return True, ""

    # -- sending -----------------------------------------------------------

    async def enqueue(self, chat: ChatConfig, message: RelayMessage) -> bool:
        """Hand a polled message to the outgoing queue.

        The duplicate guard is updated here, at enqueue time rather than after
        delivery: the endpoint has already handed the message over, so the
        decision "have we taken this one?" is settled now. Delivery outcomes are
        the queue's concern."""
        if self._delivery is None:
            return False
        await self._delivery.enqueue(chat, message.text, origin="relay",
                                     external_ref=message.external_id)
        chat.last_relay_text = message.text
        chat.last_relay_utc = utcnow()
        chat.last_relay_status = "queued for delivery"
        await self._to_thread(self._repo.save_chat, chat)
        return True

    async def deliver(self, chat: ChatConfig, message: RelayMessage, sender) -> bool:
        """Persist, send, verify, persist. The same order and the same
        guarantees as an automated reply: nothing is claimed as sent that was
        not observed to leave the compose box."""
        result = await sender.send_async(chat.chat_name, message.text)

        if not result.ok:
            chat.last_error = result.detail
            chat.last_relay_status = f"send failed: {result.detail}"
            await self._to_thread(self._repo.save_chat, chat)
            self._repo.log("ERROR", "relay.send_failed", chat_id=chat.chat_id,
                           chat_name=chat.chat_name, direction="out",
                           webhook_url=chat.webhook_url, error=result.detail,
                           message=f"Relayed message could not be delivered: {message.text[:120]}")
            # Deliberately NOT recorded as relayed: the next poll should offer
            # it again. An endpoint that has dequeued it will not, and that is
            # the endpoint's choice to make, not ours.
            return False

        stored = StoredMessage(
            message_key=outgoing_key_for(chat.chat_id, message.text),
            chat_id=chat.chat_id,
            chat_name=chat.chat_name,
            sender="You",
            text=message.text,
            direction="out",
            status=MessageStatus.SENT,
            origin="relay",
            external_ref=message.external_id,
        )
        await self._to_thread(self._repo.save_message, stored)

        chat.last_outgoing_text = message.text
        chat.last_outgoing_utc = utcnow()
        chat.last_relay_text = message.text
        chat.last_relay_utc = utcnow()
        chat.last_relay_status = f"relayed via {result.strategy}"
        chat.last_error = ""
        await self._to_thread(self._repo.save_chat, chat)
        await self._to_thread(self._repo.flush_json, True)
        self._repo.log("INFO", "relay.sent", chat_id=chat.chat_id, chat_name=chat.chat_name,
                       direction="out", webhook_url=chat.webhook_url,
                       response=message.external_id,
                       message=f"Relayed via {result.strategy}: {message.text[:120]}")
        return True

    async def record_poll(self, chat: ChatConfig, poll: RelayPoll) -> None:
        """Keep the chat's relay status current even when a poll found nothing
        — "polled, empty" and "not polled at all" look identical otherwise.

        **An empty poll also clears the duplicate guard.** That is the whole
        point of it: the guard exists to stop an endpoint that never dequeues
        from re-sending its one message forever, and an endpoint that answers
        "nothing waiting" has just proved it does dequeue. Once it says nothing
        is pending, the next thing it offers is new by definition — even if the
        text is identical to the last message sent.

        Without this the rule punishes exactly the endpoints that behave best:
        queue "OK", have it delivered, queue "OK" again an hour later, and the
        second one would be silently dropped."""
        chat.last_relay_utc = utcnow()
        if poll.error:
            chat.last_relay_status = f"poll failed: {poll.error}"[:200]
        elif not poll.messages:
            chat.last_relay_status = poll.status
            chat.last_relay_text = ""
            chat.relay_dequeues = True
        else:
            return  # deliver() writes a more useful status
        await self._to_thread(self._repo.save_chat, chat)

    def note_skipped(self, chat: ChatConfig, message: RelayMessage, reason: str) -> None:
        # Debug, not info: a non-dequeuing endpoint produces one of these every
        # poll, and at INFO it would bury the log in seconds.
        logger.debug("relay: skipped a message for %s — %s", chat.chat_name, reason)


def _same_text(left: Optional[str], right: Optional[str]) -> bool:
    return " ".join((left or "").split()) == " ".join((right or "").split())
