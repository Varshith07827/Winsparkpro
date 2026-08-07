# Headless operation: audit, evidence, and what Windows actually permits

Commissioned as "make this a true background automation service". This is the
audit, the measurements, the changes, and — because the brief asked for honesty
over promises — the part that cannot be delivered and why.

**The short answer.** Reading is now fully headless and provably so. Sending is
not, and cannot be, against WhatsApp Desktop as it ships today. Every mouse
action but one has been eliminated, the cursor no longer moves, the desktop is
handed back afterwards, and the one remaining interruption is measured, logged
and deferred until you stop typing. What could not be removed is documented
below with the evidence that says so.

---

## 1. Audit of the previous implementation

Every finding was produced by static analysis of the tree plus a call-graph
walk, not by reading the code and hoping.

### 1.1 Physical input simulation — every location

| File:line | Call | Effect |
|---|---|---|
| `sender.py:159-160` | `win32api.keybd_event(VK_MENU)` ×2 | Synthetic ALT press/release into the global input queue |
| `sender.py:262` | `compose.Click(simulateMove=False)` | **Moves the physical cursor** and clicks |
| `sender.py:263-264` | `auto.SendKeys("{Ctrl}a")`, `("{Delete}")` | Global keystroke injection |
| `sender.py:336` | `auto.SendKeys("{Ctrl}v")` | Global keystroke injection + clipboard takeover |
| `sender.py:373` | `auto.SendInput(KeyboardInput…)` | Per-character injection, 30 ms each |
| `sender.py:403` | `compose.Click(simulateMove=False)` | Cursor move + click |
| `sender.py:478` | `compose.Click(simulateMove=False)` | Cursor move + click |
| `sender.py:480` | `auto.SendKeys("{Enter}")` | Keystroke injection |
| `sender.py:601-604` | `box.Click()` + `Ctrl+A`/`Delete`/`Esc` | Cursor move + 3 keystroke injections |
| `sender.py:622-624` | `box.Click()` + `Ctrl+A`/`Delete` | Cursor move + 2 keystroke injections |
| `sender.py:759` | `item.Click(simulateMove=False)` | Cursor move + click on a chat row |

`uiautomation`'s `Control.Click()` is not a UIA call. It resolves the element's
bounding rectangle and issues a real `SetCursorPos` + `mouse_event` pair.
`simulateMove=False` only skips the *animated* glide — the pointer still jumps
to the target and clicks. Eleven separate sites did this.

No `pyautogui` or `pynput` was present.

### 1.2 Window activation — every location

`ensure_foreground()` / `_force_foreground()` (`sender.py:123-182`) escalate
through three techniques: a phantom ALT tap to defeat the anti-focus-stealing
lock, then a `HWND_TOPMOST`/`NOTOPMOST` z-order toggle, then a
minimize/restore bounce. Called from **six** places:

`open_chat_sync`, `set_compose_text_sync`, `press_enter_sync`,
`search_and_read_rows_sync`, `clear_search_sync`, and the search-recovery path
inside `open_chat_sync`.

### 1.3 The serious one: the read path could hijack the desktop

A call-graph walk found this chain:

```
_cycle  →  read_chat_rows_async  →  read_chat_rows_sync
                                       └─→ clear_search_sync
                                              ├─→ ensure_foreground   (activates WhatsApp)
                                              ├─→ box.Click()         (moves the cursor)
                                              └─→ SendKeys Ctrl+A, Delete, Esc
```

**The three-second poll — a passive read — could activate WhatsApp, move the
mouse and inject five keystrokes.** It fired whenever the recents grid was
hidden, which is exactly what happens while a search is open. Nothing in the
design intended this; it was a convenience call in a read function.

This was the single worst defect in the audit and is fixed first.

---

## 2. What WhatsApp Desktop actually exposes

Measured against the live window, not assumed. This is the "Inspect.exe
findings" the brief asked for, gathered programmatically.

### 2.1 Advertised patterns

| Element | Patterns offered |
|---|---|
| Compose box (`EditControl`) | LegacyIAccessible, ScrollItem, TextChild, TextEdit, **Text**, **Value** |
| Chat row (`DataItemControl`) | GridItem, LegacyIAccessible, ScrollItem, **SelectionItem**, TableItem, TextChild |
| Send button (`ButtonControl`) | **Invoke**, LegacyIAccessible, ScrollItem, TextChild |

