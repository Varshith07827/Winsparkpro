# Release readiness

Validation results, measured performance, and an honest assessment of what is
and is not production-ready.

Everything numeric here was **measured on this machine**, not estimated.
Environment: Windows 11 Pro 26200 · Python 3.13.14 · PySide6 6.11.1 ·
pymongo 4.17.0 · MongoDB Community on `localhost:27017` ·
WhatsApp Desktop MSIX `2.2630.102.0` · RDP session 4 (console session 5).

---

## 1. Test results

```
444 passed
```

The storage-dependent suites run **twice** — once against a dict-backed fake
and once against a real `mongod` — because two doubles have now been caught
lying (`find_one` matching only the first query key; a missing `outgoing`
collection). Both times the production code was correct and the double was not.
The real-MongoDB parameter skips, rather than fails, when no server is present.

| Suite | Tests | Covers |
|---|---:|---|
| `test_validation.py` | 50 | Crash recovery ×4, verification accuracy, failure injection — **both stores** |
| `test_send_api.py` | 62 | Contact IDs, ambiguity refusal, auth, HTTP over a real socket |
| `test_relay.py` | 43 | Response shapes, three dedup rules, record accuracy, live GETs |
| `test_ui.py` | 27 | Webhook field, search, selection, validation, theming |
| `test_webhook.py` | 19 | Response shapes, empty-reply semantics, retry policy |
| `test_delivery.py` | 16 | Verification arithmetic, queue durability, restart, metrics |
| `test_recovery.py` | 13 | The interrupted-message decision table |
| `test_engine_integration.py` | 13 | Poll loop with WhatsApp faked: discovery, dedup, reconnect |
| `test_mongo_integration.py` | 12 | Real MongoDB: indexes, uniqueness, restart, pruning |
| `test_pipeline.py` | 10 | End-to-end against a real HTTP server |
| `test_discovery.py` | 9 | New chats arrive inert; settings survive rediscovery |
| `test_storage.py` | 8 | Write-to-both, atomic mirror, dedup, export |
| `test_row_parser.py` | 6 | Real sidebar rows captured from a live window |
| `test_engine_bookkeeping.py` | 4 | Per-cycle telemetry across every periodic branch |

---

## 2. Measured performance

### Latency by stage

| Operation | Mean | p50 | p95 |
|---|---:|---:|---:|
| MongoDB chat upsert | 0.4 ms | 0.4 | 0.5 |
| MongoDB message insert | 0.5 ms | 0.5 | 0.6 |
| Queue enqueue (Mongo + JSON) | 0.6 ms | 0.5 | 1.0 |
| Session preflight probe | 0.9 ms | 0.8 | 1.8 |
| Webhook POST round trip (localhost) | 7.8 ms | 1.8 | 23.7 |
| JSON mirror flush (forced, full) | 8.0 ms | 0.0 | 160.4 |
| **WhatsApp chat-list read** | **134.8 ms** | 129.8 | 213.5 |
| **WhatsApp conversation read (25 msgs)** | **2014.9 ms** | 2060.1 | 2158.3 |

### The number that matters

**A conversation read costs two seconds.** That is by far the dominant cost in
the system — 15× a chat-list read and 4,000× a MongoDB write — and it has three
consequences worth understanding before deploying:

1. **It consumes two-thirds of a three-second poll cycle** whenever the active
   chat is automated or has changed. The poll is deliberately structured to
   avoid it: the chat-list read (135 ms) happens every cycle, the conversation
   read only when there is a reason.
2. **Verification is slower than its poll interval suggests.** `SendVerifier`
   polls every 0.6 s, but each poll includes a 2 s read, so the effective
   interval is ~2.6 s and the 8 s timeout allows roughly three attempts. That
   is enough in practice — a bubble renders almost immediately — but it is
   thinner margin than the constants imply.
3. **It is why only automated chats are opened.** Scanning every changed chat
   would cost seconds per cycle.

The cause is Chromium's accessibility tree: the compose box alone appears ~15
times in a `FindAll`, and the reader walks the whole subtree and de-duplicates
by screen position. Optimising it would mean a narrower tree walk — the largest
single performance win available, and the first thing to look at if throughput
becomes a problem.

### Resource usage

See §3 — measured over a continuous soak.

---

## 3. Stability

Measured over a continuous **900-second** soak: real MongoDB, real
WhatsApp reads, stubbed sender (so nothing reached a contact), sampling every
10 seconds.

| Metric | Start | End | Peak | Drift |
|---|---:|---:|---:|---:|
| Memory (RSS) | 67.8 MB | 68.6 MB | 68.6 MB | **+0.8 MB** |
| Threads | 22 | 14 | 22 | -8 |
| Handles | 464 | 441 | — | **-23** |
| CPU | — | — | 6.4% | mean 3.45% |

