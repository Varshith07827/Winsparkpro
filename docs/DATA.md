# Data model

---

## Identifiers — seven, none interchangeable

`POST /wam/ {"id": "2933"}` has to reach one chat and no other, so what each of
these is — and is not — matters.

| Identifier | Source | Purpose | Unique? | Can change? | Safe to route on? |
|---|---|---|---|---|---|
| `chat_id` | sha1 of the chat **name** | primary key for a chat | yes | **yes** — renaming a contact creates a new chat | yes, within a run |
| `external_id` | last 4 digits of the number, derived at discovery | what `POST /wam/` addresses | **no** — 10,000 values | only if the number does | yes, and an ambiguous one is **refused** |
| `phone_number` | the chat name when it IS a number | identity; builds the relay URL | yes | rarely | yes |
| `chat_name` | what WhatsApp displays | display, and the sender's target | no | **yes** | as a fallback only |
| `message_key` | sha1 of chat+sender+text+time+direction | incoming dedup | yes, by construction | no | never — dedup only |
| `outgoing_id` | uuid4 per queued message | one queued send | yes, forever | no | yes |
| `correlation_id` | the incoming `message_key` or the `outgoing_id` | tracing one message through the logs | yes | no | never — diagnostics only |

Three rules worth stating plainly:

* **`external_id` is not `phone_number`.** It is the last four digits of it.
  `918106972933` has `external_id` `2933`, and a second contact ending `2933`
  collides. The resolver refuses an ambiguous id rather than picking one.
* **A number is never invented.** A saved contact exposes none, so it has an
  empty `phone_number`, an empty `external_id`, and cannot be addressed by
  number at all — only by name.
* **`chat_id` follows the display name**, because WhatsApp exposes no durable
  chat identifier. Renaming a contact makes a new chat here.

Pinned by `tests/test_identifiers.py`.


MongoDB is the source of truth. The JSON files are a mirror: every write goes to
both, MongoDB first.

---

## MongoDB collections

