# Data

MongoDB is the primary store. A JSON mirror is written alongside it, so the
same state is readable without a database client and recoverable if the
database is ever lost.

Database name: `wa_events` by default, overridable with `DATABASE_NAME`.
MongoDB's own databases (`admin`, `local`, `config`) are refused outright —
collections landing there are a mess to find and a worse one to clean up.

---

## Collections

### `chat_configs`

One document per chat. Created the first time a message arrives from it.

| Field | Meaning |
|---|---|
| `chat_id` | OpenWA's identifier, verbatim. Unique index. |
| `chat_name` | Display name, from the delivery's `contact.pushName`. Falls back to the id. |
| `phone_number` | Digits, only when the id is `<number>@c.us`. Empty for a LID. |
| `automation_enabled` | The tick box. Off when the chat is registered. |
| `is_group` | |
| `last_message_preview` | What the chat list renders |
| `last_incoming_*`, `last_outgoing_*` | Most recent traffic each way |
| `messages_stored` | |
| `last_error` | |
| `created_at`, `updated_at` | |

`CONFIG_FIELDS` names the two a human may edit directly in the database and
expect the running application to notice: `automation_enabled` and `chat_name`.

### `messages`

One document per message, in or out.

| Field | Meaning |
|---|---|
| `message_key` | **WhatsApp's own message id.** Unique index. |
| `chat_id`, `chat_name` | |
| `sender` | |
| `text` | Exactly as it arrived. Nothing is summarised or classified. |
| `direction` | `in` \| `out` |
| `media_kind` | `image` \| `video` \| `audio` \| `document` \| `sticker` \| `""` |
| `origin` | `reply` (answered a message) \| `api` (POSTed in) \| `""` |
| `status` | see below |
| `reply_text` | What was sent back, on the message that prompted it |
| `error` | Why a send failed |
| `detected_at` | |

### `application_state`

One document. Version, run count, start and shutdown times.

---

## Deduplication

`message_key` is WhatsApp's message id, with a unique index. OpenWA retries a
delivery it could not confirm, and the second write is refused.

This is worth contrasting with what it replaced. The old key was a **hash of
the message's content**, because the reader re-read the same visible bubble
every three seconds and would otherwise have stored it repeatedly. That scheme
could not tell two people genuinely sending "ok" a minute apart from one
message read twice — and it lived in an in-memory set, so it forgot everything
on restart. The index does not.

---

## Message lifecycle

```
     stored
       │
   PENDING ──────────────────────────────┐
       │                                 │
       ├─ automation off ────▶ COLLECTED │
       ├─ group, not enabled ▶ COLLECTED │
       ├─ no reply wanted ───▶ COLLECTED │
       ├─ in cooldown ───────▶ COLLECTED │
       │                                 │
       └─ answered ──▶ send ──┬─▶ REPLIED
                              └─▶ FAILED
```

| Status | Means |
|---|---|
| `pending` | Stored; nothing decided yet. Should never be the resting state. |
| `collected` | Stored and deliberately not answered. A decision, not a failure. |
| `replied` | Answered. |
| `failed` | The send did not happen; `error` says why. |
| `sent` | An outgoing message this application originated. |

Five states, down from eleven. The ones that went were all about a send that
might silently not have happened — `DISPATCHING`, `AWAITING_SEND` and
`INTERRUPTED` existed so a crash mid-send could be reconstructed, and `SEEDED`
marked messages already on screen when a chat was first switched on.

**A message resting at `pending` is a bug.** It means the pipeline returned
without finishing the record, which makes a deliberately-unanswered message
indistinguishable from one the process died halfway through. That happened
once, for automation-off chats, and is covered by a test now.

---

## Retired collections

`RETIRED_COLLECTIONS` names collections a previous version created and this one
drops on boot: `automation_logs`, `poll_state`, `webhooks`, `outgoing_queue`.

- `poll_state` tracked a polling loop that no longer exists.
- `webhooks` recorded per-chat outbound webhook calls. There is one webhook
  now, registered against the session in OpenWA.
- `outgoing_queue` was a durable send queue, needed when a send took seconds
  and might half-happen.
- `automation_logs` was dropped earlier, for cost: a billable write per log
  line, stored three times over.

---

## The JSON mirror

Written to `JSON_BACKUP_FOLDER` (default `backup/`), coalesced over
`JSON_AUTOSAVE_INTERVAL` seconds (`0` = write through).

| File | Contents |
|---|---|
| `chats.json` | Every chat |
| `messages.json` | Recent messages, capped at 5,000 |
| `automation.json` | Which chats are switched on |
| `logs.json` | The activity log, capped at 2,000 |
| `app_state.json` | Application state |
| `settings.json` | Effective configuration, credentials redacted |

**The activity log lives only here.** It used to be inserted into MongoDB as
well, which made every log line a billable write and stored it three times over
— the ring buffer feeds `logs.json`, and the logger writes `wadam.log`. A
diagnostic trail is worth keeping and is not worth paying a cloud provider to
keep for you.

If MongoDB starts empty but the mirror has chats, configuration is restored
from the mirror and written back to the database, and the startup screen says
so.