**No leak evident.** Memory drifted +0.8 MB across 328 cycles;
handles and threads both *fell* from their startup peak as the deep-scan work
finished. MongoDB stayed connected throughout, the queue stayed empty, and the
JSON mirror settled at 11.7 KB.

Cadence measured 2.744 s/cycle against a 3.0 s target. The measurement
window includes startup and shutdown, so read that as ≈3 s ± 10% rather than as
evidence of drift — but it is worth re-measuring over a longer run before
treating cadence as exact.

**A 24-hour soak was not run.** What was run is a continuous soak with real
MongoDB, real WhatsApp reads and a stubbed sender, sampling RSS, threads,
handles and CPU throughout. Results are in `docs/soak.json`; the summary is in
the readiness table below. **Treat 24-hour behaviour as unverified** — the
observed window is the observed window, and extrapolating it would be exactly
the kind of estimate this document avoids.

Log growth is bounded by construction: the diagnostic log rotates at 5 MB × 3,
`automation_logs` is pruned to 20,000 rows every 200 cycles, and the JSON
mirror is capped (5,000 messages / 2,000 webhook calls / 2,000 log lines).

---

## 4. Failure injection

| Scenario | Expected | Actual | Verified by |
|---|---|---|---|
| Send fails to leave the compose box | Requeue, retry to `max_attempts`, then FAILED | As expected | `test_a_send_when_whatsapp_vanishes_is_retried_not_lost` |
| Sent but no bubble appears | UNVERIFIED, **not retried** | As expected, sent exactly once | `test_a_send_that_leaves_the_box_but_never_arrives_is_not_retried` |
| WhatsApp restarts mid-verification | UNVERIFIED, never re-sent | As expected | `test_whatsapp_restarting_during_verification_leaves_it_unverified` |
| MongoDB unavailable | JSON mirror keeps recording; queue stays deliverable | As expected | `test_mongodb_failure_does_not_lose_the_queue_from_the_mirror` |
| Webhook timeout / 500 / 429 / refused | Retried with backoff | As expected | `test_webhook_transport_failures_are_classified` |
| Webhook 400 / 404 | **Not** retried | As expected | same |
| Invalid webhook URL | Rejected before any request | As expected | `test_invalid_webhook_urls_are_rejected_before_any_request` |
| Session locked / RDP disconnected | Send held with a stated reason; reading continues | As expected | `test_a_session_without_an_input_desktop_blocks_sending_with_a_reason` |
| Crash before send | Sent once on restart | As expected | `test_crash_before_send_is_delivered_once` |
| Crash after send, before verification | **Not** re-sent — found in chat, marked delivered | As expected | `test_crash_after_send_before_verification_does_not_duplicate` |
| Crash during verification | Sent only if absent from the chat | As expected | `test_crash_during_verification_sends_only_if_absent` |
| Crash after verification, before recording | Found and marked delivered, no second copy | As expected | `test_crash_after_verification_before_marking_complete` |
| Chat deleted with messages queued | CANCELLED, not silently dropped | As expected | `test_deleting_a_chat_cancels_what_it_still_owed` |

**Not injected:** WhatsApp force-closed mid-send, Explorer restart, real RDP
disconnect, real network loss. All three would have disrupted the live session
this work was done in. Their handling is covered by equivalent simulations
above, but the real-world variants are **unverified**.

---

## 5. Verification accuracy

Every message shape passes verification and is recorded as its own message:

plain · multiline · emoji (`🚀 💖`) · long URLs with query strings ·
Unicode (Greek, Cyrillic, Japanese, Arabic) · 1,200 characters ·
leading/trailing whitespace · punctuation-heavy.

Three cases prove the census logic rather than mere presence:

* **`"OK"` ×3** — each verifies against its own new bubble; three separate
  records, not one collapsed by a content-derived key.
* **A prior identical message does not falsely verify** — two `"OK"` already in
  the chat, a send that does not land → `UNVERIFIED`, correctly.
* **An incoming `"OK"` from the other party does not verify our `"OK"`** —
  direction is part of the census.

---

## 6. Logging and observability

* Every log line carries a **`correlation_id`** — the incoming `message_key` or
  the `outgoing_id` — so one message can be traced end to end.
  `Repository.trace(id)` returns that message's whole story.
* Structured fields: timestamp, level, event, chat, direction, webhook URL,
  response, retry count, error.
* 35 distinct event tags, listed in [DATA.md](DATA.md).
* **No credential appears in any log statement** — verified by scanning every
  `logger.*` and `repo.log` call site for token/URI/password references.
* `settings.json` redacts `mongodb_uri` (password only), `api_token` and
  `webhook_api_key`.

