# Known limitations and future enhancements

Written plainly, because the failure modes of desktop automation are not
obvious, and finding them out during an incident is expensive.

---

## A saved contact's phone number cannot be discovered

**This is the single most consequential limitation in the product**, because the
webhook URL is built from the phone number.

### What works

A contact who is **not** in the address book appears in WhatsApp's sidebar with
their number as the chat name. Discovery reads it, normalises it and persists
it, with no configuration and nothing to type:

```
chat name     '+91 81069 72933'
phone_number  918106972933
webhook       https://noteify.org/ntext/whook/?918106972933
```

### What does not

A **saved** contact shows only their name. Their number is not reachable
through UI Automation anywhere. Established by four read-only probes against
WhatsApp Beta `2.2630.102.0`:

| Probe | Result |
|---|---|
| Deep tree scan with the chat open, 18 levels | 37 distinct accessible names, **0 phone-shaped strings** |
| Element carrying the conversation title | **None.** The title is not an accessible element; the app derives the name from the compose box placeholder, `"Type a message to <name>"` |
| `"Open chat details for …"` button | Present on **group** bubbles only, to identify a sender. Absent in a 1:1 chat |
| Every `ButtonControl` with a real rectangle in the whole window | **Eight**: `Close`, `Minimize`, `Restore`, `New Tab` ×2, `Close tab` ×2, `Resize the chat list panel` |

That last row is the finding. The entire window exposes eight clickable
elements with geometry, and not one of them is a contact header, profile or
info affordance. There is nothing to invoke, and nothing to read.

### What the application does instead

A chat with no resolvable number is addressed by **name**, URL-encoded:

```
https://noteify.org/ntext/whook/?Novus%20Tech%20Group
```

The alternative — refusing to build a URL — meant a saved contact could never
forward anything at all, which was the behaviour before this fallback existed.
A number always wins when one is known, and the name is replaced the moment it
becomes available.

### What was deliberately NOT done

Clicking a hardcoded pixel offset where the contact header is believed to be.
It would work today and break on any layout change, and it is the same class of
assumption as the alignment threshold that once made every outgoing message
read back as incoming. A guess that happens to be right is still a guess.

### If number-based addressing is required

Four options, in the order they preserve the current architecture:

1. **Accept name-based URLs for saved contacts.** What ships today.
2. **Require unsaved-number chats** for automated conversations. Reliable, and
   restricts who can be automated.
3. **Find a non-UIA source** — WhatsApp's own local storage, or an export.
   Unexplored, and outside the accessibility layer this application is built on.
4. **The WhatsApp Business Platform.** Phone numbers are first-class there, and
   it removes the desktop dependency entirely. See
   [SENDING.md](SENDING.md), Option D.

---

## Windows UI Automation

### The window must be visible, and sending takes it over

Reading is passive — it walks the accessibility tree and needs no focus. But
**sending requires the WhatsApp window to be genuinely in the foreground**,
because the fallback rungs use coordinate clicks and keystrokes, which go to
whatever window the OS considers foreground. So a send brings WhatsApp to the
front, and if you were typing elsewhere at that moment, your keystrokes may land
in it.

Mitigations in place: sends are serialized behind one lock, the foreground change
is *verified* before anything is clicked, and the operation aborts rather than
clicking blind. There is no mitigation for the interruption itself — that is
inherent to Option A. See [SENDING.md](SENDING.md) for why B (background window
messaging) does not work against Chromium.

**WhatsApp cannot be minimised to the tray** while automation is enabled. A
minimised window is restored before a send; a closed one means no automation
until it is reopened (detected and reconnected within one cycle).

### Only what is on screen is readable

Chromium realizes the accessibility tree lazily. `GridPattern.RowCount` reports
the full logical row count, but `GetItem()` only succeeds near the current scroll
position. So:

* The poll sees the chats currently visible plus a nearby buffer — **not all 500
  of your chats**. "Rescan chats" scrolls to reach further, and runs once at
  startup.
* Reading a conversation returns its **visible tail**, not its history. A burst
  of messages arriving faster than they can be read may push earlier ones out of
  view before they are seen.
