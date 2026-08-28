# Architecture

A bridge, not a bot. It does not decide anything on its own — it carries a
message to your code and carries the answer back.

```
                        ┌──────────────┐
   someone messages you │              │  reply goes back
            │           │   OpenWA     │        ▲
            ▼           │  (gateway)   │        │
        WhatsApp ──────▶│              │────────┘
                        └──────┬───────┘
                    POST       │      ▲   POST /messages/send-text
             message.received  │      │
                               ▼      │
                        ┌─────────────┴─┐
                        │     wadam     │
                        │               │
                        │  verify sig   │
                        │  store        │──▶ MongoDB + JSON mirror
                        │  POST ────────┼──▶ your endpoint
                        │  send  ◀──────┼─── its reply
                        └───────────────┘
```

There is no polling loop, no worker queue, and no automation lock. OpenWA tells
this process when a message arrives; a threaded HTTP server handles the
delivery inline.

---

## What used to be here

This application drove **WhatsApp Desktop** through Windows UI Automation,
because there was no API. That layer was about 4,000 lines and it is gone. It
is worth knowing what it did, because most of the design decisions that remain
are scar tissue from it:

- **The reader** walked the accessibility tree every three seconds and pulled
  messages out of a flattened string of name + preview + timestamp.
- **The sender** was a ladder: `InvokePattern`, then a viewport-checked
  coordinate click, then the search box — because `GridPattern` realizes rows
  below the viewport at real screen coordinates thousands of pixels down, and
  clicking one opens the wrong chat. Filling the compose box was a second
  ladder, because `ValuePattern.SetValue` silently no-ops on a contenteditable
  div.
- **The verifier** counted outgoing bubbles, because an empty compose box
  proves the text left the input, not that it reached the conversation.
- **The outgoing queue** was durable because a send took seconds, could not run
  concurrently, and might half-happen.

All of that was transport. A send is now an HTTP POST whose response says
whether it worked.

---

## Threads

| Thread | What runs on it |
|---|---|
| Qt main thread | The window. Reads snapshots; writes two things — a chat's automation flag and its webhook URL. |
| HTTP server threads | One per delivery. Verify, store, decide, send. |
| Qt timers | Session status every 10s; the directory every 5 minutes. |
| Sync thread | The directory sync, off the GUI thread — a first pass takes ~13s against a real account, and a window frozen that long looks like a crash. |

**Why concurrent deliveries are safe.** The old engine serialized everything
behind an automation lock, because two concurrent UI-Automation sends would
race for the same foreground window and put a message in the wrong
conversation. Sends are independent HTTP calls now, and OpenWA does its own
pacing. The only shared mutable state is the per-chat cooldown, which takes a
lock.

`EngineHost` is the boundary. The service calls `on_snapshot` with an immutable
`EngineSnapshot`; the host re-emits it as a Qt signal, which Qt queues onto the
GUI thread. The window never reads the repository to render and never writes to
it directly.

---

## Modules

```
wadam/
  openwa/       the transport
    client.py     send_text, session_status
    inbound.py    signature verification, delivery parsing
  engine/
    service.py    the HTTP listener, and the snapshot the window renders
    pipeline.py   what happens to one message
    webhook.py    calling your endpoint, and understanding the answer
    directory.py  syncing chats and contacts; resolving a name to a chat
    guards.py     the per-chat cooldown
    metrics.py    counters
  storage/
    repository.py MongoDB primary + JSON mirror, one API over both
    mongo.py      collections and indexes
    json_backup.py
  domain/
    models.py     one dataclass per collection
  api/            the inbound send API (optional)
  ui/             the window
```

---

## One message, end to end

1. **Signature.** `X-OpenWA-Signature` is `sha256=<hex>` over the raw body,
   checked *before* `json.loads` — re-serializing a parsed object reorders keys
   and the signature stops matching. A bad one is the only `401` this service
   returns.
2. **Parse.** Each field is read from the first of several plausible keys.
   OpenWA has moved field names between releases, and being strict about a
   shape you do not control turns someone else's rename into your outage.
3. **Find the chat.** Chats come from OpenWA's own list, synced every few
   minutes; one that arrives mid-cycle is registered on the spot. Either way
   automation starts **off** — a sync that switched chats on would begin
   answering every conversation in the account at once.
4. **Store.** Before anything is decided, so a crash leaves a record of how far
   the message got.
5. **Dispatch.** POST the message to the chat's webhook (or `DEFAULT_WEBHOOK`)
   and read the reply. An empty answer means "seen, don't answer".
6. **Cooldown**, asked last and only for a message actually about to be
   answered.
7. **Send**, and store the outgoing message against the chat.

---

## Why almost everything answers HTTP 200

A 4xx or 5xx tells OpenWA the delivery failed and earns a retry. There is
nothing to retry about a message that was correctly ignored — it would be
ignored again, three more times. So "automation is off", "that is a group",
"already handled", "still in cooldown", "no webhook configured" and "the
endpoint sent no reply" are all `200`.

The exception is a bad signature, which is the one case where repeating the
request verbatim really is wrong.

**A failed send also answers 200**, and this one is worth stating plainly: a
retry would call your endpoint again and could deliver twice. It is not theoretical.
On the first live message through this architecture, OpenWA 0.7.2 returned HTTP
500 for a message it had *already* delivered — a retrying client would have
sent four copies. A duplicate is worse than a miss, which is the same judgment
the old code made when it refused to retry an unverified send.

---

## Chat identity

WhatsApp's LID addressing means a chat is identified as
`216298915164281@lid`, and that is **not derivable from a phone number**.

The chat id that arrived is the chat id a reply goes to, verbatim. Nothing in
this codebase composes one from digits. `phone_from_chat_id` returns a number
only for a `@c.us` id and an empty string for a LID, because displaying a
plausible-looking number that belongs to nobody is worse than displaying
nothing.

The send API resolves an identifier by *lookup* and refuses an ambiguous one
with `409` rather than picking. Sending to the wrong person is the one failure
that must not happen quietly.