---

## 7. Security review

| Check | Result |
|---|---|
| Secrets loaded only from `.env` | ✅ One `os.environ` read, in `config.py`, the documented override |
| Secrets in logs | ✅ None — scan of every log call site is clean |
| Secrets in the JSON mirror | ✅ Redacted in `settings.json`; `.env` is gitignored |
| Webhook URL validation | ✅ Scheme, host and whitespace checked before any request |
| Send API authentication | ✅ Optional on loopback, **mandatory** off it; startup error otherwise |
| Token comparison | ✅ `hmac.compare_digest` |
| Token in URLs | ✅ Header only — never a query parameter |
| Request body limits | ✅ 256 KB, rejected before reading |
| MongoDB failure handling | ✅ Startup error with a specific cause; runtime outage degrades to the mirror |
| JSON mirror corrupting state | ✅ Atomic `os.replace`; a malformed file is logged and ignored, never partially applied |
| Repo contains no secrets | ✅ Verified before the first push; `.env` and `data/` excluded |

---

## 8. Production readiness checklist

| Area | Status | Notes |
|---|---|---|
| **Reliability** | | |
| No duplicate messages | ✅ | Proven across all four crash points, both stores |
| No lost messages | ✅ | Durable queue; `test_nothing_is_lost_across_a_restart` |
| Retry policy | ✅ | Applies to every path now that API sends are queued (§13) |
| Per-chat ordering | ✅ | Sequence assigned at enqueue |
| **Recovery** | | |
| Crash before/during/after send | ✅ | Four enumerated scenarios, both stores |
| WhatsApp restart | ✅ | Reconnects within one cycle |
| MongoDB outage | ⚠️ | Survives; **writes made during an outage are not replayed** on reconnect |
| Session lock / RDP disconnect | ✅ | Held with a reason, resumes on the session event |
| Explorer restart | ⚠️ | Not tested — would have disrupted the live session |
| **Performance** | | |
| Poll cadence | ✅ | Measured; conversation read is the dominant cost |
| Latency by stage | ✅ | §2 |
| Memory / handles / threads | ✅ | 15-min soak: +0.8 MB, handles -23. Raw data in `docs/soak.json` |
| 24-hour stability | ❌ | **Not run.** 15-minute soak only — see §17 |
| **Security** | ✅ | §7 |
| **Testing** | | |
| Unit + integration | ✅ | 313 tests |
| Real MongoDB | ✅ | Storage suites run against both stores |
| Real WhatsApp reads | ✅ | Live probes and benchmarks |
| **Real WhatsApp send end-to-end** | ✅ | §17: two-send diagnostic BOTH_MESSAGES_OBSERVED, each send one bubble |
| **Inbound collector** | ✅ | §16: 2 bubbles → 2 reader → 2 keys → 2 records, re-poll adds 0 |
| **RDP disconnect (real)** | ❌ | Simulated only |
| **Documentation** | ✅ | Ten documents; see the README index |
| **Deployment** | ⚠️ | No installer or packaging; run from source |
| **Send API path** | ✅ | Queue-backed since §13; 20-message burst accepted in 11s, 19/20 verified |
| **Monitoring** | ✅ | Operations card, 19 metrics, session health |
| **Observability** | ✅ | Correlation IDs, structured logs, `trace()`, all 13 metrics wired |

---

## 9. Known limitations

Separated by whose limitation it actually is, because the answer changes what
could ever be done about it.

### Application limitations (fixable here)

* **Six of thirteen metrics were never wired** until this validation pass —
  they read zero regardless of activity. Fixed, with a test that fails if a
  counter is ever defined and not recorded. Worth noting as a reminder that
  *observability needs its own tests*: the dashboard was lying and nothing
  caught it.

* **MongoDB writes made during an outage are not replayed** when it comes back.
  They are in the JSON mirror; reconciling is manual.
* **No installer.** Runs from source with a venv.
* **The end-to-end send path has never run against a real chat** since the
  queue and verifier were introduced. The riskiest untested assumption is that
  verification can read the conversation back while the chat is still open
  after a send.
* **Conversation reads cost ~2 s.** A narrower tree walk is the obvious win.
* **No per-chat relay switch** — the relay follows the automation toggle.
* **Single instance only.** Two processes would fight over the foreground.

### WhatsApp Desktop limitations (not fixable here)

* **`ValuePattern.SetValue` returns `S_OK` and discards the call**, with
  `IsReadOnly = False`. Same for `LegacyIAccessible.SetValue`,
  `SelectionItemPattern.Select` and `DoDefaultAction`. Verified at the raw COM
  layer. **This is the single reason sending cannot be invisible.**
* **No durable chat identifier** — identity is hashed from the display name, so
  renaming a contact creates a new chat here.
