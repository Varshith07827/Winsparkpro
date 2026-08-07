# Test report

```
245 passed
```

Environment: Windows 11 Pro 26200 · Python 3.13.14 · PySide6 6.11.1 ·
pymongo 4.17.0 · MongoDB Community Server on `localhost:27017` ·
WhatsApp Desktop running.

```bash
python -m pytest
```

`pyflakes wadam tests` is clean. No `TODO`, `FIXME`, `XXX` or `HACK` markers
remain in the source.

---

## Coverage by area

| Suite | Tests | What it pins down |
|---|---:|---|
| `test_row_parser.py` | 6 | Parsing real sidebar rows captured from a live window |
| `test_webhook.py` | 19 | Response shapes, empty-reply semantics, retry policy |
| `test_storage.py` | 8 | Write-to-both, atomic mirror, dedup, export, `.env` parsing |
| `test_discovery.py` | 9 | New chats arrive inert; user settings survive rediscovery |
| `test_pipeline.py` | 10 | End-to-end against a real HTTP server; verification rules |
| `test_recovery.py` | 13 | The crash decision table, and state across a restart |
| `test_engine_integration.py` | 13 | The poll loop with WhatsApp faked: discovery, dedup, reconnect |
| `test_engine_bookkeeping.py` | 4 | Per-cycle telemetry across every periodic branch |
| `test_ui.py` | 27 | Webhook field, search, selection, validation, theming |
| `test_send_api.py` | 51 | Contact IDs, ambiguity refusal, auth, HTTP over a real socket |
| `test_relay.py` | 43 | Response shapes, the two dedup rules, record accuracy, live GETs |
| `test_mongo_integration.py` | 12 | Real MongoDB: indexes, uniqueness, restart, pruning |
| `test_delivery.py` | 16 | Verification arithmetic, queue durability, restart, metrics |
| **Total** | **245** | |

`test_mongo_integration.py` skips itself when no server is reachable, so the
suite still passes on a machine without MongoDB. Point it elsewhere with
`WADAM_TEST_MONGODB_URI`.

---

## Acceptance criteria → evidence

| Criterion | Where it is verified |
|---|---|
| Launches from a valid `.env` with no user configuration | Live run; `test_storage.py::test_env_parser_keeps_urls_with_equals_signs` |
| WhatsApp Desktop discovered automatically | Live run (window handle `6556476`, 4 chats read) |
| Existing chats populate the sidebar | Live run; `test_engine_integration.py::test_a_new_chat_is_registered_within_one_cycle` |
| New chats detected within one polling cycle | `test_a_chat_appearing_later_is_picked_up_on_the_next_cycle` |
| Each new chat gets a MongoDB record and JSON backup | `test_discovery_writes_through_to_the_json_mirror`; `test_all_six_collections_and_their_indexes_exist` |
| Sidebar updates live without restart | `test_ui.py::test_an_unchanged_snapshot_does_not_rebuild_the_list`, `test_selection_survives_a_rebuild` |
| Selecting a chat opens its configuration panel | `test_ui.py::test_selecting_a_chat_shows_that_chats_webhook` |
| Webhook URL can be assigned, edited and saved | `test_ui.py` validation + save group (6 tests) |
| Automation toggled globally and per chat | `test_the_global_switch_writes_every_chat_and_individuals_still_override` |
| Incoming messages detected exactly once | `test_re_reading_the_same_conversation_produces_no_repeat_work`; `test_the_unique_index_refuses_a_duplicate_message` |
| Persisted **before** webhook execution | `test_pipeline.py::test_the_full_path_persists_every_step` |
| Webhook responses persisted **before** sending | same, plus the `AWAITING_SEND` transition in `test_recovery.py` |
| Outgoing messages sent through the UIA pipeline | `test_pipeline.py` (fake sender); live run (`clipboard-paste + send-button-invoke`) |
| Failed sends retried per policy | `test_webhook.py::test_retry_policy`; `test_a_failing_endpoint_is_recorded_not_swallowed` |
| All events logged with timestamps | `test_recovery.py` log assertions; `automation_logs` schema |
| MongoDB primary, JSON synchronized | `test_storage.py` (4 tests); `test_mongo_integration.py` |
| Polling every 3 s, not modifiable from the UI | `POLL_INTERVAL_SECONDS` constant; config warns if `.env` disagrees |
| Responsive while polling and processing | Architecture: separate threads; `test_engine_bookkeeping.py` |
| Closing and reopening restores all state | `test_configuration_and_dedup_survive_a_restart`; `test_state_survives_a_repository_restart` |
| Reconnects when WhatsApp closes or restarts | `test_whatsapp_closing_is_survived_and_reconnected_to`, `test_a_recreated_window_is_detected_even_while_one_is_open` |
| No AI code paths, panels, dependencies or configuration | Dependency list is 7 packages; word-boundary scan returns only prose |
| Send API resolves an ID to exactly one chat, or refuses | `test_send_api.py` resolution group (10 tests) |
| Send API cannot be reached without a token | `test_a_request_without_a_token_is_refused`; config rejects a tokenless port |
| An API send is persisted like any other message | `test_an_api_send_is_stored_like_any_other_message` |
| The relay never re-sends what it already sent | `test_relay.py` dedup group (6 tests) |
| A relayed message is sent and persisted like a reply | `test_a_delivered_message_is_persisted_like_any_other` |

