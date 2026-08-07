# Operations and architecture

Developer documentation for running and maintaining the automation engine.
For *why* sending cannot be invisible, see [HEADLESS.md](HEADLESS.md); this
document assumes that finding and describes the system built around it.

---

## Architecture

```
                          WhatsApp Desktop
                                 │  UI Automation (read: free, write: no-op)
                    ┌────────────┴────────────┐
                    │   STA automation thread │   one thread, COM-safe
                    └────────────┬────────────┘
                                 │
   ┌─────────────────────────────┴──────────────────────────────────────┐
   │  Engine — own asyncio loop, own OS thread                          │
   │                                                                    │
   │   poll loop (3 s)          worker              relay loop          │
   │   ───────────────          ──────              ──────────          │
   │   discovery                scan chat           GET each webhook    │
   │   read open chat           read messages       parse + dedupe      │
   │   detect change            webhook dispatch    enqueue             │
   │   enqueue jobs             enqueue replies                         │
   │                                 │                                  │
   │                                 ▼                                  │
   │                       ┌──────────────────┐                         │
   │                       │  OUTGOING QUEUE  │  durable, per-chat order │
   │                       └────────┬─────────┘                         │
   │                                ▼                                   │
   │                      send ─▶ verify ─▶ delivered                   │
   └──────┬───────────────────────────────┬─────────────────────────────┘
          │ every write                   │ snapshots
   ┌──────▼─────────────┐          ┌──────▼──────────────┐
   │ Repository         │          │ UI (Qt thread)      │
   │ ├─▶ MongoDB        │          │ chat rail           │
   │ └─▶ JSON mirror    │          │ config · health · ops│
   └────────────────────┘          └─────────────────────┘
          ▲
          │ submit(send)
   ┌──────┴───────────┐
   │ Send API (opt.)  │◀── POST {"id","message"}
   └──────────────────┘
```

Dependency direction is one-way:
`{ui, api} → engine → {storage, whatsapp} → domain`.

---

## Lifecycles

### Message (incoming)

```
detected ─▶ PENDING ─▶ DISPATCHING ─▶ [webhook] ─▶ AWAITING_SEND ─▶ REPLIED
                │                          │
                └─▶ SEEDED / IGNORED       └─▶ WEBHOOK_OK / WEBHOOK_FAILED
```