* **One HWND for all content**, so window messaging has nothing to address.
* **No DevTools port**, and it cannot be enabled externally.
* **Media bytes are unreachable** — attachments arrive as placeholders.
* **Only realized rows are readable** — the poll sees the visible window of the
  chat list, not all of it.

### Windows platform limitations (security boundaries — do not work around)

* **Keystroke injection requires an attached input desktop.** A disconnected
  RDP session or locked workstation cannot send; `SendInput` returns success
  and the events go nowhere.
* **Session 0 isolation** means a Windows service can never do this.
* **Foreground activation is restricted** from background threads.

### UI Automation limitations

* **Pattern availability is not capability.** A provider may advertise a
  pattern and implement nothing. This is the most important lesson in the
  codebase and the reason `capabilities.py` probes rather than assumes.
* **No write-capable text pattern.** `TextPattern` is read-only by design;
  `ValuePattern` is the only write path and it is the one being discarded.

---

## 10. Recommendation

**See §17 for the current verdict.** This section is kept as written because
its history is the useful part: it has been wrong twice, in both directions,
and each correction came from evidence rather than argument. It first
recommended supervised production before the send path had ever run against a
real chat. §11 corrected it to "not ready" when that test delivered 14 of ~114
messages. §12 found and fixed the cause. Defect 1 of §11 — the Send API
bypassing the durable queue — was closed in §13.

If genuinely unattended, invisible operation is a hard requirement, the desktop
path cannot deliver it and the WhatsApp Business Platform is the correct
architecture — see [SENDING.md](SENDING.md), Option D.

---

## 11. End-to-end send failure (2026-08-07)

The first real exercise of the send path. ~114 messages posted to the Send API
for one chat across two runs; **14 arrived**. Recorded here in full because §10 previously
called the system ready on the strength of tests that never touched this path.

### Defect 1 — the Send API bypasses the queue and the verifier

`DeliveryService.enqueue` is called from exactly two places, `pipeline.py:170`
(webhook replies) and `relay.py:142`. The Send API path — `api/host.py:97` →
`Engine.send_message` (`engine.py:862`) — calls `self._sender.send_async`
**directly**. Consequences, all observed:

* `data/outgoing.json` stayed `[]` through 141 sends; no `outgoing.*` event was
  ever logged. The durable queue was not involved.
* No verification ran. `api.send` is logged on *transport* success — the compose
  box emptying — which is not proof the message appeared in the conversation.
* No retry. A failure returns 500 to the caller and the message is gone.
* No `UNVERIFIED` state, no per-chat ordering, no crash recovery on this path.

The docstring on `Engine.send_message` claims the opposite: *"the same sender,
the same action lock and the same verification as an automated reply."* The
action lock is real; the verification is not. **The docstring is wrong and the
hardening sprint's guarantees do not apply to API sends.**

### Defect 2 — the compose fill is intermittently lost

Reproduced away from the API, filling and clearing the box in a loop with no
Enter pressed: **8/12, 9/15, 7/15, 5/14, 8/14 filled**. Roughly half of all
attempts fail with `paste did not verify`, then fail again on the
per-character fallback. This is the proximate cause of the send failures.

Root cause **not identified.** Two hypotheses were tested and refuted:

| Hypothesis | Test | Result |
|---|---|---|
| Foreground handover per send disturbs input | A/B, same fill path, only the take/restore differing | **Refuted** — 9/15 churn vs 7/15 held |
| The contenteditable needs a physical click to place the DOM caret | UIA SetFocus vs real click vs SetFocus after a click | **Refuted** — all three pasted correctly |
| A physical mouse click into the box before filling ("mouse hijack") | Interleaved A/B, cursor restored after each click | **Does not fix it** — 8/14 with the click vs 6/14 without |
| The 3 s poll loop's ~2 s conversation read races the send | Stopped the app entirely, re-ran the same fill loop | **Refuted** — fills went to **0/14**, worse, not better |
| Keystrokes need the WebView2 content window foregrounded, not the WinUI shell | Foregrounded hwnd of the `Chrome_WidgetWin_1` content host | **Refuted** — 0/10, foreground confirmed correct each time |

The fill rate is **environment-dependent in a way none of these explain**: it sat
near 50% while the app was running and dropped to 0% with the app stopped, with
`GetForegroundWindow` confirming WhatsApp in front on every failing attempt.
That dependency is itself unexplained and makes every percentage above a
measurement of an uncontrolled system, not of the variable under test.

One hypothesis is **open and unproven**: `set_compose_text_sync` always tries
rung 1 (`_try_value_pattern`) first, even though the capability probe has
already recorded `value_pattern_write: false` for this build. Skipping it
scored 8/14 against 5/14 — directionally better, but a 3-count gap at n=14 is
not evidence, and ~50% of attempts still failed in the better arm. **The
majority of the failure is still unexplained.**

