# Architecture

## The whole system

```
                    ┌──────────────────────────┐
                    │   WhatsApp Desktop       │
                    │   (Chromium, UIA tree)   │
                    └───────────▲──────────────┘
                                │ read (accessibility tree)
                                │ write (UIA patterns)
                    ┌───────────┴──────────────┐
                    │  STA automation thread   │  one thread, COM-safe,
                    │  wadam/whatsapp/         │  every UIA call marshalled
                    └───────────▲──────────────┘
                                │
   ┌────────────────────────────┴─────────────────────────────────────┐
   │  Automation engine — its own asyncio loop, its own OS thread     │
   │                                                                  │
   │   poll loop (3 s)              worker (one job at a time)        │
   │   ───────────────              ──────────────────────────        │
   │   find window                  open chat                         │
   │   read chat list        queue  read messages                     │
   │   discovery ───────────────▶   persist                           │
   │   read open conversation       webhook dispatch                  │
   │   detect change                outgoing send                     │
   │   enqueue                      verification                      │
   │                                                                  │
   │   relay loop (optional): GET each chat's webhook ──▶ queue        │
   └───────▲───────┬─────────────────────────────┬────────────────────┘
           │       │ every write                 │ snapshots (Qt signal)
           │  ┌────▼────────────────────┐  ┌─────▼──────────────────────┐
           │  │  Repository             │  │  UI (Qt GUI thread)        │
           │  │  ├─▶ MongoDB  (primary) │  │  ├─ chat rail              │
           │  │  └─▶ JSON     (mirror)  │  │  └─ configuration panel    │
           │  └─────────────────────────┘  └────────────────────────────┘
           │ submit(send)
   ┌───────┴──────────────────┐
   │  Send API (optional)     │◀── POST {"id","message"} from your server
   │  http.server thread pool │
   └──────────────────────────┘
```

The send API is the only inbound path. It resolves an identifier to exactly one
chat, submits a send onto the engine loop, and blocks its own request thread —
never the engine's — until the send has been verified.

## The three threads, and why

| Thread | Owns | Must never |
|---|---|---|
| **Qt GUI** | widgets, painting, user input | block on the engine or the database |
| **Engine** (asyncio) | the poll loop, the work queue, webhooks | block on UI Automation or on MongoDB |
| **STA automation** | every UI Automation / COM call | be bypassed by a call from another thread |
| **Send API** (optional) | the listening socket, one thread per request | touch the repository or WhatsApp directly |

The relay is not a fourth thread — it is a second task on the engine's loop,
because it is only network I/O and belongs where the queue is.

UI Automation is COM, and COM is not safe from arbitrary threads — hence the
dedicated STA thread. The engine needs its own loop because a three-second poll
cannot wait on a repaint, and a repaint cannot wait on a twenty-second webhook.
MongoDB calls are synchronous (pymongo), so the engine pushes them through
`asyncio.to_thread` rather than stalling its own loop.

Traffic across the boundaries is deliberately narrow:

* **engine → UI**: an immutable `EngineSnapshot`, re-emitted as a Qt signal.
  The UI never reaches into the engine or the database.
* **UI → engine**: `AutomationEngine.submit(...)` schedules a coroutine onto the
  engine loop with `run_coroutine_threadsafe`.
* **engine → WhatsApp**: only through the STA thread.

## Message flow

```
  WhatsApp                Engine                    Repository            Webhook
     │                      │                            │                   │
     │◀── read chat list ───┤ (3 s)                      │                   │
     │                      ├─ discovery ───────────────▶│ upsert + JSON     │
     │◀── read messages ────┤                            │                   │
     │                      ├─ new incoming? ───────────▶│ save   (PENDING)  │
     │                      │                            │                   │
     │                      ├──────────────────────────▶ │ mark DISPATCHING  │
     │                      ├─ POST ─────────────────────┼──────────────────▶│
     │                      │◀─ reply ───────────────────┼───────────────────┤
     │                      ├──────────────────────────▶ │ save response     │
     │                      ├──────────────────────────▶ │ mark AWAITING_SEND│
     │◀── open + fill + send┤                            │                   │
     │─── compose cleared ─▶│ verified                   │                   │
     │                      ├──────────────────────────▶ │ mark REPLIED      │
```

