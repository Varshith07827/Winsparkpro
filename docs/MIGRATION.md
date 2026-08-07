# Migration notes: winSpark → WhatsApp Desktop Automation Manager

winSpark was a general-purpose Windows desktop automation application that
happened to have a WhatsApp connector. This is a WhatsApp automation engine.
That is not a refactor of emphasis — it changes what belongs in the codebase at
all, so this was built as a new project that **reuses winSpark's proven parts
verbatim** rather than a fork carrying everything forward.

The reference project is untouched at `..\WinsparkPro`.

---

## What was carried over

These modules encode findings that were expensive to obtain — each was verified
against a live WhatsApp window, and several exist because of a specific failure.
Rewriting them would have meant rediscovering the same things.

| winSpark | Here | Changes |
|---|---|---|
| `automation/sta_thread_manager.py` | `whatsapp/sta_thread.py` | Trimmed to what is used; health reporting kept |
| `connectors/whatsapp.py` | `whatsapp/reader.py` | Media/timestamp reading kept; thumbnail-capture rects dropped |
| `connectors/whatsapp_group_sender.py` | `whatsapp/sender.py` | Restructured into an explicit UIA-first ladder; `WhatsAppGroupSendResult` → local `SendResult` |
| `connectors/whatsapp_row_parser.py` | `whatsapp/row_parser.py` | Added a group hint from the speaker prefix |
| `connectors/whatsapp_chat_name_rules.py` | `whatsapp/name_rules.py` | Unchanged in substance |
| `ui/theme.py` (approach) | `ui/theme.py` | Rewritten for WhatsApp's palette, plus a light theme |
| `connectors/fetch_webhook_client.py` (approach) | `engine/webhook.py` | GET-poll → POST-dispatch; stdlib `urllib` preference kept |
| `data/chat_memory.py` (Mongo handling) | `storage/mongo.py` | Local-vs-Atlas timeouts, SRV/dnspython guidance, the `authSource`-in-path trap |

The specific knowledge that survived, and would have been costly to relearn:

* WhatsApp's chat list is a **virtualized Chromium DataGrid** — a tree walk sees
  zero rows, but `GridPattern.GetItem(row, 0)` returns them. "Search results." is
  the exact reverse. No OCR is needed for anything.
* `GridPattern` realizes rows **below the viewport** with real screen coordinates
  thousands of pixels down; clicking one opens the wrong chat.
* Windows refuses `SetForegroundWindow` from a background thread; a **phantom
  ALT tap** lifts the lock, and `AttachThreadInput` actively prevents it.
* `ValuePattern.SetValue` **silently no-ops** on the compose box, and
  `ValuePattern.Value` reads back a static `"\n"`.
* `uiautomation.SendKeys` **truncates astral codepoints**, corrupting emoji.
* WhatsApp renders the conversation into the accessibility tree **twice**, and
  tree order is not visual order.
* An **empty compose box is the only proof of send**.

---

## What was removed

### AI — every trace

| Removed | Was |
|---|---|
| `connectors/openai_client.py` | Chat-completion client |
| `connectors/retrieval.py` | RAG over chat memory |
| `data/chat_memory.py` | Conversation memory feeding AI prompts |
| `connectors/trigger_match.py` | Semantic trigger matching via LLM |
| AI settings, prompts, per-chat `ai_prompt` / `ai_mode` | Reply-source configuration |
| AI UI panels | Prompt editors, memory viewers |
| `openai` dependency, API-key configuration | |

Verified: `requirements.txt` lists seven packages — `uiautomation`, `pywin32`,
`psutil`, `pymongo[srv]`, `certifi`, `PySide6`, `python-dotenv`. A word-boundary
scan for `openai|groq|gpt|llm|anthropic|prompt|completion` across `wadam/`
returns two hits, both prose in docstrings.

**What replaced it.** Nothing, deliberately. The webhook *is* the decision-maker.
What sits behind that URL — a rules engine, a language model, a person with a
keyboard — is the operator's business and completely invisible here.

### Generic desktop automation

`automation/rule_engine.py`, `rule_index.py`, `matcher.py`, `mapper.py`,
`registry.py`, `actions.py`, `safety.py`, `screen_agent.py`,
`bus_event_trigger_mapper.py`; `engines/window_discovery.py`,
`window_actions.py`, `event_monitoring.py`, `text_injection.py`,
`ui_automation_interaction.py`; `eventbus/`, `hub/`, `services/process_metrics.py`;
`cli.py`, `app.py`, the multi-app adapter registry, and the plugin surface.

Window discovery survives only as `find_window_sync`, which looks for exactly
one process. There is no adapter registry and no way to point this at another
application.

### OCR

`connectors/window_ocr.py`, `screen_watch.py`, the OCR configuration pages, and
the six `winrt-*` and `Pillow` dependencies. The reader was already
accessibility-tree-only; the OCR path existed for other applications.

### SQLite

`data/connection.py`, `schema.py`, `repositories.py`. MongoDB is the primary
store and the JSON mirror is the backup, so a third local database had no role.

---

## What is genuinely new

* **`storage/repository.py`** — the write-to-both facade. winSpark had SQLite
  *or* Mongo as alternatives; here they are primary *and* mirror, and every
  write goes through one place.
* **`storage/json_backup.py`** — atomic controlled saves.
* **`engine/discovery.py`** — automatic registration. winSpark bound chats to
  automations by hand.
* **`engine/pipeline.py`** and the `MessageStatus` lifecycle — persistence before
  every irreversible step, and crash recovery derived from it.
* **`engine/engine.py`** — the fixed 3-second loop with a separate worker.
* The whole UI. winSpark's was a generic automation console.

---

## Behavioural differences

| | winSpark | Here |
|---|---|---|
| Chat registration | manual binding | automatic, every cycle |
| Trigger | poll a GET URL, or AI | POST each incoming message |
| Interval | per-binding, configurable | fixed 3 s, not configurable |
| Storage | SQLite, optional Mongo | Mongo primary + JSON mirror |
| Settings | settings window | `.env` only |
| Scope | any Windows application | WhatsApp Desktop only |
| Crash recovery | none | state-machine driven |

---

## If you are moving from a winSpark install

There is **no automated data migration**, and this is deliberate: winSpark's
`fetch_webhook_bindings` are GET-poll bindings with AI configuration attached.
The shapes don't correspond, and a migration that silently reinterpreted a poll
URL as a dispatch endpoint would send traffic somewhere the operator did not
intend.

Moving over by hand takes a few minutes:

1. Install and configure `.env` (see [the README](../README.md#quick-start)).
2. Start the application. Every chat is discovered automatically.
3. For each chat you had bound, paste its URL and tick **Enabled**.
4. Change the endpoint from a GET that returns a message to a POST handler that
   receives one — see [the webhook contract](../README.md#the-webhook-contract).

winSpark's data is untouched, so both can be run side by side while you switch.
Not at the same moment, though: two processes driving one WhatsApp window will
fight over the foreground.