### Defect 3 — the capability probe is computed and never used

`data/capabilities.json` records `value_pattern_write: false`, yet
`sender.py:474` calls `_try_value_pattern` on every send regardless. The probe
exists to stop exactly this, and nothing consults it.

### Observation — the title hint matches four windows

`find_window_sync("WhatsApp")` resolved correctly here, but four top-level
windows match: WhatsApp Beta (the target), its WebView2 content host, a Chrome
window, and **wadam's own Qt window**. Matching on title alone is fragile;
the target should be pinned by process and window class.

### A false-negative scare that was not one

An earlier reading of this data appeared to show messages logged as
`send_failed` sitting in the chat — which would have meant the app reports
failure for delivered messages, the worst possible defect for a retry policy.
It was an artifact: the burst was run twice with identical text, so
`Hello Varshith #1` was present from the *first* run while the second genuinely
failed. Matching on message text alone cannot tell those apart. **No
false-negative was demonstrated**; 15 outgoing messages are readable in the
chat against 14 logged as sent, which is consistent. Recorded because the
verifier's census logic exists precisely because identical text is
indistinguishable, and the analysis fell into the trap the code avoids.

### Operational note

A `for /L` loop of 100 curl requests kept running after the operator pressed
`^C` and had to be killed by PID. Each iteration takes ~10 s because the sender
waits for an idle desktop, so a cancelled burst keeps sending for minutes. The
API should expose a way to cancel queued work for a chat.

---

## 12. Root cause and fix (2026-08-07)

**The compose box was focused but never clicked.**

`SetFocus` makes an element the focused UIA element — `HasKeyboardFocus`
returns True, `GetForegroundWindow` returns WhatsApp, every call reports
success — while a Chromium contenteditable still has **no insertion point**.
Pasted and typed characters are discarded. The failure is invisible at every
layer except the readback.

### How it got in

The headless refactor removed `compose.Click(simulateMove=False)` from the fill
path, on the strength of one live measurement where SetFocus alone appeared
sufficient, recorded in the docstring as *"The click that used to precede every
keystroke was unnecessary."*

That measurement was taken **after a chat switch**. Switching chats clicks a
sidebar row, which leaves WhatsApp with a caret already in the compose box — so
it measured a caret it had not created. Every send in that session followed a
chat switch. The moment sends went to an already-open chat, nothing placed a
caret and roughly half of them silently failed.

The reference implementation
(`winspark/connectors/whatsapp_group_sender.py`) had already found this and
says so in two separate docstrings: *"SetFocus alone was not enough … A
physical click is what actually places the caret inside the contenteditable;
SetFocus focuses the element without a caret and Enter is ignored."* The
regression was reintroducing a bug the reference had already fixed.

### Measured

Interleaved A/B on a live window, identical fill path, no Enter pressed:

| Arm | Filled |
|---|---|
| SetFocus only (the regression) | 10/14 |
| **SetFocus + Click (restored)** | **14/14** |

Baseline before the capability gate was added was worse still — ~6/14.
Two changes contribute:

1. **`focus_compose_caret`** — SetFocus, then `Click(simulateMove=False)`, with
   the cursor put back where the user left it. Took 10/14 → 14/14.
2. **The capability probe is now consulted.** `capabilities.json` recorded
   `value_pattern_write: false` and nothing read it, so every send spent 0.4s
   re-attempting a write this build discards — immediately before the paste.
   Gating it took ~6/14 → 10/14.

Longer confirmation across plain, long, emoji, multiline and Unicode text:
**21/25**, the remainder recovering through the per-character fallback or a
foreground failure unrelated to the caret.

### End-to-end

Five messages through the Send API — the path that had failed ~100 consecutive
times — all five returned `ok: true` and **all five were independently read
back from the conversation**, in order, no duplicates.

### Guarded

`tests/test_compose_caret.py` fails if the click is removed again, if it is
ordered before the focus, or if the engine stops passing the probe result to
the sender. The docstring on `focus_compose_caret` records why the click is
there and what the flawed measurement was, so the next person to "optimise"
it has to argue with the evidence first.

### Still open

* Defect 1 of §11 — API sends bypass the durable queue and the verifier. The
  fix above makes the transport reliable; it does not give that path retry,
  verification, ordering or crash recovery.
* The `WhatsApp` title hint still matches four windows, including this app's
  own.

---

## 13. Queue-backed API sends (2026-08-07)

Closes defect 1 of §11.

### The arithmetic that made blocking untenable

