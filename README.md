# WhatsApp Automation Manager

A bridge between WhatsApp and your webhook, running on top of
[OpenWA](https://github.com/rmyndharis/OpenWA).

It listens for OpenWA's webhook deliveries, registers every chat it sees, stores
every message in MongoDB, and — for the chats you switch on — sends back
whatever your reply function decides.

No polling. No UI Automation. No OCR. It does one thing.

---

## What changed, and why it is so much smaller

This used to drive **WhatsApp Desktop** through Windows UI Automation, because
there was no API. That worked, and it cost about 12,800 lines: finding the
window, forcing it foreground, clicking a sidebar row that `GridPattern` had
realized ten thousand pixels off-screen, filling a contenteditable div that
silently rejects `ValuePattern.SetValue`, then proving the message arrived by
counting outgoing bubbles.

All of that was *transport*. OpenWA is the transport now, so it is gone:

| Deleted | Why it existed | What replaced it |
|---|---|---|
| `wadam/whatsapp/` (~4,000 lines) | No API — drive the desktop app | An HTTP POST |
| The 3-second poll loop | The only way to know a message arrived was to look | OpenWA delivers a webhook |
| `chat_id_for(chat_name)` | The accessibility tree exposed no durable chat id, so ids were hashed from the display name — **renaming a contact created a new chat** | OpenWA's real chat id |
| `message_key_for(...)` | Content hash, because the same bubble was re-read every 3s — could not tell two people saying "ok" from one message read twice | WhatsApp's own message id |
| The outgoing queue | A UIA send took seconds, could not run concurrently, and might leave the compose box without reaching the conversation | A send is an HTTP call whose response says whether it worked |
| `uiautomation`, `pywin32`, `psutil` | Driving the desktop app | Nothing — and with them went the last reason this had to run on Windows |

---

## Quick start

You need a running OpenWA instance with a linked session, and a MongoDB you can
reach.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

First run asks for four things — your MongoDB URI, and OpenWA's address, API
key and session id — tests both connections, and writes `.env`. It never asks
again.

Then register the webhook in OpenWA, pointing at this application:

```
POST /api/sessions/<session-id>/webhooks
{ "url": "http://host.docker.internal:8765/hook",
  "events": ["message.received"],
  "secret": "<the WEBHOOK_SECRET from your .env>" }
```

### Two things that will bite you

**`localhost` inside the container is the container.** OpenWA resolves the
webhook URL from inside Docker, so a URL pointing at `http://localhost:8765`
reaches nothing. Use `host.docker.internal` — and that is also why
`WEBHOOK_HOST` defaults to `0.0.0.0` rather than `127.0.0.1`.

**OpenWA blocks internal addresses by default.** Its SSRF guard refuses to
deliver to a private range, which `host.docker.internal` resolves into. Add
`SSRF_ALLOWED_HOSTS=host.docker.internal` to OpenWA's own `.env` — the narrow
allowlist, which is what OpenWA's own source recommends over turning the guard
off.

---

## Deciding what to say

Edit [`wadam/reply.py`](wadam/reply.py). One function, message in, text or
`None` out:

```python
def reply_for(msg: InboundMessage, chat: ChatConfig) -> Optional[str]:
    if msg.text.strip().lower() == "ping":
        return "pong"
    return None
```

**Returning `None` is a success, not an error.** It is recorded and never
retried. Most messages in a live chat do not want an answer, and an endpoint
forced to invent one for every message will eventually say something stupid.

Everything else — signatures, deduplication, cooldown, loop protection,
persistence, the send — happens before this is called.

---

## The rules a message passes

1. **Valid signature.** `X-OpenWA-Signature` is `sha256=<hex>` over the raw
   body, verified before `json.loads` — re-serializing a parsed object reorders
   keys and the signature stops matching. A bad one is `401`.
2. **Inbound.** Outgoing messages are the account's own traffic; answering
   those is a loop with extra steps.
3. **Not already handled.** OpenWA retries deliveries it cannot confirm. The
   key is WhatsApp's message id with a unique index, so this survives a restart.
4. **Automation on for that chat.** The tick box in the window.
5. **Not in cooldown.** Nothing stops two automated endpoints from answering
   each other forever, so the loop is bounded.

Everything that fails a rule still answers **200**. A 4xx/5xx tells OpenWA the
delivery failed and earns a retry, and there is nothing to retry about a
message that was correctly ignored. The single exception is rule 1.

**A failed send also answers 200**, deliberately. A retry would re-run the
decision and could deliver twice. This is not theoretical: on the first live
message through this architecture, OpenWA 0.7.2 returned HTTP 500 for a message
it had *already* delivered, and a retrying client would have sent four copies.

---

## Chat ids are echoed, never rebuilt

WhatsApp's newer LID addressing means a chat is identified as
`216298915164281@lid`, and that is **not derivable from a phone number**.
Composing one from digits is sending to a guess. The id that arrived is the id
a reply goes to, verbatim — the same reason the send API refuses an ambiguous
identifier with a 409 rather than picking a chat.

---

## The window

A chat list, a transcript, and one tick box per chat. That box is the only
control in the application; everything else is a read of what happened.

Turning automation off stops replies. It does **not** delete the chat's
history — the earlier version did, which is why it had a confirmation dialog.
There is nothing destructive left to confirm.

---

## Sending messages in (optional)

The webhook is the app telling you a message arrived. The send API is the other
direction:

```bash
curl -X POST http://127.0.0.1:8766/wam/ -H "Content-Type: application/json" -d "{\"id\":\"Alice\",\"message\":\"Hello\"}"
```

Off unless `API_PORT` is set, loopback-bound by default (where a token is
optional; bind it anywhere else and `API_TOKEN` is mandatory). `id` is a chat
id, a name, or a phone number. **An identifier matching more than one chat is
refused with 409**, never delivered to a guess.

---

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The two flows, module boundaries, message lifecycle |
| [DATA.md](docs/DATA.md) | MongoDB collections and the JSON mirror |
| [UI.md](docs/UI.md) | Screen-by-screen walkthrough |
| [SEND_API.md](docs/SEND_API.md) | The inbound HTTP API |
| [OPERATIONS.md](docs/OPERATIONS.md) | Health, deployment, supported environments |
| [LIMITATIONS.md](docs/LIMITATIONS.md) | Known limitations |

---

## Tests

```bash
python -m pytest -q
```

94 tests, ~4 seconds. Storage tests run twice — against a dict-backed fake and,
when one is reachable, a real `mongod`. Doubles here have been caught lying
before.

---

## A standing caveat

OpenWA is an unofficial WhatsApp client, and connecting a number to any
automated gateway carries a real risk of the account being restricted. Use a
dedicated number, keep the pacing sane, and do not point this at strangers.
