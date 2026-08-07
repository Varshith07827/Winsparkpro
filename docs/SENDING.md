# Sending messages: the transport options

Only **Option A** is implemented. B, C and D are recorded here so the decision
is documented rather than rediscovered, and so a future implementer starts from
what is already known rather than from scratch.

---

## Option A — Windows UI Automation (implemented, the supported path)

Implemented in [`wadam/whatsapp/sender.py`](../wadam/whatsapp/sender.py).

The sender locates the WhatsApp window, the conversation row, the message input
and the Send button, and drives them through UI Automation patterns. Each step
is a ladder that begins at the purest UIA mechanism and descends only when that
mechanism demonstrably fails on the live application.

| Step | Rung 1 (pure UIA) | Rung 2 | Rung 3 |
|---|---|---|---|
| Open a chat | `InvokePattern` / `SelectionItemPattern` / `LegacyIAccessible` | viewport-checked coordinate click | search box → result row |
| Fill the input | `ValuePattern.SetValue` | clipboard paste (Option C) | per-character Unicode input |
| Send | `InvokePattern` on the Send button | `Enter` keystroke | — |

### Why the ladder has lower rungs at all

These are not defensive hypotheticals. Each was established against a running
WhatsApp Desktop:

- **`ValuePattern.SetValue` silently no-ops on the compose box.** It is a
  contenteditable `div`; the call reports success and the text never appears.
  For the same reason `ValuePattern.Value` cannot be used to read the box back —
  it returns a static `"\n"` regardless of content. Verification goes through
  `TextPattern.DocumentRange.GetText()`.
- **`GridPattern` realizes rows below the viewport.** A 512-chat list reports a
  52,927px-tall grid on a 1,200px screen. Clicking a realized-but-scrolled-away
  row clamps the cursor to the screen edge and opens the bottom-most *visible*
  chat instead. Hence the pattern activation first (it cannot land on the wrong
  row) and the viewport check before any click.
- **Windows refuses `SetForegroundWindow` from a background thread.** A phantom
  ALT keypress first makes the following call count as user-initiated. On
  Windows 11 this succeeded where both a bare `SetForegroundWindow` and the
  `AttachThreadInput` technique failed — and `AttachThreadInput` combined with
  the ALT tap actively *prevented* the change, so it is deliberately not used.
- **`uiautomation.SendKeys` corrupts emoji.** It sends each character as one
  16-bit `KEYEVENTF_UNICODE` scan code, truncating U+1F496 to ``. The
  fallback splits astral codepoints into their UTF-16 surrogate pair, as
  Windows' own text input does.
- **30 ms per character, not 10 ms.** At 10 ms some applications drop
  keystrokes; measured, `"the quick brown fox 12345"` arrived in Notepad as
  `"the quick brown oox 55555"`.

### Verification

WhatsApp clears the compose box when a message is actually delivered. That empty
box is the only accepted proof of send: "we typed it and clicked Send" is
evidence of nothing. A send that leaves text in the box is recorded as a
**failure** and retried — reporting it as success is exactly how a
typed-but-unsent message gets marked sent and never retried.

---

## Option B — Background window messaging (documented, not implemented)

Send without touching the visible UI, by posting native window messages
(`WM_SETTEXT`, `WM_KEYDOWN`, `PostMessage`) directly to the input control's
`HWND`.

**Why it is attractive.** It needs no foreground, no focus, and no mouse. The
user could keep working while messages are sent, and WhatsApp could stay
minimised.

**Why it is not implemented.** WhatsApp Desktop is a Chromium application. Its
"controls" are not native window handles — the entire renderer is one HWND, and
the compose box is a contenteditable DOM node inside it with no window of its
own to address. `WM_SETTEXT` has nothing to target, and synthesised key messages
posted to the renderer HWND are generally discarded by Chromium's input
pipeline, which reads from the OS input queue rather than the window message
queue.

**What would need to be true.** Either WhatsApp ships a non-Chromium input
surface, or a path is found through Chromium's own automation interfaces (the
accessibility bridge already used by Option A, or a remote-debugging port if one
were ever exposed). Worth revisiting only if one of those changes; this should
be verified against a live window before any code is written.

---

## Option C — Clipboard-assisted UI Automation (implemented as a fallback)

Put the text on the clipboard, focus the input, `Ctrl+V`, then trigger Send
through UI Automation.

This is already in the codebase as **rung 2** of the fill step, because it is
the mechanism that actually works today: a paste inserts text verbatim in one
action, where per-character typing costs 30 ms a character, risks dropped
keystrokes proportional to length, and turns a newline into a keystroke the
compose box does not reliably render as a line break.

**Its cost, stated plainly.** It takes over the system clipboard for the
duration. Text contents are saved and restored; non-text contents (an image, a
file selection) cannot be preserved and are lost. That trade is why it is rung
two and not rung one — and why the pure `ValuePattern` path is tried first even
though it currently fails, so the moment WhatsApp implements it the clipboard
stops being touched at all, with no code change.

---

## Option D — Official WhatsApp Business Platform (documented, not implemented)

For operators with a WhatsApp Business account, the Cloud API is the officially
supported channel: an HTTPS POST to Meta's endpoint with a phone number ID and a
bearer token.

**How it would fit.** As a separate *transport*, selected per chat, behind the
same interface the pipeline already calls:

```
MessagePipeline ──▶ Transport
                     ├── UiAutomationTransport   (Option A, today)
                     └── BusinessApiTransport    (Option D, future)
```

The pipeline's contract — `send(chat, text) -> SendResult` — is already the
right shape, so this is an addition rather than a rewrite. **The desktop
automation architecture would not change.**

**What it does not solve.** The Business Platform can only message users who
have opted in, within a 24-hour customer-service window or via pre-approved
message templates, from a business number that is not simultaneously usable in
WhatsApp Desktop. It is a different product with different rules, not a drop-in
replacement for automating a personal account — which is why it is a parallel
transport rather than a migration target.