The endpoint held the HTTP request open until WhatsApp physically sent. At
~17s per send, a burst of twenty needs almost six minutes, so every caller past
the third received:

```
{"ok": false, "code": "timeout",
 "error": "The send did not complete within 60s..."}
```

**for messages that were in fact delivered.** A caller that retried on timeout
would have duplicated real messages — the failure this system works hardest to
avoid. Confirmed live: 19 of 20 requests timed out and all 20 messages arrived.

### The change

A request is now bounded by the **enqueue**, not by the send. `POST` returns
`202 Accepted` with an `outgoing_id`; the message goes into the same durable
queue the webhook and relay paths use, and the single drainer delivers it with
per-chat ordering, retry, and census verification. Delivery is reported by
`GET /wam/status/<outgoing_id>`.

`ok: true` now means **accepted**, not delivered. That is a deliberate contract
change: the previous response could not honestly mean "delivered" either, since
it timed out on messages that had been sent.

### Measured, same 20-message burst

| | Before | After |
|---|---|---|
| HTTP responses | 1 × 200, **19 × 504 timeout** | **20 × 202** |
| Time to accept all 20 | >19 minutes | **11 seconds** |
| Per-request latency | 60s (timeout) | 36–144 ms |
| Delivered | all 20, reported as failures | **19 verified, 1 unverified** |
| Lost | 0 | 0 |
| Retry / ordering / verification | none | all three |

The one `unverified` is the design working, not a failure: its pre-send census
could not be read, so a new bubble could not be told apart from one already
present. It reports that reason verbatim through the status endpoint and is
**not** retried, because a retry there risks a duplicate.

### Send latency

Two phases were cut:

* `chat_already_open` ran an expensive header scan (~2.5s) even when the
  active-conversation read had already answered the question. It now only falls
  back to the header when no name came back at all — which is the case it was
  written for (read-only groups have no compose box to name).
* Clearing the compose box took the foreground and clicked, even when the box
  was already empty — the common case straight after a send.

Send **including verification** now averages **10.8s** (min 9.0, max 12.4)
against a previous **17.0s** cadence that did no verification at all.

### Still open

* `find_window_sync` matches four top-level windows by title, including this
  application's own. It resolves correctly today by ordering, not by design.
* Filling the compose box (~1.5s) and confirming the box emptied (~1.6s) are
  now the floor. Both are Chromium accessibility-tree walks; a narrower walk is
  the remaining win.

---

## 14. Draining a backlog as one run (2026-08-08)

§13 made bursts safe. They were still slow: a queue of twenty took **329
seconds**, because delivery paid the full per-message setup twenty times.

### What each message was paying for

Measured inside a live drain, per send:

| Phase | Before |
|---|---:|
| Find the chat row | 3.5 s |
| Open the chat | 0.3 s |
| Read the active conversation | 0.8 s |
| Fill the compose box | 4.9 s |
| Confirm the box emptied | 1.6 s |

Plus, per message: a session probe, a wait for the desktop to go quiet, a
foreground change and its restore, a pre-send census read and a post-send
verification read.

### Three changes

1. **One run, not twenty.** `WhatsAppSender.batch()` holds the action lock, the
   quiet moment and the foreground for the whole drain, and
   `DeliveryService.deliver_batch` takes **one** census read before a chat's
   messages and **one** verification read after them. The desktop is
   interrupted once per burst instead of once per message.
2. **Don't re-find a chat that never closed.** Consecutive messages to one chat
   skip finding and opening the row. The conversation's own name is still read
   back before anything is typed — a mismatch falls through to the full path,
   because a message typed into the wrong conversation is the one failure worth
   paying a second to avoid.
3. **Stop polling while draining.** The 3-second poll reads the open
   conversation (~2 s) on the same STA thread, so it was not just delaying the
   poll — it was delaying every message queued behind it. This was the single
   biggest cost: the already-open guard measured **4.6 s** with polling active
   and **0.4 s** without.

### Measured, 20 messages to one chat, queue cleared first

| | Per message | 20 messages |
|---|---:|---:|
| Blocking, unqueued (§11) | ~17 s | ~340 s, 19 reported as timeouts |
| Queued, one at a time (§13) | 16.4 s | 329 s |
| Batched | 8.3 s | 213 s |
| Batched + open-chat reuse | 8.2 s | 216 s |
| **+ polling paused while draining** | **5.2 s** | **104 s** |

Send phases after: `guard=0.4s fill=1.55s confirm=1.6s` — **3.6 s of actual
send**, against 11 s before.

**20/20 delivered and verified**, in order, no duplicates, in all runs.

### What did not change