Every arrow into the Repository is a write to **both** MongoDB and the JSON
mirror. The two `mark` steps before irreversible actions are what makes a crash
recoverable — see [DATA.md](DATA.md#message-lifecycle) and
[LIMITATIONS.md](LIMITATIONS.md).

## Module boundaries

```
wadam/
  config.py          .env → validated Settings. The entire configuration surface.
  constants.py       Fixed values. The 3-second interval lives here and nowhere else.
  logging_setup.py   Console + rotating file.

  domain/            Data shapes and rules with no I/O.
    models.py          One dataclass per collection; MessageStatus lifecycle.

  storage/           Persistence. Knows nothing about WhatsApp.
    mongo.py           Primary: connection, indexes, collections.
    json_backup.py     Mirror: atomic controlled saves, autosave timer.
    repository.py      The facade every write goes through.

  whatsapp/          UI Automation. Knows nothing about MongoDB or webhooks.
    sta_thread.py      The COM apartment every call is marshalled onto.
    reader.py          Chat list + conversation reading.
    sender.py          Option A: open, fill, send, verify.
    row_parser.py      Flattened accessible Name → fields.
    name_rules.py      Truncation-tolerant chat-name matching.

  engine/            Orchestration. The only layer that knows about all of them.
    engine.py          Poll loop, relay loop, worker queue, recovery, commands.
    discovery.py       Automatic chat registration.
    pipeline.py        Persist → webhook → persist → send → verify.
    relay.py           GET the webhook, dedupe, send what it offers.
    webhook.py         The HTTP calls (POST and GET), shapes, retry policy.

  api/               The inbound send API. Optional; off unless a port is set.
    server.py          Transport only: HTTP, auth, JSON in and out.
    resolver.py        identifier → exactly one chat, or a refusal.
    host.py            Policy: resolution outcomes → HTTP status codes.

  ui/                Qt. Talks to the engine through snapshots and submit().
    app.py             load → validate → launch.
    main_window.py     Layout, global controls, actions.
    chat_list.py       Left rail.
    config_panel.py    Right panel.
    widgets.py         Chat-row painting (delegate).
    theme.py           Light/dark palettes and stylesheet.
    startup.py         Startup error and warning screens.
    engine_host.py     The thread bridge.
```

The dependency direction is one-way: `{ui, api} → engine → {storage, whatsapp} → domain`.
Nothing in `storage/` imports from `whatsapp/`, nothing in `whatsapp/` imports
from `storage/`, and `domain/` imports nothing of ours at all. That is what makes
the engine testable with a fake reader and a fake database, which is how most of
the test suite works.

## Design decisions worth knowing

**Two loops, not one.** Opening a chat, calling a webhook and sending a reply
take seconds. If the poll did that work it would no longer be a three-second
poll. The loop does only cheap accessibility reads; anything expensive goes to a
single worker that runs jobs one at a time — a second concurrent send is exactly
how a message ends up in the wrong conversation.

**Only automated chats are opened.** Everything else is tracked from its sidebar
row. Switching chats is visible to the user, so the application does it only
when it has a reason.

**The JSON mirror is fed from memory, not from MongoDB.** Rebuilding the mirror
by querying the primary would mean the backup only works while the primary is
healthy — backwards for a backup. A ring buffer (seeded from MongoDB at startup)
feeds it instead, so a MongoDB outage degrades to "JSON keeps recording".

**The queue is not a durable structure.** It is reconstructed at startup from
the state each message was persisted in. That is why the states exist.

**A chat's identity is its name.** WhatsApp exposes no durable chat id, so one
is hashed from the display name — with the consequences documented in
[LIMITATIONS.md](LIMITATIONS.md).
