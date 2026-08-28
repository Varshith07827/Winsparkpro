# Operations

## What has to be running

| | |
|---|---|
| **OpenWA** | With a linked session in `ready` state |
| **MongoDB** | Local `mongod` or Atlas |
| **This application** | GUI (`run.py`) or headless (`run_headless.py`) |

WhatsApp Desktop is **not** required, and neither is Windows. The last reason
this was Windows-only was the UI-Automation transport; it has been replaced by
HTTP calls made with the standard library.

---

## Wiring it to OpenWA

Register a webhook against the session:

```bash
curl -X POST http://localhost:2785/api/sessions/<session-id>/webhooks \
  -H "x-api-key: <openwa-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"url":"http://host.docker.internal:8765/hook",
       "events":["message.received"],
       "secret":"<WEBHOOK_SECRET from .env>",
       "retryCount":3}'
```

Two things go wrong here more than anything else.

**`localhost` inside the container is the container.** OpenWA resolves the
webhook URL from inside Docker, so a URL pointing at `http://localhost:8765`
reaches nothing and every delivery fails. Docker Desktop publishes the host as
`host.docker.internal`. That is also why `WEBHOOK_HOST` defaults to `0.0.0.0`
rather than `127.0.0.1` — a loopback listener is unreachable from the
container.

**OpenWA blocks internal addresses by default.** Its SSRF guard refuses to
deliver to a URL resolving into a private range, which `host.docker.internal`
does:

```
400  Host host.docker.internal resolves to a blocked internal address: 192.168.127.254
```

Add `SSRF_ALLOWED_HOSTS=host.docker.internal` to **OpenWA's** `.env` and
recreate its container. That is the narrow allowlist, which OpenWA's own source
recommends over `WEBHOOK_SSRF_PROTECT=false`.

Verify the wiring without sending a WhatsApp message:

```bash
curl -X POST http://localhost:2785/api/sessions/<session-id>/webhooks/<webhook-id>/test \
  -H "x-api-key: <openwa-api-key>"
```

`{"success":true,"statusCode":200}` proves the container reached the host
**and** that the signature matched — a mismatch would be a 401 and OpenWA would
report failure.

---

## Health

```bash
curl http://localhost:8765/health
```

```json
{"ok": true, "listening": true, "session": "ready",
 "mongo": "connected · wa_events",
 "deliveries": 12, "replies": 4, "send_failures": 0}
```

`deliveries` is the number to look at first. Zero means OpenWA is not reaching
this process, which is a webhook problem, not an automation one.

---

## When a ticked chat is not being answered

Work down this list; each step rules out the one above it.

1. **`deliveries` is 0** → OpenWA is not reaching the listener. Check the
   webhook URL uses `host.docker.internal`, that `SSRF_ALLOWED_HOSTS` is set,
   and that nothing else holds the port.
2. **Deliveries climb, `replies` stays 0, log says `invalid signature`** →
   `WEBHOOK_SECRET` and the webhook's `secret` in OpenWA differ.
3. **Log says `automation off for this chat`** → tick the box.
4. **Log says `endpoint sent no reply`** → your webhook answered empty. Working
   as configured.
4b. **Log says `no webhook configured for this chat`** → set one, or
   `DEFAULT_WEBHOOK`.
5. **Log says `cooldown`** → within `COOLDOWN_SECONDS` of the last reply to
   that chat.
6. **Log says `duplicate delivery`** → OpenWA retried something already
   handled. Correct behaviour.
7. **`send_failures` climbing** → the send reached OpenWA and was refused. The
   message's `error` field says why; a session that is not `ready` is the usual
   cause.

---

## Known failure modes

**A send reported as failed that actually arrived.** OpenWA 0.7.2's
whatsapp-web.js adapter dereferenced `msg.id._serialized` on a `sendMessage`
that returned `undefined`, so a delivered message came back as HTTP 500 and was
stored as `failed`. Fixed in newer OpenWA, which routes through `sendResolved`
and `toMessageResult`. If you see `failed` messages that recipients confirm
receiving, check OpenWA's version first.

This is also why a failed send is **not retried**: a retry would have sent four
copies of that message.

**A session that needs re-pairing after an OpenWA image change.** Stored auth
is tied to the browser profile. A different Chromium version in a rebuilt image
can invalidate it, and OpenWA clears the auth and asks for a QR. Pinning
`WWEBJS_WEB_VERSION` to the build a session was paired under makes this less
likely.

---

## Backup and recovery

MongoDB is the primary. The JSON mirror in `JSON_BACKUP_FOLDER` is a readable
second copy, and if MongoDB starts empty while the mirror has chats,
configuration is restored from it and written back — the startup screen says so.

The mirror is capped (5,000 messages, 2,000 log lines). It is a safety net and
a debugging aid, not an archive.

---

## Logs

`wadam.log` in the backup folder, plus stdout. `LOG_LEVEL=DEBUG` adds
per-request HTTP lines.

The in-app activity log is mirrored to `logs.json` and shown in the window. It
is deliberately not written to MongoDB — a billable write per log line, stored
three times over.

---

## Upgrading

Retired collections are dropped on boot, so moving from an older version needs
no manual migration. The four dropped so far: `automation_logs`, `poll_state`,
`webhooks`, `outgoing_queue`.

Chat documents from an older version still load — unknown keys are ignored and
missing ones take their defaults. A chat that predates the OpenWA transport
will have a name-derived `chat_id` that no longer matches anything OpenWA
sends, so it will sit inert while the real chat is registered fresh. Delete the
stale rows once you have confirmed the new ones work.