The compose box reports `ValuePattern.IsReadOnly = False` and
`IsKeyboardFocusable = True`. The chat row reports
`LegacyIAccessible.DefaultAction = "Double Click"`.

### 2.2 What those patterns actually do

Every write/action pattern was invoked against the live window:

| Attempt | Result |
|---|---|
| `ValuePattern.SetValue("…")` on the compose box | **Silent no-op.** Returns success, `TextPattern` reads back `''` |
| `LegacyIAccessiblePattern.SetValue("…")` | **Silent no-op** |
| `SelectionItemPattern.Select()` on a chat row | **Silent no-op.** Active conversation unchanged |
| `LegacyIAccessible.DoDefaultAction()` on a chat row | **Silent no-op** |
| `WM_SETTEXT` / `WM_CHAR` to the Chromium HWND | **No effect** |
| `SetFocus()` + `SendKeys` | **Works** — text lands, cursor never moves |
| `InvokePattern.Invoke()` on the Send button | Pattern present and offered |

**This is the finding the whole redesign turns on: Chromium advertises UIA
patterns it does not implement.** `IsReadOnly = False` on a control whose
`SetValue` does nothing is not a subtle bug — it is a provider that maps its
accessibility tree for *reading* and leaves the write side unimplemented while
still claiming support. Any design that assumes "the pattern is offered, so it
works" will fail silently, which is the worst failure mode available.

### 2.3 The window tree

```
WhatsApp (top level)
├─ InputNonClientPointerSource
├─ ReunionWindowingCaptionControls     ← WinUI 3 / Windows App SDK chrome
├─ Microsoft.UI.Content.DesktopChildSiteBridge
├─ Chrome_WidgetWin_0                  ← the entire web content, one HWND
└─ InputSiteWindowClass
```

WhatsApp Desktop is a WinUI 3 shell hosting Chromium content. **There is one
HWND for all of the UI** — no per-control window handles — which is why
`WM_SETTEXT` has nothing to address and Option B (background window messaging)
is dead on arrival. This confirms empirically what `docs/SENDING.md` predicted.

---

## 3. Why sending stops when RDP disconnects

The brief asked for a technical explanation with evidence rather than a guess.

### 3.1 Measured state of this machine

```
SM_REMOTESESSION (is RDP)   : True
our session id              : 4
active CONSOLE session id   : 5
we are the console session  : False
input desktop               : 'Default'      ← while connected
```

### 3.2 The mechanism

Windows gives each session a window station, `WinSta0`, containing desktops:
`Default` (the normal interactive desktop), `Winlogon` (the secure desktop used
for the lock screen and UAC), and `Disconnect`.

`SendInput` — which every keystroke and mouse event here ultimately uses —
injects into **the calling session's input queue**, and that queue is serviced
by whichever desktop currently owns input. When an RDP client disconnects,
the session survives, its processes keep running, and its windows keep
existing — but the session's input is no longer attached to `Default`. There is
no interactive desktop to deliver to.

**`SendInput` still returns success.** It reports the number of events inserted,
not the number delivered. The events go nowhere. That is precisely the observed
symptom: sending "stops" with no error anywhere.

The same applies to a locked workstation, where input moves to `Winlogon`, and
during a UAC prompt.

### 3.3 Which layer is responsible

| Candidate | Verdict |
|---|---|
| **Windows session architecture** | **Yes — this is the cause.** Input injection requires an attached, unlocked input desktop. By design, and it is a security boundary, not a bug |
| WhatsApp Desktop | No. The process runs normally and its UIA tree stays readable throughout |
| UI Automation | No. Reading works in a disconnected session; UIA is not the limitation |
| Previous implementation | Partly — it never *checked*, so a blocked send looked like a mysterious failure instead of a stated precondition |

### 3.4 What was done about it

`OpenInputDesktop` is the exact test: it succeeds only for the desktop that
currently owns session input. It is now a **preflight on every send**. A
disconnected or locked session produces an immediate, explicit refusal —

> *This RDP session has no input desktop — the session is disconnected or
> locked. Keystrokes injected now would silently go nowhere, so sending is held
> until it reconnects.*

