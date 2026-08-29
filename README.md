# WhatsApp Automation Manager

A bridge between WhatsApp and your webhook, running on top of
[OpenWA](https://github.com/rmyndharis/OpenWA).

It syncs your chats and contacts, POSTs each incoming message to your endpoint,
and sends back whatever you answer with. It also takes messages the other way:
`POST` a name and some text, and it goes to that chat.

No polling. No UI Automation. No OCR.

**→ [docs/GUIDE.md](docs/GUIDE.md)** — setup, commands for cmd/PowerShell/bash,
writing the endpoint, remote deployment, troubleshooting.

---

## Setup is two lines

```ini
MONGODB_URI=mongodb://localhost:27017
OPENWA_API_KEY=owa_k1_…
```

```bash
python run.py
```

Startup finds the session, generates a webhook secret, and registers its own
webhook with OpenWA. Everything else has a working default.

---

## Both directions

**In** — a message arrives in a chat you have ticked on:

```
WhatsApp → OpenWA → wadam → POST to your endpoint → its reply → sent back
```

Answer with `{"reply": "…"}`, `{"message": …}`, `{"text": …}`,
`{"data": {"reply": …}}`, a bare string, or plain text. **An empty answer means
"seen, don't answer"** — a success, never retried.

**Out** — you send, by name:

```bash
curl -X POST http://127.0.0.1:8766/wam/ -H "Content-Type: application/json" -d "{\"id\":\"Priya Menon\",\"msg\":\"Hello\"}"
```

`id` is a contact name, a phone number with its country code, or a chat id. It
reaches anyone in your address book, not only chats that have spoken. The cmd
and PowerShell forms are in [the guide](docs/GUIDE.md#sending-a-message) — they
differ more than they look.

---

## Why it is so much smaller than it was

This used to drive **WhatsApp Desktop** through Windows UI Automation, because
there was no API. That worked, and it cost about 12,800 lines: finding the
window, forcing it foreground, clicking a sidebar row that `GridPattern` had
realized ten thousand pixels off-screen, filling a contenteditable div that
silently rejects `ValuePattern.SetValue`, then proving the message arrived by
counting outgoing bubbles.

All of that was *transport*. OpenWA is the transport now:

| Deleted | Why it existed | Replaced by |
|---|---|---|
| `wadam/whatsapp/` (~4,000 lines) | No API — drive the desktop app | An HTTP POST |
| The 3-second poll loop | The only way to know a message arrived was to look | OpenWA delivers a webhook |
| `chat_id_for(chat_name)` | No durable chat id, so ids were hashed from the display name — **renaming a contact created a new chat** | OpenWA's real chat id |
| `message_key_for(...)` | Content hash, because the same bubble was re-read every 3s — could not tell two people saying "ok" from one message read twice | WhatsApp's own message id |
| The outgoing queue | A UIA send took seconds and might half-happen | A send is an HTTP call whose response says whether it worked |
| `uiautomation`, `pywin32`, `psutil` | Driving the desktop app | Nothing — and with them went the last reason this had to run on Windows |

---

## Chat ids are echoed, never rebuilt

WhatsApp's LID addressing means a chat is `111111111111111@lid`, and that is
**not derivable from a phone number**. Composing one from digits is sending to
a guess, so the id that arrived is the id a reply goes to, verbatim.

The same rule makes the send API refuse an ambiguous name with a 409 rather
than picking. Sending to the wrong person is the one failure that must not
happen quietly.

---

## Tests

```bash
python -m pytest -q
```

180 tests, about five seconds. Storage tests run twice — against a dict-backed
fake and, when one is reachable, a real `mongod`. Doubles here have been caught
lying before.

---

## A standing caveat

OpenWA is an unofficial WhatsApp client. Connecting a number to any automated
gateway carries a real risk of the account being restricted. Use a dedicated
number, keep the pacing sane, and do not point this at strangers.