Every guarantee. `tests/test_batch_delivery.py` pins them: per-chat ordering,
interleaving preserved across chats (grouping is by *consecutive* chat, since
sorting the queue would be faster still and would break the ordering promise),
transport failures retried without stopping the batch, an unreadable
conversation marking the whole batch UNVERIFIED rather than guessing, and — the
one batching could most easily get wrong — three identical messages each
requiring their own new bubble, with a batch that half-lands marking exactly
the missing ones.

### Note on the poll pause

Nothing is missed. Incoming messages are picked up on the first cycle after the
queue empties. The trade is deliberate: while there is a backlog to send,
sending it is more urgent than noticing new arrivals a few seconds sooner.

---

## 15. Final audit (2026-08-09)

A full current-state audit against the simplified product definition, then two
release blockers, then an attempted end-to-end test that went wrong and taught
more than a clean run would have.

### Defects found and fixed

| # | Defect | Severity | Evidence |
|---|---|---|---|
| 1 | **Our own messages read as INCOMING** | CRITICAL | Outgoing bubbles at `center_x=1005` against a threshold of `1154` — 60% of the *window*, which includes a 570px sidebar. The definitive `"You:"` label was on 2 of 100 bubbles. Misclassified sends are stored as incoming, **posted to the webhook, and the endpoint's answer sent back** — the app answering its own answer. It also defeated the loop guard, which only ran for messages already believed outgoing. |
| 2 | **The packaged EXE destroyed its own backup on every quit** | CRITICAL | `PROJECT_ROOT` derives from `__file__`, which in a one-file build lives under `sys._MEIPASS` — deleted on exit. Two orphaned `_MEI*/backup/` folders found, each a full mirror. |
| 3 | **Sender name and number leaked into the message body** | CRITICAL | A partially saved contact's message reached MongoDB and the webhook as `"Pritam +91 63032 31690 Ok mam"` when the message was `"Ok mam"`. Violates the raw-content rule outright. |
| 4 | **A drain could starve message discovery forever** | CRITICAL | The poll skips itself while draining, with no bound. A relay answering every 3s kept the drainer busy, the cycle never ran, and a real incoming message was **never read or stored at all**. |
| 5 | The webhook payload had no `phone_number` | HIGH | The field the product is keyed on, absent from the body — and with the name fallback the URL may carry no number either. |
| 6 | The relay was enabled in configuration | HIGH | Not part of this product. GETs every automated chat's webhook every 3s and sends whatever comes back. |

### The incident

An end-to-end attempt sent about **thirty unintended messages to a real
contact**. The cause was the test rig: a capture endpoint that answered GET as
well as POST, feeding a relay that polls with GET every three seconds. All nine
recorded sends carry `origin=relay`; none came from the pipeline under test.