* A chat archived or far down the list is not polled until it surfaces.

### Chat identity is the chat name

WhatsApp exposes no durable identifier, so `chat_id` is a hash of the display
name. Consequences:

* **Renaming a contact or group creates a new chat here**, with a fresh
  configuration. This fails safe — the new one starts with automation OFF rather
  than inheriting a webhook meant for a different conversation — but the old
  configuration is orphaned and its history stays under the old id.
* **Two chats with identical names collide.** Rare, but real for groups.
* An unsaved number saved as a contact mid-run appears as a new chat.

### The row parser is a tuned heuristic

Chromium flattens all descendant text into one accessible Name, with no
delimiters:

```
"4 unread messages Vishnu Cr Gvp Yesterday ekada grp names navi unaye"
```

Name, timestamp and preview are separated by pattern-matching a day/time anchor.
It was tuned against rows captured from a live window, but **a chat whose name
contains a day name or a time-like substring will misparse** — "Friday Football"
would split at "Friday". The `chat_id` derived from a misparsed name is stable,
so such a chat still works consistently; its display name is just wrong.

Group detection is likewise a hint: a preview carrying a speaker prefix
(`"Chaitu: hello"`) implies a group. A one-to-one message beginning `"Re: …"`
would look like one. It is sticky once set and affects only a badge.

### Message dedup can merge true duplicates

Identity is `chat + sender + text + time label + direction`. Two genuinely
identical messages within the same minute are treated as one. Accepted
deliberately: the alternative is re-webhooking the same bubble every three
seconds forever.

### Emoji and media

* Emoji **inside** text are read (WhatsApp renders them as inline images).
* Attachments arrive as placeholders — `[Voice note · 0:12]`, `[Photo] caption`.
  The accessibility tree can name an attachment but never hand over its bytes,
  so **the webhook receives no media content**, and there is no way to send media.
* The clipboard fallback replaces the clipboard's **text** and restores it.
  Non-text contents (an image, a file selection) cannot be preserved and are
  lost during a send.

### Sending has no automated test coverage

It drives a real window with real input; a test that passed would have delivered
a message to a real person. Everything around it is tested — the ladder's
decision logic is exercised through fakes — but the UIA calls themselves are
verified by running the application. This is the single largest gap in the test
suite, and it is deliberate.

---

## Reliability boundaries

### A crash mid-webhook is not retried

If the process dies while a webhook call is in flight, the message is marked
`interrupted` and left for a person. It may already have reached the endpoint and
caused a side effect there; retrying risks a duplicate call. Losing an automatic
reply is recoverable, sending someone's customer two of them is not.

**What to do:** the chat's activity panel and `logs.json` both name it. Decide,
then reply by hand or reset the chat.

### An unverified send is reported as failed

WhatsApp clears the compose box on delivery; that empty box is the only accepted
proof. If the clear is slow beyond the poll window, a **delivered** message can
be recorded as `reply_failed`. The pipeline does not retry it, so the risk is a
false negative in the record — never a duplicate send.

### Recovery of an unsent reply requires reading the chat

On restart, an `awaiting_send` reply is verified by opening the chat and looking
for its text. If the chat cannot be read, it is left alone and retried next time
rather than sent blind.

### MongoDB is required at startup, tolerated later

A primary the application cannot reach at startup is a startup error. An outage
*during* a run is survivable: the JSON mirror keeps recording, the failure is
shown in the UI, and each operation retries. **Writes made during an outage are
not replayed into MongoDB when it returns** — they are in the mirror, and
reconciling them is a manual import.

### The mirror is capped

5,000 messages, 2,000 webhook calls, 2,000 log lines. A disaster recovery from
JSON alone restores configuration completely and history partially.

---

## The send API

**Contact IDs collide.** Four digits is 10,000 values; with a few hundred chats
a collision is likely rather than exotic. A colliding identifier is **refused**
(409) rather than delivered to a guess, so the failure mode is a send that
doesn't happen — not one that reaches the wrong person. Give one of the two
chats a longer contact ID to resolve it.