Database: `DATABASE_NAME` from `.env` (default `wadam`). Never taken from the
connection-string path — see [the note below](#why-the-database-name-is-not-taken-from-the-uri).

### `chat_configs`

One document per discovered chat. Created automatically on first sight, never
through a dialog.

| Field | Type | Notes |
|---|---|---|
| `chat_id` | string | **unique index.** SHA-1 of the case-folded, space-collapsed name, 24 hex chars |
| `chat_name` | string | indexed. The display name as WhatsApp renders it |
| `webhook_url` | string | `""` = no webhook. Set from `DEFAULT_WEBHOOK` for new chats |
| `external_id` | string | the contact ID the [send API](SEND_API.md) addresses this chat by — last 4 digits of the number, auto-filled when the chat name is a number |
| `automation_enabled` | bool | **always `false` on discovery** |
| `last_message_preview` | string | mirror of the sidebar row |
| `timestamp_text` | string | as WhatsApp renders it: `"12:04"`, `"Yesterday"` |
| `unread_count` | int | |
| `is_pinned` / `is_muted` / `is_group` | bool | `is_group` is sticky once detected |
| `last_poll_utc` | date | updated in bulk each cycle for every chat seen |
| `last_incoming_text` / `_sender` / `_utc` | string / string / date | |
| `last_outgoing_text` / `_utc` | string / date | |
| `last_webhook_status` | string | e.g. `"200 OK · reply"`, `"timeout"` |
| `last_webhook_response` | string | reply text, or body, or error. Truncated to 1000 |
| `last_webhook_utc` | date | |
| `webhook_retry_count` | int | attempts spent on the **most recent** call, not a lifetime total |
| `last_relay_status` / `last_relay_text` / `last_relay_utc` | string / string / date | the [relay](RELAY.md): last poll result, and the text last relayed — which doubles as the consecutive-duplicate guard |
| `relay_dequeues` | bool | set once the endpoint answers a poll with "nothing waiting", which retires the content guard for that chat |
| `messages_stored` | int | |
| `last_error` | string | |
| `seeded` | bool | `false` until the backlog has been baselined |
| `row_signature` | string | hash of the sidebar row; drives "has this chat changed?" |
| `created_at` / `updated_at` | date | |

### `messages`

Every message bubble read, in either direction.

| Field | Type | Notes |
|---|---|---|
| `message_key` | string | **unique index.** SHA-1 of chat + sender + text + time label + direction |
| `chat_id` | string | indexed with `detected_at` descending |
| `chat_name`, `sender`, `text` | string | |
| `direction` | string | `"in"` or `"out"` |
| `media_kind` | string | `photo` / `voice` / `video` / `document` / `sticker` / `gif` / `""` |
| `media_note` | string | duration, filename, or caption |
| `time_text` | string | the bubble's own clock label, e.g. `"9:21 pm"` |
| `origin` | string | for outgoing: `"webhook_reply"`, `"api"`, `"relay"`, or `""` when simply read back from WhatsApp |
| `external_ref` | string | the id a relayed message carried, when it carried one. Indexed with `chat_id` + `origin` (sparse) |
| `detected_at` | date | when we noticed it, not when it was sent |
| `status` | string | see [lifecycle](#message-lifecycle) |
| `webhook_id` | string | links to `webhooks` |
| `reply_text` | string | what the endpoint answered |
| `error` | string | |

**Two kinds of key.** A message *read* from WhatsApp is keyed by content
(`message_key_for`), because a bubble has no identity of its own. A message this
application *sends* is keyed by content **plus the moment** (`outgoing_key_for`),
because we know for certain each send is a distinct event — otherwise a
legitimately repeated message would not be recorded at all. And a sent message
that a later poll reads back is recognised as ours
(`Repository.recently_originated`) and not stored a second time.

The unique index on `message_key` is not an optimization. It is the last line of
defence for deduplication: even if two code paths race, the database refuses the
second insert.

**Why identity comes from content.** WhatsApp's accessibility tree gives a
bubble no id. Every poll re-reads the same visible tail, so identity has to be
derived from what the bubble says. The cost is that two genuinely identical
messages in the same minute collapse into one — accepted deliberately, because
the alternative is webhooking the same message every three seconds forever.

### `webhooks`

One document per webhook invocation — request, response, and how many attempts
it took.

| Field | Type | Notes |
|---|---|---|
| `webhook_id` | string | unique index |
| `chat_id` | string | indexed with `created_at` descending |
| `message_key` | string | |
| `url` | string | |
| `request` | object | the exact payload sent |
| `status_code` | int | `0` = transport failure |
| `ok` | bool | |
| `attempts` | int | 1 = succeeded first try |
| `response_body` | string | truncated to 2000 |
| `reply_text` | string | extracted reply |
| `error` | string | |
| `duration_ms` | int | |
| `created_at` | date | |

### `automation_logs`

Every operation, with enough structure to answer "why did this chat stop
replying at 14:36?" from the log alone.

| Field | Type |
|---|---|
| `log_id`, `level`, `event` | string |
| `chat_id`, `chat_name` | string |
| `message` | string |
| `direction` | `"in"` / `"out"` / `""` |
| `webhook_url`, `response`, `error` | string (truncated to 500) |
| `retry_count` | int |
| `created_at` | date, indexed descending |

Event tags: `app.started`, `chat.discovered`, `chat.seeded`, `chat.deleted`,
`chat.exported`, `automation.toggled`, `automation.global`, `automation.reset`,
`webhook.configured`, `webhook.tested`, `webhook.missing`, `webhook.no_reply`,
`webhook.failed`, `reply.sent`, `reply.failed`, `startup.scan`,
`chats.rescanned`, `poll.failed`, `job.failed`, `recovery.complete`,
`recovery.interrupted`, `recovery.resending`, `recovery.already_sent`,
`recovery.unverifiable`, `recovery.from_json`, `api.started`, `api.send`,
`api.send_failed`, `api.ambiguous_id`, `chat.contact_id`, `relay.sent`,
`relay.send_failed`, `relay.poll_failed`.

Pruned to the newest 20,000 rows, checked every 200 cycles.

### `application_state`

Exactly one document, `_id: "singleton"`.

| Field | Type |
|---|---|
| `global_automation_enabled` | bool — the last bulk action, for display |
| `version`, `started_at`, `last_shutdown_at`, `run_count`, `updated_at` | |

### `poll_state`

Exactly one document, `_id: "singleton"`. Written on the first cycle and every
tenth thereafter — it is telemetry, and the in-memory copy the UI reads is
always current.

| Field | Type |
|---|---|
| `cycle_count`, `last_cycle_ms`, `chats_seen`, `queued_chats` | int |
| `last_cycle_utc`, `updated_at` | date |
| `whatsapp_found` | bool |
| `last_error` | string |

---

## Message lifecycle

Persisted at every transition. Each state answers **"what has the outside world
already seen?"** — the only question that matters when deciding whether resuming
is safe.

```
        detected
           │
     ┌─────▼─────┐  automation off / no webhook / backlog
     │  PENDING  │────────────────────────────────▶ IGNORED · SEEDED
     └─────┬─────┘
           │ persisted BEFORE the call
     ┌─────▼────────┐
     │ DISPATCHING  │──── crash ────▶ INTERRUPTED   (never auto-retried)
     └─────┬────────┘
           │ endpoint answered
     ┌─────▼──────────┐   no reply
     │ AWAITING_SEND  │◀──────────────▶ WEBHOOK_OK · WEBHOOK_FAILED
     └─────┬──────────┘
           │ persisted BEFORE the send
     ┌─────▼─────┐
     │  REPLIED  │  or REPLY_FAILED when the compose box did not clear
     └───────────┘
```

| State | The endpoint / chat has… | On restart |
|---|---|---|
| `seeded` | nothing — pre-existing backlog | leave alone |
| `pending` | nothing — webhook provably not called | **resume** |
| `dispatching` | unknown — call was in flight | **park as `interrupted`** |
| `webhook_ok` | seen it, chose not to reply | done |
| `webhook_failed` | maybe; it gave up either way | done |
| `awaiting_send` | replied; our send unconfirmed | **verify, then send** |
| `replied` | received our reply, verified | done |
| `reply_failed` | our send provably did not land | done |
| `ignored` | nothing | leave alone |
| `interrupted` | unknown | left for a human |
| `sent` | an outgoing message we originated | — |

`dispatching` is the important one. It is written *before* the webhook call, so
a message found in that state after a crash might already have reached the
endpoint and caused a side effect there. Retrying risks a duplicate webhook
call, which the reliability requirements forbid — so it is parked, logged at
WARNING, and left for a person. **Losing an automatic reply is recoverable;
sending someone's customer two of them is not.**

---

## JSON backup format

Written to `JSON_BACKUP_FOLDER` (default `backup/`).

```
backup/
  chats.json        array of chat_configs documents
  messages.json     array of the newest 5,000 messages
  webhooks.json     { configurations: [...], calls: [...] }
  automation.json   { global_automation_enabled, updated_at, chats: [...] }
  app_state.json    { application_state: {...}, poll_state: {...} }
  logs.json         array of the newest 2,000 log entries
  settings.json     the effective .env, credentials redacted
  logs/wadam.log    the diagnostic log (rotating, 5 MB × 3)
```

Dates are ISO-8601 strings. `chats.json` is the file disaster recovery reads.

### The controlled save

Nothing is edited in place. A flush writes the whole file to a `.tmp` sibling in
the same directory, `fsync`s it, then `os.replace`s it over the target.
`os.replace` is atomic on Windows for same-volume paths, so a reader sees either
the old complete file or the new one — never a half-written file, and never a
file that vanished because the process died mid-write. The temp file is created
in the target's own directory precisely because `os.replace` is only atomic
within one volume, and `%TEMP%` may be on another drive.

Writes are coalesced over `JSON_AUTOSAVE_INTERVAL` (default 15 s) so a
three-second poll doesn't rewrite the mirror twenty times a minute. A forced
flush lands on every pipeline step, every user action, and shutdown. Setting the
interval to `0` writes through on every change.

### Caps

`messages.json` 5,000 · `webhooks.json` 2,000 calls · `logs.json` 2,000.
MongoDB keeps everything. A backup is not an archive, and an unbounded mirror
turns every flush into a multi-megabyte rewrite.

### Disaster recovery

If MongoDB has **no chats** at startup and `chats.json` does, the mirror is
loaded, written back to MongoDB, and the user is told at startup. That is the
only situation in which the mirror is read during normal operation.

---

## Why the database name is not taken from the URI

`mongodb://localhost:27017/admin` is the shape you get by habit — the auth
database really does belong in a connection string, but as `?authSource=admin`,
not as the path. Trusting the path puts application collections in MongoDB's own
`admin` database while the configured one sits empty. `DATABASE_NAME` is the
only thing that selects the database, and `admin`, `local` and `config` are
rejected at startup with an explanation.
