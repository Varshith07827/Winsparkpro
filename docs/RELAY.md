# The relay — polling your webhook for outbound messages

winSpark's fetch-webhook model, on this application's plumbing.

```
every RELAY_POLL_INTERVAL seconds:

    app ──GET https://your.server/hook?tok──▶ your server
    app ◀── {"id":"42","message":"Hello Varshith"} ──┘
     └─▶ deduped → persisted → sent by UI Automation → verified → persisted
```

It is the mirror image of the outbound webhook, over **the same URL**. A chat's
webhook gets a `POST` when a message arrives, and a `GET` when the relay asks
whether anything is waiting to go out. One URL, two verbs, one thing to
configure.

**Why pull rather than push.** No listening socket, no open port, no token
crossing the network, and it works from behind NAT or a corporate firewall
where nothing can reach the machine WhatsApp runs on. The
[send API](SEND_API.md) does the same job by push; use whichever suits where
your server sits. They can both be on at once.

---

## Enabling it

```ini
RELAY_ENABLED=true
RELAY_POLL_INTERVAL=3
```

Off by default, deliberately: an endpoint written to receive POSTs should not
start getting GETs because the application was upgraded.

A chat is polled when it is **automated** and **has a webhook URL** — the same
two things that make it answer incoming messages. There is one switch per chat,
not two.

`RELAY_POLL_INTERVAL` is per chat, minimum 1 second. Every automated chat with
a webhook is polled, so ten chats at 3 seconds is 200 requests a minute against
your server; raise it if you have many.

---

## What your endpoint returns

Everything winSpark understood, unchanged:

```json
{"message": "Hello Varshith"}          {"text": …}   {"content": …}
{"body": …}    {"msg": …}   {"reply": …}
{"data": {"message": …}}               {"result": {"text": …}}
"Hello Varshith"                        Hello Varshith      ← plain text
```

Nothing waiting? Return `{}`, an empty body, `null`, or `[]`. All mean "no", and
none is an error.

### Several at once

An array yields **every** message in it, in order:

```json
[{"id":"1","message":"first"},
 {"id":"2","message":"second"},
 {"id":"3","message":"third"}]
```

All three are sent. Delivering only the first is how a backlog quietly
disappears.

### Include an `id`

Read from `_id`, `message_id`, `messageId`, `external_id`, `externalId`, `uid`
or `id` — **in that order**, and a bare number is fine. `_id` comes first
deliberately: a queue that hands back its own document carries the message's
identity there, while `id` is often the *destination* (a chat or contact
number), identical on every message, which would suppress all but the first. It is not required — but it is what lets you send the
same text twice on purpose, and it is the difference between the relay knowing
what it has handled and inferring it.

---

## Deduplication

This is the part worth understanding, because a `GET` is not inherently a
destructive read. If your endpoint keeps returning the same message, the relay
must not keep sending it — while an endpoint that legitimately wants to send
"OK" twice must still be able to.

**Rule 1 — an `id` is authoritative.** Seen before → never sent again. Two
messages with different ids are two messages, even if the text is identical and
they arrive back to back.

**Rule 2 — without an id, only a *consecutive* repeat is suppressed.** The relay
compares against the last text it sent to that chat. A poll URL is a statement
of what is pending, so an unchanged answer means "nothing new".

**Rule 3 — an endpoint that has ever answered "nothing waiting" is exempt from
rule 2 from then on.** Answering empty proves it dequeues, and an endpoint that
dequeues never shows the same message twice, so everything it hands over is new
however the text reads. The fact is remembered per chat (`relay_dequeues`), so
a restart does not re-arm a guard already proved unnecessary. A *failed* poll
proves nothing — a 503 says nothing about the queue.

Rule 3 exists because rule 2 does real damage to a dequeuing endpoint.
Suppressing a message there does not defer it: the endpoint removed it from its
queue in order to hand it over, so **a message declined is a message
destroyed**. Measured, not theorised — it cost four of eight messages in a live
run where every message carried identical text and two were queued 0.8 seconds
apart.

What that means in practice:

| Your endpoint returns | Result |
|---|---|
| `"A"`, `"A"`, `"A"`, … (never dequeues) | sent once, then quiet |
| `"A"`, `{}`, `"A"`, `"A"`, `"A"` (dequeues) | **four** sends — the first `{}` proves it dequeues, and the guard never applies again |
| `"A"`, `""`, `"B"`, `""`, `"A"` | **three** sends — the value changed in between |
| `{"id":1,"…":"A"}` then `{"id":2,"…":"A"}` | two sends |
| `{"id":1,"…":"A"}` then `{"id":1,"…":"A"}` | one send |

### How this differs from winSpark

winSpark deduped on an external id when present, and otherwise on a SHA-256 of
the content **permanently** — so a chat could never be sent the same text twice,
ever. That is safe and slightly wrong; "OK" is a reasonable thing to send twice
in a day. Rule 2 replaces the permanent content hash with a consecutive-repeat
check, and rule 3 retires even that as soon as the endpoint proves it dequeues —
which protects against the same failure without ever destroying a message.

---

## What happens to a message

Exactly what happens to an automated reply, because it is the same code path:
the same sender, the same automation lock, the same verification.

* stored in `messages` with `direction: "out"`, `origin: "relay"`, and your `id`
  in `external_ref`
* mirrored to `messages.json`
* the chat's **Last outgoing message** and **Relay (webhook poll)** status update
* logged as `relay.sent` with the strategy that delivered it

**A failed send is not recorded as relayed**, so the next poll offers it again.
An endpoint that has already dequeued it will not offer it — and that is your
call to make, not the relay's.

---

## Operational notes

**Polls run concurrently, sends do not.** Up to four chats are polled at once
because those are independent network calls; anything they produce goes to the
single worker that runs every send one at a time, so a relay message and an
automated reply can never interleave.

**A failed poll is not retried.** It runs again in a moment anyway, and retrying
inside a call that is about to repeat only multiplies load on an endpoint that
is already struggling. The failure appears as the chat's relay status and a
`relay.poll_failed` log line.

**The relay never stalls the chat poll.** It runs as its own task; a slow
endpoint delays only itself.

**Each send takes over the screen.** Like every send, it brings WhatsApp to the
foreground — see [LIMITATIONS.md](LIMITATIONS.md). A chatty endpoint means a
machine that keeps grabbing focus.

**`WEBHOOK_API_KEY` is sent on the GET too**, as `Authorization: Bearer …`, so
one credential covers both directions.