— and the message stays queued rather than being marked sent. **Silent
non-delivery became a reported, recoverable hold.**

### 3.5 Keeping an RDP session alive

If the machine must keep sending while nobody is looking at it, the session has
to stay interactive. Options, in order of preference:

1. **Don't disconnect — minimise the RDP client.** The session stays attached.
2. **`tscon` back to the console:** `tscon <sessionid> /dest:console` moves the
   session to the physical console, which remains interactive after the RDP
   client goes away. Requires admin.
3. **A physical or virtual display + auto-logon**, with the screensaver and
   lock timeout disabled. Group Policy `DisableLockWorkstation` prevents the
   `Winlogon` switch.
4. **Do not run it as a Windows service.** Session 0 isolation gives services
   no interactive desktop at all — the failure would be permanent rather than
   intermittent.

---

## 4. Changes made

### 4.1 Reading is now provably input-free

`clear_search_sync` was removed from `read_chat_rows_sync`. When an active
search hides the recents grid, the reader now reads the **search results grid**
instead — same data, zero interaction. Clearing a search is left to the send
path, which is allowed to interact and accounts for it.

Verified by re-running the call-graph walk: **no read-path function can reach
any input-simulating call.**

*Why this improves reliability:* a poll that steals focus while someone is
typing corrupts their input and makes the application feel broken. It also
raced with itself — a poll could activate WhatsApp while a send was mid-sequence
and change which chat was open.

### 4.2 The cursor never moves on the normal path

`focus_control()` replaces every `Control.Click()` in the compose and search
paths with `IUIAutomationElement::SetFocus`. Measured: the caret lands in the
compose box and keystrokes go to it, with `GetCursorPos()` unchanged.

**Ten of the eleven click sites are gone.** The click that "was needed to place
the caret" was never needed.

*Why this improves reliability:* a coordinate click depends on the window being
where the rectangle says it is at the instant of the click. A moved, resized or
occluded window means the click lands somewhere else. `SetFocus` addresses the
element, not a screen position.

### 4.3 The one remaining click is contained

Switching conversations still needs it — nothing else works (§2.2). It now:

- runs only when the chat is **not already open** (the common case skips it),
- is viewport-checked so it cannot land on the wrong row,
- **saves and restores the cursor position**, so the pointer flicks and returns,
- is documented in place with the full list of alternatives that failed.

### 4.4 The desktop is handed back

Every send captures the previously-foreground window and reactivates it
afterwards. Verified live: focus returned to the previous application every
time. The restore is reliable *because* we are the foreground process by then,
which is exactly the state in which `SetForegroundWindow` is permitted.

### 4.5 Sends wait for a quiet moment

Before taking focus, the sender waits for `GetLastInputInfo` to report 1.5 s of
no keyboard or mouse activity, up to a 20 s cap. Past the cap it proceeds and
logs that it did — a busy machine that never delivers is a worse failure than a
brief interruption.

### 4.6 A session preflight before every send

`wadam/whatsapp/session.py` checks the whole precondition chain — session id,
console session, RDP status, input desktop, UIA availability, window presence,
minimised state, user idle time — and refuses with a reason rather than
attempting a send that cannot work.

### 4.7 Session health in the UI

A **Session health** card, refreshed every cycle:

```
WhatsApp           Connected                    [ok]
Desktop session    Active (Default)             [ok]
Session type       RDP (session 4)              [degraded]
UI Automation      Available                    [ok]
Sender             Ready                        [ok]
```

`Desktop session` flips to `Disconnected / locked` the moment the RDP client
goes away, with the blocking reason spelled out underneath.

### 4.8 Every send is recorded in full

`SendResult` now carries: method, UIA pattern used, attempts, duration,
`activated_window`, `moved_cursor`, `used_clipboard`, `recovery_used`,
`foreground_restored`, and the failure reason — surfaced through
`as_log_fields()` into the activity log alongside the existing timestamp, chat
and direction.

*Why this improves reliability:* "did this send disturb the user, and how?" is
now answerable from the log rather than from someone watching the screen.

---

## 5. Is fully background sending achievable?

**No — not against WhatsApp Desktop as it ships today.** Being specific about
which part:

| Operation | Headless? | Evidence |
|---|---|---|
| Discover the window | **Yes** | Process enumeration, no interaction |
| Read the chat list | **Yes** | UIA `GridPattern`, works minimised and unfocused |
| Read a conversation | **Yes** | UIA tree walk |
| Detect new messages | **Yes** | Reading only |
| Call a webhook | **Yes** | Network |
| **Switch conversation** | **No** | Every pattern no-ops (§2.2); needs a click |
| **Put text in the box** | **No** | `ValuePattern.SetValue` no-ops; needs focus + keystrokes |
| **Press Send** | **Yes** | `InvokePattern` is real — but the box must have text first |

The blocker is not Windows. **Windows offers exactly the right API — 
`ValuePattern.SetValue` — and WhatsApp's Chromium provider declines to
implement it while advertising it.** If that one method worked, sending would
be completely invisible: no focus change, no keystrokes, no cursor.

So the honest position is: this is an *application* limitation, not a platform
one, and it could disappear with a WhatsApp update. The code is arranged to
take advantage the moment it does — `ValuePattern` is still attempted first on
every send, and if it ever succeeds the entire input path is skipped.

What *is* a genuine Windows restriction is the input-desktop requirement (§3):
once keystrokes are unavoidable, an interactive session is unavoidable too.
That one is a security boundary and should not be worked around.

---

## 6. Alternatives considered

| Approach | Verdict |
|---|---|
| **UI Automation** (current) | Best available. Reading is complete and free; writing is limited by the provider |
| **Legacy IAccessible / MSAA** | Tested. `SetValue` and `DoDefaultAction` both no-op — it is the same provider behind a different interface |
| **`SendMessage` / `PostMessage`** | Tested, no effect. One HWND for all content; there is nothing to address |
| **WinAppDriver** | Wraps the same UIA provider, so it inherits the same no-ops, and it is archived/unmaintained. Adds a service dependency for no capability gain |
| **Chromium DevTools / CDP** | The only route that would *truly* be headless — it drives the DOM directly. Requires WhatsApp to start with a remote-debugging port, which it does not, and cannot be enabled from outside the process |
| **UIA provider-side injection** | Would require code inside the WhatsApp process. Out of scope and fragile |
| **Clipboard + keystroke** | Already the fallback. Faster and more reliable than typing, at the cost of briefly borrowing the clipboard |
| **WhatsApp Business Platform** | The genuinely headless answer — an HTTPS API with no desktop involved. Different product, different rules; see `docs/SENDING.md` Option D |

**Recommendation.** Keep the UIA-first ladder for desktop automation and treat
it as inherently semi-interactive. If genuine unattended, invisible operation
is a hard requirement, the Business Platform is the correct architecture and
the desktop path should be treated as a stopgap.

---

## 7. Remaining limitations

1. **Switching chats moves the cursor briefly.** Contained and restored, but
   visible. Unavoidable until a working activation pattern exists.
2. **Sending takes the foreground for 1–3 seconds.** Restored afterwards.
   Unavoidable while keystrokes are required.
3. **A disconnected or locked session cannot send.** Detected and held, not
   failed. A Windows security boundary.
4. **Session 0 / Windows service hosting cannot work at all.** No interactive
   desktop, ever.
5. **The clipboard is briefly borrowed** on the paste path. Text contents are
   restored; non-text contents are lost.
6. **A UAC prompt blocks sending** while it is up — input is on the secure
   desktop. Detected by the same preflight.

---

## 8. Recommendations

1. **Re-test `ValuePattern.SetValue` after every WhatsApp update.** It is one
   probe, and if it starts working the application becomes silent overnight. Worth
   automating as a startup capability check.
2. **Run on the console session, not RDP**, or use `tscon` — removes the whole
   class of disconnect failures.
3. **A dedicated machine or VM** if invisibility matters. The interruption only
   matters if somebody is using that desktop.
4. **Batch sends per chat.** Consecutive messages to one conversation currently
   pay the switch cost each time; grouping them would cut the interruption count.
5. **Move to the Business Platform** for anything genuinely unattended.
6. **Send during idle windows** — the deferral is in; a scheduled quiet-hours
   window would extend it.
