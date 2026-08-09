# Architecture — two independent flows

winSpark is a **bridge**, not a bot. It does not decide anything, interpret
anything, or answer anything. Messages travel in two directions and the two
paths never touch:

```
INBOUND — collect                       OUTBOUND — deliver

  someone messages you                   your application
          │                                      │
          ▼                                      │ POST /wam/
  WhatsApp Desktop                                │ {"id","message"}
          │                                      ▼
          │ passive accessibility read     winSpark send API
          ▼                                      │
      winSpark reader                     resolve id → chat
          │                                      │
          ▼                                      ▼
   MongoDB  wa_events                    durable outgoing queue
          │                                      │
          ▼                                      ▼
  your monitoring application            WhatsApp Desktop (UI Automation)
                                                 │
                                                 ▼
                                        census verification
                                                 │
                                                 ▼
                                             VERIFIED
```

**Receiving a message never causes winSpark to send one.** The inbound side
writes to `wa_events` and stops. Whatever reads that database decides what to
do; if it wants to reply, it calls the outbound bridge like any other caller.
That is what makes a reply loop structurally impossible rather than guarded
against — an earlier version POSTed each incoming message to a webhook and sent
back whatever came, and that behaviour is gone.

---

## Inbound: WhatsApp → `wa_events`

Every three seconds the reader walks WhatsApp's accessibility tree. **It is
passive**: no mouse, no keyboard, no focus change, no foreground steal. Proven
by call graph — the poll cycle reaches only reader functions, and
`wadam/whatsapp/reader.py` contains no input call of any kind.

What is stored, per message: the **raw** text exactly as WhatsApp rendered it,
the chat, the phone number if known, direction, state and timestamps. Nothing
is summarised, classified, filtered or enriched.

Duplicate protection is a content-derived `message_key` with a unique index, so
re-reading the same visible bubble every three seconds cannot store it twice.

---

## Outbound: `POST /wam/` → WhatsApp

```bash
curl --location 'http://127.0.0.1:8765/wam/'   --header 'Content-Type: application/json'   --data '{"id":"2933","message":"Hello"}'
```

`id` resolves to a chat — by `external_id` (the last four digits of the
number), the full number, the chat id, or the chat name. An ambiguous `id` is
**refused**, never guessed: sending to the wrong person is the one failure this
must not produce quietly.

The response is `202 Accepted` with an `outgoing_id`. It means *queued*, not
*delivered* — delivery is reported by `GET /wam/status/<outgoing_id>`. The
request is bounded by the enqueue (about a millisecond), never by the send,
because a physical send takes seconds and a burst of twenty would otherwise
hold twenty connections open for minutes.

### Delivery is a bubble, not an empty box

```
resolve chat → open it if it is not already → read the BASELINE
             → send → read the chat again → count
```

Verification requires the count of matching outgoing bubbles to have **gone
up**. An empty compose box proves the text left the input; it does not prove it
reached the conversation. A pre-existing identical message can never satisfy a
new send.

A message that leaves the box but is never found is `UNVERIFIED` and is **not
retried** — retrying risks a duplicate, which is the worse failure. It is
surfaced for a person to resolve.

---

## Noteify's `/ntext/whook/` is a different system

```
https://noteify.org/ntext/whook/?<WEB_KEY><MESSAGE_ID>
```

That endpoint belongs to Noteify. winSpark does not call it. Probed live: GET,
POST and a bare request all answer
`400 Bad Request: Invalid URL format. Expected format is ?<WEB_KEY><MESSAGE_ID>`.

It is not the winSpark outbound API and must not be configured as one. The
winSpark outbound API is `POST /wam/`.

---

## The relay

An **alternative** outbound mechanism, off by default and not used by the
Noteify integration. Instead of your server pushing to `/wam/`, winSpark polls
each automated chat's `WEBHOOK_URL` with `GET` and sends whatever comes back.
It exists for deployments where nothing can reach the machine WhatsApp runs on.

It is retained and documented, not deleted — see [RELAY.md](RELAY.md) for the
response shapes and the three deduplication rules. `WEBHOOK_URL` is the relay's
poll address and **nothing else reads it** now that inbound no longer calls a
webhook.

---

## Limitations that shape the design

* **A saved contact's phone number cannot be discovered.** WhatsApp exposes it
  nowhere reachable, so such a chat has no `external_id` and cannot be
  addressed by number. See [LIMITATIONS.md](LIMITATIONS.md).
* **Sending needs an interactive desktop.** Reading works over a disconnected
  RDP session or a locked workstation; sending does not, and messages wait in
  the queue rather than being lost.
* **Identity is hashed from the display name**, so renaming a contact creates a
  new chat here.
