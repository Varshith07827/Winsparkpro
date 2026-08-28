# The send API

The webhook is the application telling you a message arrived. This is the other
direction — you telling it to send one.

**It is off unless `API_PORT` is set.** Nothing listens by default.

```bash
curl -X POST http://127.0.0.1:8766/wam/ \
  -H "Content-Type: application/json" \
  -d '{"id":"Alice","message":"Hello"}'
```

```json
{"ok": true, "chat": "Alice", "chatId": "111111111111111@lid"}
```

The response is not sent until the message is: the request blocks on the HTTP
call to OpenWA, so a `200` means the gateway accepted it.

---

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `API_PORT` | *(unset)* | Empty or 0 disables the API entirely |
| `API_HOST` | `127.0.0.1` | Unreachable from other machines |
| `API_TOKEN` | *(empty)* | Optional on loopback, **mandatory** (16+ chars) otherwise |
| `API_SEND_TIMEOUT` | `60` | Seconds a send may take |

The line is drawn at reachability, not at principle. `127.0.0.1` cannot be
reached from another machine at all, so an unauthenticated listener there
exposes this machine only to itself. Bind it anywhere else and the token is the
only thing between the network and someone's WhatsApp account, so configuration
refuses to start without one.

Send the token as `Authorization: Bearer <token>` or `X-API-Token`.
Deliberately not a query parameter: those end up in logs, proxies and browser
history.

---

## Paths

Several, because callers have usually already written their integration against
one of them:

```
POST  /  /send  /wam  /wam/  /send/
GET   /health  /status
```

The message text is read from the first of `message`, `text`, `reply`, `body`.
The chat from the first of `id`, `chat`, `chat_id`, `contact`, `to`.

---

## How `id` resolves

In order:

1. **An exact chat id** (`111111111111111@lid`) is used as is.
2. **A chat name**, matched case-insensitively against chats this application
   has seen.
3. **A phone number**, matched against a chat's stored `phone_number` — which
   exists only for `<number>@c.us` chats.

If exactly one chat matches, it is used. Otherwise:

| Situation | Response |
|---|---|
| More than one match | **409** `ambiguous`, listing the candidates |
| No match, but the id contains `@` | Passed through — a chat this app has never seen is still a real chat |
| No match at all | **404** `unknown_chat` |
| Send failed | **502** `send_failed` |

**An ambiguous identifier is never guessed at.** Sending to the wrong person is
the one failure that must not happen quietly, so the request is refused and the
candidates are named so you can pick with a chat id.

The pass-through for unseen `@` ids matters: without it, the API could only
reach conversations someone had already started, which makes it useless for
starting one.

---

## What is not here

**No status endpoint.** `GET /wam/status/<id>` used to look a message up in a
durable outgoing queue. There is no queue: a send either happened by the time
the request returned, or it did not and the response says so. The endpoint
answers `501` rather than pretending.

**No media.** Text only. OpenWA exposes `send-image`, `send-document`,
`send-audio` and the rest, so adding one is a method on `OpenWAClient` plus a
branch here — not a redesign.

---

## Recorded like any other message

A message sent this way is stored against its chat with `origin: "api"`, and
the chat's preview updates. It appears in the window's transcript alongside
messages the automation sent, because from the conversation's point of view
there is no difference.
