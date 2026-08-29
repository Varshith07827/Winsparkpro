# Guide

Everything operational, in one file. Commands are given for **cmd**,
**PowerShell** and **bash** — they differ more than they look, and the
differences are what break first.

- [Setup](#setup)
- [Sending a message](#sending-a-message)
- [Writing the endpoint](#writing-the-endpoint)
- [The window](#the-window)
- [Running on a remote machine](#running-on-a-remote-machine)
- [When something is wrong](#when-something-is-wrong)
- [How it works](#how-it-works)
- [Limitations](#limitations)

---

## Setup

You need a running OpenWA with a linked session, and a MongoDB.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Two lines in `.env`:

```ini
MONGODB_URI=mongodb://localhost:27017
OPENWA_API_KEY=owa_k1_…
```

The API key is in OpenWA's `data/.api-key`, or on its dashboard. Then:

```bash
.venv\Scripts\python.exe run.py
```

Startup finds the session, generates a webhook secret, and registers its own
webhook with OpenWA. If OpenWA has more than one session it stops and lists
them so you can set `OPENWA_SESSION_ID`.

**One thing OpenWA needs.** Its SSRF guard blocks private addresses, and this
application registers a webhook at `host.docker.internal`. Add to **OpenWA's**
`.env` and recreate its container:

```ini
SSRF_ALLOWED_HOSTS=host.docker.internal
```

Without it registration fails, the log says so, and inbound messages never
arrive — sending still works.

### Everything else has a default

| | |
|---|---|
| `OPENWA_URL` | `http://localhost:2785` |
| `DEFAULT_WEBHOOK` | unset — messages are stored, nothing is dispatched |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` | `0.0.0.0` / `8765` |
| `COOLDOWN_SECONDS` | `60` — per-chat quiet period |
| `ANSWER_GROUPS` | `false` |
| `API_PORT` | unset — the send API is off |
| `DATABASE_NAME` | `wa_events` |

---

## Sending a message

`POST` a JSON body with an `id` and a message. `id` is a **contact name**, a
**phone number with its country code**, or a **chat id**.

Set `API_PORT=8766` in `.env` first — the send API is off until you do.

**cmd**

```bash
cd /d "C:\Users\alone\OneDrive\Desktop\OpenWA\Winsparkpro" && for /f "tokens=2 delims==" %t in ('findstr /b "API_TOKEN=" .env') do @curl -s -X POST http://127.0.0.1:8766/wam/ -H "Content-Type: application/json" -H "Authorization: Bearer %t" -d "{\"id\":\"Priya Menon\",\"msg\":\"Hello\"}"
```

Three things differ from bash and all three bite: cmd has **no `$( )`**, so the
token must come from `for /f`; **single quotes do not quote**, so the JSON needs
`\"`; and without the **`@`** the `for` echoes your token into the scrollback.

Drop `API_TOKEN` from the line entirely when the API is on loopback without a
token — which is the default.

**PowerShell**

```bash
cd "C:\Users\alone\OneDrive\Desktop\OpenWA\Winsparkpro"; $t=(Select-String '^API_TOKEN=' .env).Line.Split('=')[1]; Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8766/wam/ -Headers @{Authorization="Bearer $t"} -ContentType 'application/json' -Body '{"id":"Priya Menon","msg":"Hello"}'
```

Note `curl` in PowerShell is an alias for `Invoke-WebRequest`, which takes
different arguments — use `curl.exe` if you want the real thing.

**bash**

```bash
cd "C:/Users/alone/OneDrive/Desktop/OpenWA/Winsparkpro" && curl -s -X POST http://127.0.0.1:8766/wam/ -H "Content-Type: application/json" -H "Authorization: Bearer $(grep '^API_TOKEN=' .env | cut -d= -f2)" -d '{"id":"Priya Menon","msg":"Hello"}'
```

### What comes back

```json
{"ok": true, "chat": "Priya Menon", "chatId": "111111111111111@lid"}
```

| Status | Meaning |
|---|---|
| **401** | Missing or wrong token |
| **404** `unknown_chat` | No chat or contact matches. The message says why |
| **409** `ambiguous` | The name matches several people; they are listed |
| **502** `send_failed` | OpenWA refused. Usually a session that is not `ready` |
| **503** `busy` | More than 8 sends in flight |

### The body is lenient

`id` may also be `chat`, `chat_id`, `contact` or `to`. The text may be `msg`,
`message`, `text`, `reply` or `body`.

### Addressing, and why it refuses

A chat is `111111111111111@lid`. That is **not derivable from a phone number**,
which is why this application syncs OpenWA's chat list and address book and
keeps the mapping — so a name works.

Two refusals are deliberate:

- **A name matching two people** is a 409 listing both. On a real address book
  this is rare but not never — 4 shared names in 494 — and the alternative is
  messaging the wrong person silently.
- **A number without a country code** is refused rather than guessed. India and
  the US both use ten national digits, so `9100251854` is a real person in
  either country.

A number that has never messaged you still works: OpenWA is asked for the chat
id it would use.

---

## Writing the endpoint

When a message arrives in a switched-on chat, it is POSTed to that chat's
webhook — or `DEFAULT_WEBHOOK` — and whatever comes back is sent to the chat.
What sits behind that URL is your business and invisible here.

```json
{ "event": "message.received",
  "app":  { "name": "…", "version": "1.0.0" },
  "chat": { "id": "111111111111111@lid", "name": "Priya Menon",
            "phone": "919876543210", "is_group": false },
  "message": { "key": "…", "sender": "…", "text": "are you there?",
               "direction": "in", "media_kind": "", "detected_at": "…" } }
```

Answer with any of these:

```
{"reply": "Confirmed"}   {"message": …}   {"text": …}
{"data": {"reply": …}}   "Confirmed"      Confirmed
```

**An empty answer is a success.** `{}`, `204`, or an empty body all mean "seen,
don't answer" — recorded, never retried. Most messages do not want an answer.

**Retries** cover transport failures, 5xx and 429 with backoff. A 4xx fails
immediately: it says the request itself is wrong, and repeating it is noise.

### Two things that will break a naive endpoint

**Names are not ASCII.** A real chat here is called `𝕊𝕒𝕚 𝕍𝕒𝕣𝕤𝕙𝕚𝕥𝕙🩵` —
mathematical script plus an emoji. Printing that to a cp1252 console raises
*inside the request handler*, killing the socket before any response. The
caller sees `Remote end closed connection without response` and retries four
times. Encode explicitly, or do not print the name.

**Answer even when your handler throws.** A dropped connection is
indistinguishable from a timeout, and gets retried.

`test_endpoint.py` in the repo is a working example that gets both right:

```bash
python test_endpoint.py
```

Then set `DEFAULT_WEBHOOK=http://127.0.0.1:9000/hook`. It answers `ping`,
`time`, `quiet` and `boom` differently so each branch of the contract can be
exercised.

---

## The window

```
┌──────────────────────────┬────────────────────────────────────┐
│ search                   │ Priya Menon                        │
│──────────────────────────├────────────────────────────────────┤
│ ☑ Priya Menon   pong     │ 919876543210 · 111111111111111@lid │
│ ☐ Team chat     Are you… │ automation on                      │
│ ☑ Ravi Kumar    Send it  │ Webhook [                        ] │
│                          │ last: 200 OK                       │
│                          │────────────────────────────────────│
│                          │  ← ping                     9:21   │
│                          │  → pong                     9:21   │
├──────────────────────────┴────────────────────────────────────┤
│ session ready · 908 contacts · 12 delivered · 4 replied       │
└───────────────────────────────────────────────────────────────┘
```

Two things can be changed: a chat's **tick box** and its **webhook URL**.
Everything else is a read.

Chats are synced from OpenWA at startup and every five minutes, named from the
address book where there is one. Search matches the name, the number and the
last message — the number because a chat named in unicode art cannot be typed.

A chat arrives **unticked**. Ticking it starts answering; unticking stops. It
does not delete anything.

`0 delivered` in the status bar is the most useful number on the screen: it
means OpenWA is not reaching this process, which is a webhook problem rather
than an automation one.

### Without the window

```bash
python run_headless.py
```

No display needed. Chats must be ticked on from the window once, or in the
database.

---

## Running on a remote machine

OpenWA and this application on the same remote box; you send to it from your
own machine.

```
   your machine                       remote machine
                              ┌──────────────────────────┐
   POST /wam/  ──────────────▶│  wadam        :8766 ◀────┼── the only
   Bearer token               │    │              exposed│   open port
                              │    │ localhost:2785      │
                              │    ▼                     │
                              │  OpenWA  ──▶ WhatsApp    │
                              │    └─▶ :8765/hook        │
                              └──────────────────────────┘
```

**OpenWA's port never leaves the box.** The API key that can do anything on
that instance never crosses the network; only the send API is exposed, and it
does one thing.

Three lines differ from a local install:

```ini
API_PORT=8766
API_HOST=0.0.0.0
API_TOKEN=<32+ random characters>
```

Generate the token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Configuration **refuses to start** with `API_HOST` off loopback and a token
that is missing or under 16 characters. Off loopback that token is the only
thing between the network and your WhatsApp account.

### Prefer a tunnel to an open port

```bash
ssh -L 8766:localhost:8766 you@remote-machine
```

Then POST to `127.0.0.1:8766` as though it were local, and leave
`API_HOST=127.0.0.1` on the remote machine so the port is unreachable any other
way. Plain HTTP on an open port sends your token in a header for anything on
the path to read.

### Keeping it running

```bash
$action = New-ScheduledTaskAction -Execute "C:\path\to\.venv\Scripts\python.exe" -Argument "run_headless.py" -WorkingDirectory "C:\path\to\Winsparkpro"; Register-ScheduledTask -TaskName "wadam" -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -RunLevel Highest -User "SYSTEM"
```

It no longer needs a live desktop session. The old version drove WhatsApp
Desktop through UI Automation and needed the RDP session logged in with the
window visible — minimising it was enough to break a send.

---

## When something is wrong

### Health

**bash / cmd**

```bash
curl -s http://localhost:8765/health
```

**PowerShell**

```bash
Invoke-RestMethod http://localhost:8765/health
```

```json
{"ok": true, "listening": true, "session": "ready",
 "mongo": "connected · wa_events", "deliveries": 12, "replies": 4}
```

### A ticked chat is not being answered

Work down this list; each step rules out the one above.

| Symptom | Cause |
|---|---|
| `deliveries: 0` | OpenWA is not reaching this process. Check `SSRF_ALLOWED_HOSTS` on OpenWA, and that the registered URL is `host.docker.internal`, not `localhost` |
| log says `invalid signature` | Something else registered the webhook with a different secret. Delete it in OpenWA and restart |
| `automation off for this chat` | Tick the box |
| `no webhook configured for this chat` | Set `DEFAULT_WEBHOOK` or a per-chat URL |
| `endpoint sent no reply` | Your webhook answered empty. Working as configured |
| `cooldown` | Within `COOLDOWN_SECONDS` of the last reply to that chat |
| `duplicate delivery` | OpenWA retried something already handled. Correct |
| `send_failures` climbing | OpenWA refused the send. Check the session is `ready` |

### Two copies will share a port rather than conflict

`http.server` sets `allow_reuse_address`, so a second instance binds the same
port with **no error** and deliveries are split between the two unpredictably.
A window left running from before a code change once answered alongside a fresh
service out of the old build, and every symptom pointed at the new code.

Startup now refuses to begin when something already answers on the port. If you
see that message, find the other process:

```bash
Get-NetTCPConnection -State Listen -LocalPort 8765 | Select-Object OwningProcess
```

### Check the session

**bash**

```bash
curl -s -H "x-api-key: $(grep '^OPENWA_API_KEY=' .env | cut -d= -f2)" http://localhost:2785/api/sessions
```

**PowerShell**

```bash
$k=(Select-String '^OPENWA_API_KEY=' .env).Line.Split('=')[1]; Invoke-RestMethod http://localhost:2785/api/sessions -Headers @{'x-api-key'=$k}
```

A session at `qr_ready` needs re-pairing, and only a phone can do that.

### Logs

`wadam.log` in the backup folder, and stdout. `LOG_LEVEL=DEBUG` adds
per-request lines.

---

## How it works

```
WhatsApp ──▶ OpenWA ──POST message.received──▶ wadam ──▶ your endpoint
                ▲                                │           │
                └──── POST /messages/send-text ◀─┴───────────┘
```

No polling loop. OpenWA delivers; a threaded HTTP server handles each delivery
inline.

A message passes five checks before it is answered, and **everything that fails
one still answers HTTP 200** — a 4xx or 5xx would tell OpenWA to retry, and
there is nothing to retry about a message that was correctly ignored.

1. **Valid signature** — `sha256=<hex>` over the raw body, checked before
   `json.loads`, because re-serializing reorders keys. The only `401`.
2. **Inbound** — outgoing messages are this account's own traffic.
3. **Not already handled** — keyed on WhatsApp's message id with a unique
   index, so it survives a restart.
4. **Automation on**, and a webhook to call.
5. **Not in cooldown.**

**A failed send also answers 200.** A retry would call your endpoint again and
could deliver twice. OpenWA 0.7.2 once returned HTTP 500 for a message it had
*already* delivered; a retrying client would have sent four copies.

### Storage

MongoDB is primary, with a readable JSON mirror beside it.

| Collection | |
|---|---|
| `chat_configs` | One per chat: name, phone, automation flag, webhook URL, last webhook result |
| `messages` | Every message in and out. `message_key` is WhatsApp's own id, unique-indexed |
| `contacts` | The address book, so a name resolves even with no chat |
| `application_state` | Version, run count, the generated webhook secret |

Message statuses: `pending` → `collected` (stored, deliberately unanswered),
`replied`, `failed`, or `sent` (originated here). A message resting at
`pending` is a bug.

---

## Limitations

**Text only.** Media arrives as a `media_kind` with empty text, so a rule can
notice a photo but not read it or answer with one. OpenWA exposes the send-media
endpoints; they are simply not wired up.

**One session.** Two numbers means two copies, with different ports and
databases.

**Replies are stateless.** Your endpoint gets one message and its chat, so
anything multi-turn keeps its own state — which is the right place for it.

**No delivery confirmation.** A `200` means OpenWA accepted the message, not
that WhatsApp delivered or anyone read it.

**A failed send is not retried.** Deliberate. It is stored as `failed` with the
reason and shown in the transcript.

**The GUI has no test coverage.** The pipeline, transport, storage, config,
directory, webhook client and send API do — 180 tests, about five seconds.

**OpenWA is an unofficial client.** There is always a non-zero risk of the
account being restricted. Use a dedicated number, keep the pacing sane, and do
not point this at strangers. For anything where compliance matters, use
[Meta's Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api).