`DISPATCHING` is written *before* the webhook call, so a crash there is known
to be ambiguous and is never retried. See [DATA.md](DATA.md#message-lifecycle).

### Outgoing message (queue)

```
                       enqueue (persisted)
                              │
                              ▼
                          QUEUED ◀──────────┐ transport failed, attempts left
                              │             │
                              ▼             │
                          SENDING ──────────┘
                              │ compose box cleared
                              ▼
                        VERIFYING
                    ┌─────────┴──────────┐
        new bubble  │                    │  no bubble within 8 s
                    ▼                    ▼
                DELIVERED           UNVERIFIED   ← never auto-retried
                                                   (a retry risks a duplicate)

    attempts exhausted ─▶ FAILED      chat deleted ─▶ CANCELLED
```

On restart:

| Found in | Meaning | Action |
|---|---|---|
| `QUEUED` | nothing was attempted | send |
| `SENDING` / `VERIFYING` | may already be on their screen | **read the chat first**, send only if absent |
| `DELIVERED` / `UNVERIFIED` / `FAILED` / `CANCELLED` | finished | leave |

Ordering is per chat, assigned at enqueue time (`sequence`), so two replies to
one conversation arrive in the order they were produced. One drainer means
sends never overlap.

### Sender

```
preflight (session)  ─▶ blocked?  ─▶ refuse with a reason, message stays queued
        │
        ▼
wait for 1.5 s of user idle  (cap 20 s)
        │
        ▼
capture foreground ─▶ open chat (if not already open)
        │                └─ pattern attempts → viewport-checked click (cursor restored)
        ▼
fill compose  ValuePattern ─▶ clipboard paste ─▶ per-character Unicode
        │      (no-op today)   (SetFocus, no cursor)
        ▼
send  InvokePattern on Send ─▶ Enter
        ▼
compose box empties  = transport success
        ▼
restore foreground ─▶ hand back to whatever was in front
```

### Session

```
                    WTSRegisterSessionNotification
                              │
     lock / unlock / RDP connect / disconnect / console attach / logoff
                              │
                              ▼
                    re-probe immediately ──▶ publish
```

`OpenInputDesktop` remains the authority — the events only stop the answer
being up to one cycle out of date. If registration fails, polling alone is
still correct, just later.

### Capability probe

```
startup ─▶ read WhatsApp package version
              │
      cached version matches? ──yes──▶ use the cached result
              │ no
              ▼
   write a probe string via ValuePattern, read it back, restore
   write a probe string via LegacyIAccessible, read it back, restore
   record which patterns are present on chat rows / the Send button
              │
              ▼
   cache against the version ─▶ adapt automatically
```

Skipped entirely if the compose box is not empty — probing must never destroy
something being typed. A WhatsApp update invalidates the cache, so the day the
provider implements `SetValue`, sending goes silent with no code change.

---

## Runtime health

Shown in the configuration panel and the status bar:

```
WhatsApp            Connected
Desktop session     Active (Default)
Session type        RDP (session 4)
UI Automation       Available
Sender              Ready
```

Operations counters, all cumulative since start, with windowed averages:

```
Messages read / queued / sent / verified      Delivery rate
Verification failures · Send failures         Queue depth
Webhook calls (failures) · Relay polls        Reconnects
Sends held (session) · Focus restores         Cursor restores
Average read / send / verification / webhook
```

Averages use a 50-sample window on purpose. A lifetime mean stops moving and
will report a healthy 900 ms while every send in the last ten minutes has taken
twelve seconds.

---

## Why UIA write patterns fail

Established through raw `IUIAutomation` COM, with the Python wrapper removed:

```
IUIAutomationValuePattern::SetValue("…")   →  S_OK, no COMError
CurrentIsReadOnly                          →  0  (writable)
CurrentValue after                         →  '\n'   unchanged
```

WhatsApp Desktop is an MSIX app (`5319275A.WhatsAppDesktop`) with a WinUI 3
shell over Chromium content. Its UIA provider maps the accessibility tree for
**reading** and leaves the write side unimplemented while still advertising it.
Full evidence, including the same result for `SelectionItemPattern.Select`,
`LegacyIAccessible`, `WM_SETTEXT` and `WM_CHAR`, in [HEADLESS.md](HEADLESS.md).

Consequence for maintainers: **never trust pattern availability as proof of
capability.** Probe it. That is what `wadam/whatsapp/capabilities.py` is for.

---

## Supported environments

| | Status |
|---|---|
| Windows 10/11, console session, unlocked | **Supported** — full read and send |
| Windows 11 over RDP, client connected | **Supported** — sends while attached |
| RDP session disconnected | **Read only.** Sends are held with a stated reason, not failed |
| Workstation locked | **Read only.** Same hold |
| Windows service / Session 0 | **Not supported.** No interactive desktop, ever |
| Headless VM with no display | **Not supported for sending.** Needs a virtual display + auto-logon |
| Multiple instances on one machine | **Not supported.** Two processes fight over the foreground |
| WhatsApp minimised | Supported — restored automatically for a send |
| WhatsApp closed | Detected; reconnects within one cycle when it returns |

---

## Deployment recommendations

1. **Dedicated machine or VM.** The one unavoidable interruption only matters if
   somebody is using that desktop.
2. **Console session, not RDP.** Or `tscon <id> /dest:console` to move an RDP
   session to the console, where it survives the client disconnecting.
3. **Disable the lock timeout and screensaver.** A locked workstation switches
   input to the `Winlogon` desktop and sending stops until it is unlocked.
4. **Do not run as a Windows service.** Session 0 isolation makes sending
   permanently impossible; run it as a logged-on user, via Task Scheduler with
   "run only when user is logged on" if it needs to start automatically.
5. **Keep MongoDB local** unless you need otherwise — the queue and mirror are
   both on the critical path for every message.
6. **Watch three numbers**: queue depth (should return to zero), verification
   failures (should be zero), and sends held (non-zero means the session keeps
   going away).

---

## Failure taxonomy

Every failure is classified, and the classes want different responses:

| Class | Log event | Meaning | Retried? |
|---|---|---|---|
| Session hold | `session.changed` | no interactive desktop | held, resumes automatically |
| Transport | `outgoing.retry` / `outgoing.failed` | never left the compose box | **yes**, to `max_attempts` |
| Verification | `outgoing.unverified` | left the box, no bubble found | **no** — a retry risks a duplicate |
| Webhook | `webhook.failed` | endpoint error | yes, per the retry policy |
| Interrupted | `recovery.interrupted` | crashed mid-webhook | **no** — may already have been delivered |
| Ambiguous send | `outgoing.recovered` / `outgoing.resuming` | crashed mid-send | verified first, then sent only if absent |

Nothing fails silently. The two "no" rows are the ones to understand: both
choose a *missed* message over a *duplicate* one, because a duplicate reaches
someone else's phone and cannot be taken back.
