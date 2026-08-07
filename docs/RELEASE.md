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
297 passed
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
| Retry policy | ⚠️ | Holds for queued sends; **the Send API path has no retry at all** — see §11 |
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
| 24-hour stability | ❌ | **Not run.** Unverified |
| **Security** | ✅ | §7 |
| **Testing** | | |
| Unit + integration | ✅ | 297 tests |
| Real MongoDB | ✅ | Storage suites run against both stores |
| Real WhatsApp reads | ✅ | Live probes and benchmarks |
| **Real WhatsApp send end-to-end** | ❌ | **Exercised 2026-08-07 and it FAILED — see §11.** ~141 attempts, 13 delivered |
| **Documentation** | ✅ | Ten documents; see the README index |
| **Deployment** | ⚠️ | No installer or packaging; run from source |
| **Send API path** | ❌ | Bypasses the durable queue and the verifier entirely — §11 |
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

**Not ready for production.** Superseded by §11: the first real end-to-end
send test delivered 13 of ~141 messages. An earlier draft of this document
recommended supervised production use; that recommendation was written before
the send path had ever been exercised against a real chat, and it was wrong.

If genuinely unattended, invisible operation is a hard requirement, the desktop
path cannot deliver it and the WhatsApp Business Platform is the correct
architecture — see [SENDING.md](SENDING.md), Option D.

---

## 11. End-to-end send failure (2026-08-07)

The first real exercise of the send path. ~141 messages posted to the Send API
for one chat; **13 arrived**. Recorded here in full because §10 previously
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

### Operational note

A `for /L` loop of 100 curl requests kept running after the operator pressed
`^C` and had to be killed by PID. Each iteration takes ~10 s because the sender
waits for an idle desktop, so a cancelled burst keeps sending for minutes. The
API should expose a way to cancel queued work for a chat.
