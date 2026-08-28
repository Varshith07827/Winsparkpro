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

First run asks for your MongoDB URI and OpenWA's API key, tests both
connections, and writes `.env`. It never asks again.

That is it. On startup it finds the session, generates a webhook secret,
and registers its own webhook with OpenWA, so incoming messages arrive without
any further setup.

If you would rather register it yourself, set `REGISTER_WEBHOOK=false` and
point OpenWA at:

```
http://host.docker.internal:8765/hook     events: message.received
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

You don't — your endpoint does. An incoming message in a switched-on chat is
POSTed to that chat's webhook, and whatever comes back is sent to the chat.
What sits behind that URL — a rules engine, a language model, a person with a
keyboard — is your business and completely invisible here.

```json
{ "event": "message.received",
  "app":  { "name": "…", "version": "1.0.0" },
  "chat": { "id": "111111111111111@lid", "name": "Priya Menon",
            "phone": "919876543210", "is_group": false },
  "message": { "key": "…", "sender": "…", "text": "are you there?",
               "direction": "in", "media_kind": "", "detected_at": "…" } }
```

Answer with any of these — the endpoint should be as simple as you like:

```
{"reply": "Confirmed"}   {"message": …}   {"text": …}
{"data": {"reply": …}}   "Confirmed"      Confirmed
```

**An empty answer is a success, not an error.** `{}`, `204`, or an empty body
all mean "seen, don't answer" — recorded, never retried. Most messages in a
live chat do not want an answer.

Retries cover transport failures, 5xx and 429 with backoff. A 4xx is the
endpoint saying the request itself is wrong, so repeating it verbatim would be
noise and it fails immediately.

Set `DEFAULT_WEBHOOK` for every chat, or a per-chat URL in the window.

## The rules a message passes

1. **Valid signature.** `X-OpenWA-Signature` is `sha256=<hex>` over the raw
   body, verified before `json.loads` — re-serializing a parsed object reorders
   keys and the signature stops matching. A bad one is `401`.
2. **Inbound.** Outgoing messages are the account's own traffic; answering
   those is a loop with extra steps.
3. **Not already handled.** OpenWA retries deliveries it cannot confirm. The
   key is WhatsApp's message id with a unique index, so this survives a restart.
4. **Automation on for that chat.** The tick box in the window.
5. **A webhook to call** — the chat's own URL, or `DEFAULT_WEBHOOK`.
6. **The endpoint answered with something.**
7. **Not in cooldown.** Nothing stops two automated endpoints from answering
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
`111111111111111@lid`, and that is **not derivable from a phone number**.
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
optional; bind it anywhere else and `API_TOKEN` is mandatory — configuration
refuses to start otherwise). `id` is a chat id, a contact name, or a phone number **with its country
code**.
**An identifier matching more than one chat is refused with 409**, never
delivered to a guess.

Addressing by name is the point: WhatsApp's LID means a chat is
`111111111111111@lid`, which nobody can remember and which cannot be derived
from a phone number. wadam syncs OpenWA's chat list and address book and keeps
the mapping — so a name reaches anyone in your contacts, not only chats that
have already spoken. A bare ten-digit number is refused rather than guessed at:
India and the US both use ten national digits.

To run this on a remote machine and send to it from your own, see
[DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Threads, modules, one message end to end, and why almost everything answers 200 |
| [DATA.md](docs/DATA.md) | MongoDB collections, the message lifecycle, the JSON mirror |
| [UI.md](docs/UI.md) | The window, and what the status bar is telling you |
| [SEND_API.md](docs/SEND_API.md) | The inbound HTTP API and how `id` resolves |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running it on a remote desktop and sending to it from your own machine |
| [OPERATIONS.md](docs/OPERATIONS.md) | Wiring it to OpenWA, health, and what to check when a ticked chat is silent |
| [LIMITATIONS.md](docs/LIMITATIONS.md) | Honest boundaries |

---

## Tests

```bash
python -m pytest -q
```

165 tests, ~5 seconds. Storage tests run twice — against a dict-backed fake and,
when one is reachable, a real `mongod`. Doubles here have been caught lying
before.

---

## A standing caveat

OpenWA is an unofficial WhatsApp client, and connecting a number to any
automated gateway carries a real risk of the account being restricted. Use a
dedicated number, keep the pacing sane, and do not point this at strangers.
