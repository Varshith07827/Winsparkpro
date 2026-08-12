# The inbound send API

Lets another system tell this application to send a WhatsApp message.

```
your server ──POST {"id": "9423", "message": "Hello Varshith"}──▶ this app
                                                                   │
                                                  resolve id → chat │
                                                  UI Automation send│
                                                  verify delivery   │
            ◀──── 200 {"ok": true, "chat": "Varshith"} ─────────────┘
```

This is the opposite direction from the [webhook](../README.md#the-webhook-contract).
The webhook is the app telling you a message arrived; this is you telling the
app to send one. They are independent — you can use either, both, or neither.

---

## Enabling it

Off unless a port is set. In `.env`:

```ini
API_PORT=8765
API_HOST=127.0.0.1
API_TOKEN=
API_SEND_TIMEOUT=60
```

**The token is optional on loopback and mandatory off it.** The line is drawn at
reachability, not principle: `127.0.0.1` cannot be reached from another machine,
so an open listener there exposes this machine only to itself — and anything
already running as you on this machine could drive WhatsApp directly anyway.
Bind to any other address and the token is the only thing between the network
and someone's WhatsApp account, so starting without one is a startup error.

Running unauthenticated is logged at WARNING on every start, and recorded as an
`api.started` warning in the activity log. It is allowed, not hidden.

Generate a token when you need one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**The default bind address cannot be reached from another machine.** Setting
`API_HOST=0.0.0.0` is a deliberate act: without a token it is a startup error,
and with one it produces a startup warning saying what you have exposed.
If the caller is remote (a server on the internet, say), you need either a
tunnel to loopback — `cloudflared`, `ngrok`, an SSH forward — or a public bind
behind a firewall rule. **A tunnel is the better option**: the application has
no TLS, so a public bind sends your token over plain HTTP.

---

## Sending

```bash
curl -X POST http://127.0.0.1:8765/wam/ \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $API_TOKEN" \
     -d '{"id":"9423","message":"Hello Varshith"}'
```

`POST` is accepted on `/`, `/send`, `/wam` and `/wam/` — whichever your
integration already uses. The token may also be sent as `X-API-Token`.
Deliberately **not** as a query parameter: URLs end up in access logs, browser
history and error reports, and a token that sends WhatsApp messages does not
belong in any of them.

### Request

| Field | Required | Notes |
|---|---|---|
| `id` | yes | the chat. Aliases: `chat`, `chat_id`, `contact`, `to`. A bare number (`9423`, unquoted) is accepted |
| `message` | yes | the text to send. Aliases: `text`, `reply`, `body` |

### Response

```json
{
  "ok": true,
  "id": "9423",
  "chat": "Varshith",
  "chat_id": "d25187dc137f35c88bc80ec8",
  "matched_by": "phone_number",
  "strategy": "clipboard-paste + send-button-invoke",
  "message_key": "…"
}
```

**The response is not sent until the message is.** The request blocks until the
send has been verified — WhatsApp's compose box observed clearing — and only
then returns 200. That is slower than accepting and queueing, and it is the
right trade: a 200 means the message actually arrived, which is the point of the
verification everything else in this application does.

| Status | Code | Meaning |
|---|---|---|
| 200 | — | sent and verified |
| 400 | `bad_json`, `missing_id`, `missing_message` | the request is malformed |
| 401 | `unauthorized` | missing or wrong token (only when one is configured) |
| 404 | `chat_not_found` | no chat answers to that `id` |
| **409** | **`ambiguous_id`** | **two or more chats answer to it — nothing was sent** |

### Four digits collide, and that is by design

`id` is a contact's **full phone number**, or a chat's exact name. A short
four-digit form used to be the default and was removed: four digits is
10,000 values, so with enough chats two will eventually share one — the
identifier is convenient, not unique.

When that happens the API returns **409 and sends nothing**. It does not pick
one. Sending to the wrong person is the single failure this refuses to produce
quietly, and there is no ordering rule that would make a guess defensible.

The response names the colliding chats and the identifiers that are
unambiguous:

```json
{
  "ok": false,
  "code": "ambiguous_id",
  "candidates": ["+91 81069 72933", "+44 7700 902933"],
  "resolves_by": ["phone_number", "chat_id", "chat_name"]
}
```

Address such a chat by its **full phone number** or its **exact chat name**;
both are accepted by `id` and neither collides.

A caller that must use four-digit ids should treat 409 as "use a longer
identifier", not as a transient error to retry.
| 413 | `too_large` | body over 256 KB |
| 500 | `internal` | a bug here; the error text says what |
| 502 | `send_failed` | WhatsApp did not deliver it. Retry this one |
| 503 | `busy`, `engine_unavailable` | 8 sends already in flight, or the engine is down |
| 504 | `timeout` | not finished within `API_SEND_TIMEOUT`; **may still be in progress** |

`GET /health` answers without a token, and says only whether the listener is up
— no chat names, no counts describing anyone's conversations.

---

## How `id` finds a chat

The identifier is **the contact's full phone number**, and it is
filled in automatically at discovery when the chat name is the number itself —
which is how an unsaved contact appears in WhatsApp.

**A saved contact is different.** WhatsApp's sidebar shows it by name and never
exposes the number, so there is nothing to derive from. Those chats show an
empty **Contact ID** field in the configuration panel; type the four digits in
once and save.

Matching runs in tiers, most deliberate first:

1. **`phone_number`** — the full number, in any spelling
3. **`chat_id`** — the app's own 24-character identifier
4. **chat name** — exact, case-insensitive

So `{"id": "Aarav Sharma"}` and `{"id": "d25187dc…"}` both work too, if that
suits your integration better.

### Collisions are refused, never guessed

Four digits is 10,000 values. With a few hundred chats a collision is not exotic
— the birthday bound puts it above even odds at around 118 chats. When two chats
answer to the same identifier:

```json
{
  "ok": false,
  "code": "ambiguous_id",
  "error": "'9423' matches 2 chats. Set a distinct contact ID on one of them.",
  "candidates": ["Aarav Sharma", "Priya Nair"]
}
```

**Nothing is sent.** Sending a message to the wrong person is the one failure
this application must never produce quietly, and a 200 to the caller is exactly
how it would go unnoticed. The fix is to give one of the two a longer Contact ID
— the field takes any string, so full numbers or your own IDs work fine.

---

## What happens to the message

Exactly what happens to an automated reply. The same sender, the same automation
lock, the same verification, and the same persistence:

* stored in `messages` with `direction: "out"` and `origin: "api"`, so the audit
  trail distinguishes it from a webhook reply
* mirrored to `messages.json`
* the chat's **Last outgoing message** updates
* logged as `api.send` with the strategy that delivered it

A send that could not be verified stores **nothing** as sent, records the error
on the chat, and returns 502.

---

## Things to know

**It brings WhatsApp to the front.** Sending needs the window in the real
foreground — see [LIMITATIONS.md](LIMITATIONS.md). A send triggered remotely
will interrupt whatever you are doing at that machine.

**Sends are serialized.** One at a time, behind the same lock the automation
uses, so an API send and an automated reply can never interleave and land in the
wrong chat. Up to 8 requests may be in flight; the 9th gets a 503.

**A 504 does not mean it failed.** The send holds the lock and runs to its own
conclusion. Check the chat before retrying — a retry after a timeout is the one
way this API can produce a duplicate message.

**There is no TLS.** Put a tunnel or a reverse proxy in front of it if the
caller is not on the same machine.

**The token is in `.env`, which is gitignored.** It is redacted (`***`) in
`settings.json` and never logged.
