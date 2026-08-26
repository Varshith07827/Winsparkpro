# Deploying to a remote desktop

The target topology: **OpenWA and wadam both on the remote machine**, and you
send from your own machine by POSTing to wadam.

```
   your machine                          remote desktop
   ────────────                          ──────────────
                                    ┌──────────────────────────┐
   POST /wam/                       │                          │
   {"id":"Alice",       ───────────▶│  wadam        :8766 ◀────┼── the only
    "message":"…"}      Bearer tok  │    │              exposed│   open port
                                    │    │ localhost:2785      │
                                    │    ▼                     │
                                    │  OpenWA  ──▶ WhatsApp    │
                                    │    │                     │
                                    │    └─▶ :8765/hook        │
                                    │       (message.received) │
                                    └──────────────────────────┘
```

**OpenWA's port never leaves the box.** wadam reaches it on `localhost`, so the
API key that can do anything on that instance never crosses the network. Only
wadam's send API is exposed, and it can do exactly one thing: send a message to
a chat.

---

## Why this no longer needs a live desktop session

The old wadam drove WhatsApp Desktop through UI Automation, which meant the RDP
session had to stay logged in with the WhatsApp window on a real desktop. If you
disconnected, automation stopped — minimising the window was enough to break a
send.

None of that is true now. wadam talks HTTP; OpenWA runs Chromium headless in a
container. `run_headless.py` needs no display at all, so this can run as a
background service and survive you closing the RDP session.

---

## On the remote machine

### 1. OpenWA

Bring it up as usual and link a session. Its `.env` needs one line so it can
deliver webhooks to wadam on the host:

```
SSRF_ALLOWED_HOSTS=host.docker.internal
```

Its SSRF guard blocks private ranges by default, which `host.docker.internal`
resolves into. This is the narrow allowlist; prefer it to
`WEBHOOK_SSRF_PROTECT=false`.

### 2. MongoDB

A local `mongod` on the same machine, or Atlas. If local, leave
`MONGODB_URI=mongodb://localhost:27017` — there is no reason to expose it.

### 3. wadam

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

The three lines that differ from a local install:

```ini
# Reachable from your machine, not just this one.
API_PORT=8766
API_HOST=0.0.0.0
API_TOKEN=<32+ random characters>
```

Generate the token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Configuration refuses to start** if `API_HOST` is not loopback and the token
is missing or shorter than 16 characters. That check is not advisory — off
loopback, the token is the only thing between the network and your WhatsApp
account.

Everything else stays as it is locally: `OPENWA_URL=http://localhost:2785`,
`WEBHOOK_HOST=0.0.0.0`, `WEBHOOK_PORT=8765`.

### 4. Register the inbound webhook

Only needed if you want wadam to answer incoming messages. Skip it if you only
send.

```bash
curl -X POST http://localhost:2785/api/sessions/<session-id>/webhooks \
  -H "x-api-key: <openwa-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"url":"http://host.docker.internal:8765/hook",
       "events":["message.received"],
       "secret":"<WEBHOOK_SECRET from .env>",
       "retryCount":3}'
```

### 5. Run it

```bash
.venv\Scripts\python.exe run_headless.py
```

or `run.py` for the window, if you want to tick chats on and off by hand.

---

## Reaching it from your machine

### Preferred: an SSH tunnel

```bash
ssh -L 8766:localhost:8766 you@remote-desktop
```

Then POST to `http://127.0.0.1:8766/wam/` as though it were local. Nothing is
exposed to the network, and the token never crosses it in the clear.

With a tunnel you can leave `API_HOST=127.0.0.1` on the remote machine, which
is stronger still — the port is then unreachable from anywhere except through
the tunnel.

### Direct, over a VPN or a private network

```bash
curl -X POST http://<remote-host>:8766/wam/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -d '{"id":"Alice","message":"Hello"}'
```

Open port 8766 in the remote machine's firewall, to your address only if you
can.

### Not recommended: a public port over plain HTTP

The token is sent in a header, so plain HTTP exposes it to anything on the
path. If you must, put a TLS-terminating reverse proxy in front and never send
the token unencrypted.

---

## Sending

```bash
curl -X POST http://127.0.0.1:8766/wam/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_TOKEN" \
  -d '{"id":"Alice","message":"Hello"}'
```

```json
{"ok": true, "chat": "Alice", "chatId": "216298915164281@lid"}
```

`id` is a chat name, a phone number, or a chat id. **The name is the useful
part** — WhatsApp's LID addressing means a chat is identified as
`216298915164281@lid`, which nobody can remember and which cannot be derived
from a phone number. wadam keeps the mapping.

A name matching more than one chat is refused with **409** and the candidates
listed, never delivered to a guess. See [SEND_API.md](SEND_API.md).

The response is not sent until the message is, so a `200` means OpenWA accepted
it.

---

## Keeping it running

`run_headless.py` runs in the foreground and stops when its terminal closes.
For something that survives a reboot, register it as a scheduled task that runs
whether or not the user is logged on:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\path\to\Winsparkpro\.venv\Scripts\python.exe" `
                                   -Argument "run_headless.py" `
                                   -WorkingDirectory "C:\path\to\Winsparkpro"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "wadam" -Action $action -Trigger $trigger `
                       -RunLevel Highest -User "SYSTEM"
```

OpenWA's container should have `restart: unless-stopped`, which it does by
default in the shipped compose file.

---

## Checking it from your machine

```bash
curl http://127.0.0.1:8766/health          # through the tunnel
```

```json
{"ok": true, "app": "WhatsApp Desktop Automation Manager", "version": "1.0.0", "requests": 12}
```

The send API's health endpoint is unauthenticated and deliberately says
nothing about chats or messages. For the fuller picture — session state,
MongoDB, delivery counts — use wadam's own listener on the remote machine:

```bash
curl http://localhost:8765/health
```

---

## What to check when a send fails

| Response | Means |
|---|---|
| Connection refused | wadam is not running, or the firewall/tunnel is not open |
| **401** | Missing or wrong `Authorization: Bearer` |
| **404** `unknown_chat` | No chat by that name. Use a full chat id to reach one wadam has not seen |
| **409** `ambiguous` | The name matches several chats; the response lists them |
| **502** `send_failed` | wadam reached OpenWA and OpenWA refused. Usually a session that is not `ready` |
| **503** `busy` | More than 8 sends in flight |

For a `502`, check the session on the remote machine:

```bash
curl -H "x-api-key: <key>" http://localhost:2785/api/sessions
```

A session that has dropped to `qr_ready` needs re-pairing, and only a phone can
do that — see [OPERATIONS.md](OPERATIONS.md).