**A saved contact has no derivable ID.** WhatsApp shows saved contacts by name
and never exposes the number, so their Contact ID field starts empty and has to
be filled in once by hand. Only unsaved contacts — whose chat name *is* the
number — get one automatically.

**A 504 is ambiguous.** If a send exceeds `API_SEND_TIMEOUT` the caller gets a
504, but the send holds the automation lock and runs to its own conclusion.
Retrying after a timeout is the one way this API can produce a duplicate
message. Check the chat first.

**There is no TLS.** The listener speaks plain HTTP. Binding it publicly sends
the bearer token across the network in the clear — use a tunnel (`cloudflared`,
`ngrok`, an SSH forward) to loopback instead.

**A remote send takes over the machine.** It brings WhatsApp to the foreground
like any other send, so a request from a server elsewhere will interrupt
whatever the person at that desk is doing.

---

## The relay

**A non-dequeuing endpoint sends once.** If your URL keeps returning the same
text and has *never* reported "nothing waiting", the relay sends it a single
time and then stays quiet — it cannot distinguish "still pending" from "again".
Supply an `id`, or return `{}` when the queue is empty even once: that proves
the endpoint dequeues and retires the guard permanently for that chat.

**A message declined by the content guard is destroyed, not deferred**, if the
endpoint dequeues on read. Rule 3 removes the guard as soon as the endpoint
proves it dequeues, so this is confined to endpoints that have never once
reported empty.

**There is no per-chat relay switch.** A chat that is automated and has a
webhook is polled. If you have endpoints that handle POST but not GET, they
will see 405s — turn `RELAY_ENABLED` off, or give those chats no webhook.

**Every automated chat is polled.** Ten chats at three seconds is 200 requests a
minute against your server. `RELAY_POLL_INTERVAL` is the dial.

**A relayed send takes over the screen** like any other, so a chatty endpoint
means a machine that keeps grabbing focus.

---

## Scale

Measured on a 512-chat list: a shallow poll cycle is ~130–300 ms, comfortably
inside three seconds. The parts that grow:

* Discovery rebuilds the mirror's chat section each cycle — O(chats), a few ms.
* The chat list is delegate-painted, so hundreds of rows cost nothing to display.
* A deep rescan scrolls the real sidebar and takes seconds. It is not automatic.

Many simultaneously-automated chats are the real constraint, not chat count: each
one that changes is opened and read **serially**, and a send takes seconds. A
dozen busy automated chats will queue. The queue depth is in the status bar.

---

## Not implemented, by design

Per the product's non-goals: no AI, no OCR, no generic desktop automation, no
multi-application monitoring, no plugin architecture, no settings window, no
configurable poll interval. None of these are hidden behind a flag.

---

## Future enhancements

**Message transports.** Options B, C and D are analysed in
[SENDING.md](SENDING.md). D (WhatsApp Business Platform) is the interesting one:
it would slot in as a second transport behind the pipeline's existing
`send(chat, text) -> SendResult` contract, without touching the desktop
architecture.

**Worth doing, roughly in order of value:**

1. **Replay MongoDB writes made during an outage.** The mirror has them; a
   reconciliation pass on reconnect would close the one real gap in "MongoDB is
   the source of truth".
2. **Per-chat rate limiting.** Nothing currently stops a chatty endpoint from
   sending as fast as messages arrive.
3. **Follow chat renames.** Matching a renamed chat to its old configuration by
   fuzzy name plus message history would remove the orphaning described above.
4. **A scheduled deep rescan.** Startup-only means a chat that has been quiet
   for a long time is invisible until it surfaces.
5. **Webhook signing.** An HMAC over the payload so endpoints can verify the
   sender; currently only a static bearer token is offered.
6. **A per-chat relay switch**, for a mix of GET-capable and POST-only endpoints.
7. **TLS and per-caller tokens for the send API.** Today it is one shared
   bearer token over plain HTTP, which is why the default is loopback-only.
8. **Media awareness.** Even without bytes, passing a saved thumbnail path or a
   file reference would let endpoints react to attachments.
9. **An in-app activity log view.** The data is already collected and mirrored to
   `logs.json`; only the panel is missing.
10. **Packaging.** A PyInstaller spec and a signed installer.