---

## Verified against live systems

Automated tests use fakes for WhatsApp. These were run against the real thing:

**WhatsApp Desktop (read-only probe).** 4 rows read from the live window; names,
timestamps, group detection and the active conversation all parsed correctly.
One row with no timestamp and no preview fell through to the documented
all-name path.

**MongoDB (`localhost:27017`).** A 10-second engine run created all six
collections, discovered and registered 4 chats with automation OFF and no
webhook, wrote all seven JSON mirror files, and recorded shutdown state. Test
databases are dropped afterwards.

**The send API.** The real stack — settings, repository, engine loop and API
host — was started against a scratch database and exercised over a real socket:
`GET /health` 200, no token 401, wrong token 401, unknown id 404, missing
message 400, unquoted id accepted, `GET` on a send path 405. No identifier was
allowed to resolve, so nothing was sent to anyone.

**The relay.** The real engine ran for seven seconds against a local endpoint
scripted to return `"Hello Varshith"`, nothing, `"Hello Varshith"`, `"second
message"`, nothing, and then `"Hello Varshith"` forever. Seven polls produced
exactly three sends — the repeat immediately after an empty poll suppressed, the
repeat after an intervening message allowed, and the non-dequeuing tail silenced
— which is the documented rule, observed rather than asserted.

**A full application session.** Chats discovered, a chat seeded (7 existing
messages baselined, none automated), a webhook configured and tested, the global
toggle applied to 4 chats, rescans run, and **a reply delivered end to end** —
logged as `Replied via clipboard-paste + send-button-invoke`. That is rung 1 of
the fill ladder (`ValuePattern.SetValue`) falling through as documented, the
clipboard fallback filling the box, and the pure-UIA `InvokePattern` on the Send
button delivering it, verified by the compose box clearing.

---

## Defects found and fixed during this work

| Defect | Found by | Fix |
|---|---|---|
| `prune_logs` called on `Repository` when it existed only on `MongoStore` — killed the engine at cycle 200, ten minutes into a live run | Live run | Added `Repository.prune_logs`; **and** wrapped per-cycle bookkeeping in its own guard, since telemetry should never be able to stop automation |
| The webhook field kept the previous chat's URL when switching chats | Reported | The dirty-check compared the field against the newly selected chat instead of the one being left; now tracks the last loaded value |
| `ChatListPanel._build_header` called `restyle()` before `_search` existed — would have crashed at launch | `test_ui.py` | Split avatar styling out of `restyle()` |
| `update_message` wrote to MongoDB but left the in-memory copy stale, so the JSON mirror and UI kept showing a resolved message's old status | `test_recovery.py` | Replace the ring-buffer entry as well |
| `poll_state` never materialized on short runs | Live run | Persist on the first cycle as well as every tenth |
| Three relayed messages went out but only two were recorded — content-derived keys collapsed the legitimate repeat, and separately every sent message was stored twice (once by us, once when the poll read the bubble back) | Live relay run | A distinct key for messages we originate, plus `recently_originated` so our own bubble is not stored again |
| `FakeCollection.find_one` matched only the query's **first** key, so the relay's dedup lookup answered from the wrong document — a stand-in that answers differently from the real thing is worse than none | `test_relay.py` | The fake now matches every key, including `$in` and `$lt` |
| A malformed response object in the send API raised *past* the handler's guard, dropping the connection with no reply | `test_send_api.py` | Validate the response type and respond inside the guard |
| The send API replied 401/404 **without reading the request body**, so Windows reset the connection and callers saw "connection aborted" instead of the JSON explaining what they got wrong | `test_send_api.py` (as a flake) | Read the body before the path and auth checks |

---

## What is not covered

**Sending.** It drives a real WhatsApp window with real mouse and keyboard input;
a test that passed would have delivered a message to a real person. The logic
around it — which rung to try, when to declare failure, what to persist — is
tested through fakes, but the UIA calls themselves are verified by running the
application. This is the largest deliberate gap.

**Multi-hour stability.** The longest observed run is ~10 minutes (which is how
the cycle-200 defect was found). Nothing longer has been measured.

**Scale under load.** Behaviour with many simultaneously-automated busy chats is
reasoned about in [LIMITATIONS.md](LIMITATIONS.md#scale) but not measured.