Two defects it exposed (#4 and #6 above) were real and are fixed. The harness
is now hardened: `GET` returns `204` with zero bytes, the send API is disabled
outright rather than merely guarded, and `WADAM_ONLY_ORIGIN` permits exactly one
producer, checked on **all six** send paths — three of which bypass the queue
and would have been missed by a guard on `enqueue` alone.

### Measured

| Operation | Result |
|---|---:|
| Conversation read, 12-message chat | 1,640 ms |
| Conversation read, ~100-bubble chat | 5,843 ms |
| Chat-list read | 120 ms |
| EXE build | 51.8 MB, clean |

Read time **scales with the number of rendered bubbles**. The 6–8 s figures
seen mid-audit were a large conversation, not a regression — confirmed by
disabling the new parsing step and re-measuring (5,843 vs 5,824 ms, no
difference).

### Still not proven

The real inbound → webhook → response → send → verify chain. It has been
attempted once and the incoming half demonstrably did not work, because
discovery was starved (#4). That is fixed and **has not been re-tested**.

`scripts/arm_e2e.py` refuses to arm unless a chat carries a number the
application discovered itself, the relay is off, the API is unreachable, and
the capture endpoint answers GET with 204.

### Verdict at the time of §15

**NOT READY.** Superseded — see §17. The gate named here was evidence, not
suspicion: the complete pipeline had never been observed working end to end
against a real message. It has been now, in both directions.

Tests at the time: 410.


---

## 16. Inbound collector proven end to end (2026-08-09)

The chain the audit set out to establish, observed rather than inferred, with
each link measured from its own evidence source:

```
2 UIA bubbles  ->  2 reader messages  ->  2 unique keys  ->  2 MongoDB records
                                                        ->  second poll adds 0
```

Measured on `WINSPARK_TWOSEND_DIAG`, which had no pre-existing rows and is
therefore the uncontaminated case. Both invariants hold:

| Invariant | Result |
|---|---|
| distinct bubbles reading alike -> distinct records | **PASS** |
| same physical bubble re-read -> no new record | **PASS** |

### What was wrong, and where

The loss was at two independent points, either of which alone would have caused
it:

1. **The reader** returned four of seven bubbles. `_read_labeled_messages`
   iterates sender *labels*, and WhatsApp draws one per run of consecutive
   messages from the same person. The selection rule only consulted the
   bubble parser when the labelled one found no incoming message — never true
   in an active conversation.
2. **The storage key** hashed content only, so two identical messages in the
   same minute collapsed at `has_message()`.

### Known migration concern

Message keys changed format. Expect at most one extra row per repeated message
still in the visible tail, once. See
[MIGRATION.md](MIGRATION.md#message-keys-change-format--plan-for-one-duplicate-per-repeated-message).
Not reconciled during validation, deliberately: altering stored data mid-audit
destroys the evidence.

### Outstanding

Outbound verification only — the two-send diagnostic. The collector is no longer
entangled with it.


---

## 17. Final verdict — READY WITH KNOWN LIMITATIONS

Both flows are now proven end to end against real WhatsApp traffic, each link
measured from its own evidence source rather than inferred from the one next to
it.

### Inbound — WhatsApp to `wa_events`

```
2 UIA bubbles -> 2 reader messages -> 2 unique keys -> 2 MongoDB records
                                                   -> second poll adds 0
```

Measured on `WINSPARK_TWOSEND_DIAG`, which had no pre-existing rows and is
therefore the uncontaminated case. Raw message text is preserved byte for byte;
a real inbound message was stored as `'WINSPARK_E2E_TEST_84721'` with no sender
name, badge or phone number prepended.

### Outbound — `POST /wam/` to a verified bubble

```
POST /wam/ {"id":"918106972933"} -> phone_number resolution -> durable queue
                         -> sender -> new bubble -> census verification
```

The two-send diagnostic, re-run after the reader fix:

```
baseline=2  final=4  new_bubbles=2  expected=2
send_1: compose_after_fill 'WINSPARK_TWOSEND_DIAG'  bubbles 2 -> 3
send_2: compose_after_fill 'WINSPARK_TWOSEND_DIAG'  bubbles 3 -> 4
CLASSIFICATION: BOTH_MESSAGES_OBSERVED
```

Each send produced exactly one bubble. **The sender was never defective.** The
earlier "two sends, one bubble" was entirely the reader defect: the same send
code, frozen and unchanged, produced the correct result once the instrument
measuring it was fixed. Census verification recovered on its own —
`count_outgoing` now returns 4 for four bubbles — without census logic being
touched, which is the right outcome, since compensating there would have
concealed the real bug.

### Verdict

**READY WITH KNOWN LIMITATIONS.**

Ready for supervised production on a dedicated machine. Not for unattended
operation, for the reasons below.

### Known limitations

| Limitation | Impact | Documented |
|---|---|---|
| ~~A saved contact's phone number cannot be discovered~~ — **withdrawn, the finding was wrong** | The number IS readable from the contact-info panel; the original probes scanned an unrendered tree. See the correction | [LIMITATIONS.md](LIMITATIONS.md) |
| **Stale message keys from before the occurrence-aware format** | At most one extra row per repeated message still in the visible tail, once | [MIGRATION.md](MIGRATION.md) |
| **Sending requires an interactive desktop** | Over a disconnected RDP session or a locked workstation, messages queue and wait; reading continues | [LIMITATIONS.md](LIMITATIONS.md) |
| **Two chats can share an exact name** | An ambiguous id is refused with `409`, never guessed; use the full number, or the `chat_id` for a group | [SEND_API.md](SEND_API.md) |
| **Chat identity is hashed from the display name** | Renaming a contact creates a new chat here | [DATA.md](DATA.md) |
| **A conversation read costs 1.6–5.8 s**, scaling with rendered bubbles | Bounds throughput; the queue absorbs bursts | §2 |
| **RDP disconnect and reconnect: never tested for real** | Simulated only | below |
| **24-hour stability: never measured** | A 15-minute soak is all that was observed | §3 |

### What the last two mean

They are operational validation, not pipeline correctness. Both flows work; what
is unproven is how the application behaves over a day, and through a real
session disconnect rather than a simulated one. Neither should be assumed from
a 15-minute window, and this document has avoided that kind of extrapolation
throughout.

### The lesson worth keeping

Every serious defect this audit found came from collapsing two of these into
one:

```
UIA observation  !=  reader observation  !=  transport result  !=  verification result
```

The alignment threshold treated screen geometry as authorship, and our own sent
messages read as incoming — which would have posted them to the webhook and
answered them. The pre-send census treated "the chat is open" as "the chat can
be read". And a diagnostic written to exonerate the sender measured bubbles
*with the reader*, so it blamed the sender for the reader's defect and nearly
caused working send code to be rewritten.

Tests: 444.
