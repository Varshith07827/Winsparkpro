"""Sending a message — **Option A: Windows UI Automation**.

This is the one supported transport. The sender locates the WhatsApp window,
the conversation row, the message input and the Send button, and drives them
through UI Automation patterns. Options B, C and D from the design are
documented in `docs/SENDING.md`; only A is implemented.

**"UI Automation patterns only, avoid simulated typing wherever possible"** is
taken literally, and each step is a ladder that starts at the purest UIA
mechanism and only descends when that mechanism demonstrably fails:

    Opening a chat   Invoke / SelectionItem / LegacyIAccessible  ← pure UIA, no
                     ↓                                             foreground
                     verified coordinate click (viewport-checked)
                     ↓
                     search box → result row

    Filling input    ValuePattern.SetValue                       ← pure UIA
                     ↓
                     clipboard paste (Ctrl+V)                    ← Option C
                     ↓
                     per-character Unicode input                 ← last resort

    Sending          InvokePattern on the Send button            ← pure UIA
                     ↓
                     Enter keystroke

The descents are not hypothetical. `ValuePattern.SetValue` **silently no-ops**
on WhatsApp's compose box: it is a contenteditable div, the call reports
success, and the text never appears. `ValuePattern.Value` reads back a static
"\\n" regardless of content, so verification has to go through
`TextPattern.DocumentRange.GetText()`. The ladder keeps the pure path first so
it starts working the day WhatsApp implements it, without anyone editing this
file.

Three more behaviours, each earned by a real failure rather than reasoned about:

* **A realized row is not a visible row.** `GridPattern` realizes rows below
  the viewport with real screen coordinates thousands of pixels down (measured:
  a 52,927px-tall grid for a 512-chat list on a 1,200px screen). Clicking one
  clamps the cursor to the screen edge and opens the bottom-most *visible* chat
  instead. So a coordinate click only happens when the click point is genuinely
  inside the window, and the resulting conversation is always verified.
* **Foreground has to be forced and confirmed.** Coordinate clicks and
  keystrokes go to whatever window the OS considers foreground, not to whatever
  element UIA points at. Windows refuses `SetForegroundWindow` from a
  background thread; a phantom ALT tap first makes it succeed.
* **An empty compose box is the proof of send.** WhatsApp clears the input when
  a message is actually delivered. "Typed and clicked Send" is not evidence of
  anything — a send that leaves text in the box is reported as a failure and
  retried.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Optional

from wadam.whatsapp.name_rules import chat_names_match, is_system_or_list_view_title
from wadam.whatsapp.reader import (
    RECENTS_GRID,
    SEARCH_RESULTS_GRID,
    ChatRow,
    WhatsAppReader,
    find_chat_grid,
    get_active_conversation_name_sync,
    iter_grid_row_controls,
    read_chat_rows_sync,
)
from wadam.whatsapp import session
from wadam.whatsapp.transport import TransportCapabilities
from wadam.whatsapp.row_parser import parse_chat_row
from wadam.whatsapp.sta_thread import StaAutomationThread

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    import win32api
    import win32clipboard
    import win32con
    import win32gui

    _UIA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _UIA_AVAILABLE = False


# How long the desktop must have been untouched before a send takes the
# foreground, and how long it will wait for that before going ahead regardless.
#: Set by WhatsAppSender so the module-level click helper can report the one
#: physical mouse action left in the application. A plain hook rather than
#: threading metrics through every helper — the alternative was five signatures
#: changed to carry one counter.
_metrics_hook = None

QUIET_IDLE_SECONDS = 1.5
MAX_DEFER_SECONDS = 20.0


class WhatsAppUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class SendResult:
    """The outcome of a send, with enough detail to answer "what did it
    actually do to my desktop?" — which is the question this application has to
    be able to answer honestly."""

    ok: bool
    detail: str = ""
    strategy: str = ""        # which rung of the ladder delivered it
    verified: bool = False    # the compose box was confirmed empty afterwards
    pattern: str = ""         # the UIA pattern used, when one was
    attempts: int = 0
    duration_ms: int = 0
    activated_window: bool = False   # did we have to take the foreground?
    moved_cursor: bool = False       # did any physical mouse action happen?
    used_clipboard: bool = False
    recovery_used: str = ""          # non-empty when a fallback rung was needed
    foreground_restored: bool = False
    #: So a batch can reuse it instead of re-finding the window per message.
    window_handle: int = 0

    @classmethod
    def succeeded(cls, strategy: str, **kw) -> "SendResult":
        return cls(ok=True, detail="sent", strategy=strategy, verified=True, **kw)

    @classmethod
    def failed(cls, detail: str, strategy: str = "", **kw) -> "SendResult":
        return cls(ok=False, detail=detail, strategy=strategy, **kw)

    def as_log_fields(self) -> dict:
        return {
            "success": self.ok, "method": self.strategy, "pattern": self.pattern,
            "attempts": self.attempts, "duration_ms": self.duration_ms,
            "activated_window": self.activated_window, "moved_cursor": self.moved_cursor,
            "used_clipboard": self.used_clipboard, "recovery": self.recovery_used,
            "foreground_restored": self.foreground_restored,
            "failure_reason": "" if self.ok else self.detail,
        }


def _require_uia() -> None:
    if not _UIA_AVAILABLE:
        raise WhatsAppUnavailableError(
            "uiautomation + pywin32 are required, and are only available on Windows."
        )


# ---------------------------------------------------------------------------
# Foreground
# ---------------------------------------------------------------------------


def previous_foreground() -> int:
    """The window that was active before we took over, so it can be given back."""
    try:
        return win32gui.GetForegroundWindow()
    except Exception:  # noqa: BLE001
        return 0


def restore_foreground(hwnd: int) -> bool:
    """Hand the desktop back to whatever the user was using.

    Called after every send. Windows applies the same anti-focus-stealing rules
    on the way back, but by this point we ARE the foreground process, which is
    exactly the state in which `SetForegroundWindow` is permitted — so the
    restore reliably succeeds where the original steal needed persuasion.
    Verified live: foreground returned to the previous window every time."""
    if not hwnd or not _UIA_AVAILABLE:
        return False
    try:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return False
        win32gui.SetForegroundWindow(hwnd)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


def focus_control(control) -> bool:
    """Give a control keyboard focus WITHOUT touching the mouse.

    Enough for controls that only need focus. **Not enough for the compose
    box** — see `focus_compose_caret`."""
    try:
        control.SetFocus()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("SetFocus failed", exc_info=True)
        return False


def focus_compose_caret(compose) -> bool:
    """Put the CARET inside the compose box — SetFocus, then a physical click.

    The click is not belt-and-braces. UIA focus and a DOM caret are different
    things in a Chromium contenteditable: `SetFocus` makes the element the
    focused UIA element (`HasKeyboardFocus` reports True, and it looks like it
    worked) while the contenteditable has no insertion point, so pasted and
    typed characters are discarded and `Enter` is ignored.

    An earlier version of this file dropped the click, on the strength of one
    live measurement where SetFocus alone appeared sufficient. That measurement
    was taken after a chat switch, which clicks a row and leaves WhatsApp with a
    caret already placed — so it measured a caret it had not created. Without
    that, filling the box failed roughly half the time and reported
    "Could not put the message into the compose box".

    The reference implementation this project was ported from
    (`winspark/connectors/whatsapp_group_sender.py`) does SetFocus followed by
    `Click(simulateMove=False)` for exactly this reason, and records the same
    finding for `Enter`. Do not remove the click without re-measuring on a
    window that has NOT just had a chat switch.

    The cursor is put back where the user left it, as with `_click_item`."""
    try:
        compose.SetFocus()
    except Exception:  # noqa: BLE001
        logger.debug("SetFocus failed", exc_info=True)

    origin = None
    try:
        origin = win32gui.GetCursorPos()
    except Exception:  # noqa: BLE001
        pass
    try:
        compose.Click(simulateMove=False)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Failed to click the compose box", exc_info=True)
        return False
    finally:
        if origin is not None:
            try:
                win32api.SetCursorPos(origin)
                if _metrics_hook is not None:
                    _metrics_hook.record_cursor_restore()
            except Exception:  # noqa: BLE001
                logger.debug("could not restore the cursor position", exc_info=True)


def ensure_foreground(hwnd: int, attempts: int = 5, settle: float = 0.2) -> bool:
    """Bring `hwnd` to the real OS foreground and CONFIRM it.

    A safety gate, not a nicety: everything that follows a failed foreground
    change lands on whatever window happens to be on top instead. Callers must
    abort on False rather than clicking blind."""
    if not _UIA_AVAILABLE:
        return False
    for attempt in range(attempts):
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            _force_foreground(hwnd, aggressive=attempt >= 2)
        except Exception:  # noqa: BLE001 - best effort; verified below
            pass
        time.sleep(settle)
    return win32gui.GetForegroundWindow() == hwnd


def _force_foreground(hwnd: int, aggressive: bool = False) -> None:
    """One attempt at a foreground change, escalating if asked.

    The decisive step is the phantom ALT tap: it makes Windows treat the
    following `SetForegroundWindow` as user-initiated and lifts the
    anti-focus-stealing lock that otherwise refuses it from a background thread.
    Verified on Windows 11 where both a bare `SetForegroundWindow` and the
    `AttachThreadInput` technique failed — and `AttachThreadInput` combined with
    the ALT tap actively *prevented* the change, so it is deliberately absent.

    `aggressive` adds a z-order TOPMOST/NOTOPMOST toggle and then a
    minimize/restore bounce, for machines with a stricter focus-lock policy."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == hwnd:
        return

    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - Windows may still decline; verified by caller
        pass
    win32gui.BringWindowToTop(hwnd)
    if not aggressive or win32gui.GetForegroundWindow() == hwnd:
        return

    try:
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass
    if win32gui.GetForegroundWindow() == hwnd:
        return

    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Compose box
# ---------------------------------------------------------------------------


def _find_compose_element(window_handle: int):
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None
    compose = auto.Control(
        searchFromControl=root, searchDepth=40, ControlType=auto.ControlType.EditControl,
        RegexName=r"^Type a message",
    )
    return compose if compose.Exists(1, 0.2) else None


def _read_compose_text(compose) -> str:
    """The compose box is a contenteditable div: its `ValuePattern.Value` is
    stale and disconnected (it reads back a static "\\n" whatever the content).
    `TextPattern.DocumentRange.GetText()` reads it correctly. An empty box also
    reads as "\\n", so callers must compare against stripped text."""
    text_pattern = compose.GetPattern(auto.PatternId.TextPattern)
    if text_pattern is None:
        return ""
    return text_pattern.DocumentRange.GetText(-1) or ""


def _normalize_compose_text(text: str) -> str:
    """Make intended text and its compose-box readback comparable.

    Two tolerances, each from a real retry loop:

    * Whitespace collapses to single spaces — the contenteditable does not
      necessarily store a typed newline as "\\n", so a multi-line reply never
      verified exactly and was re-sent on every retry.
    * Emoji-ish characters are ignored on both sides — the box can read an emoji
      back as U+FFFC (the object-replacement placeholder) or not at all, so a
      reply containing one failed verification with the text sitting right
      there. The worst case of ignoring them is a send whose emoji went missing;
      the alternative was never sending at all."""
    _ignored = ("￼", "︎", "️")
    kept = "".join(ch for ch in (text or "") if ord(ch) <= 0xFFFF and ch not in _ignored)
    return " ".join(kept.split())


def _compose_matches(compose, text: str, attempts: int = 5, delay: float = 0.3) -> bool:
    """Does the box hold `text` yet? Polled, because WhatsApp's React render
    lags the input and the lag grows with message length — one fixed check
    passed for short messages and failed for exactly the long ones that
    matter."""
    want = _normalize_compose_text(text)
    for _ in range(max(1, attempts)):
        time.sleep(delay)
        if _normalize_compose_text(_read_compose_text(compose)) == want:
            return True
    return False


def _compose_is_blank(compose) -> bool:
    """Is the box empty, ignoring what WhatsApp leaves behind?

    Deliberately not `.strip()`: an emptied box reads back as the
    object-replacement placeholder or a stray variation selector where an emoji
    used to be, none of which `.strip()` removes. Emptiness is proof-of-send, so
    a box that never looks empty means every send is reported failed.

    Real emoji are NOT ignored here, unlike in `_normalize_compose_text`: an
    emoji still in the box means the message is still there."""
    text = _read_compose_text(compose) or ""
    leftovers = "￼︎️​‍"
    return not text.strip(leftovers + " \t\r\n").strip()


def _clear_compose(compose) -> None:
    # Focus, not click: the caret lands in the box from SetFocus alone.
    focus_control(compose)
    auto.SendKeys("{Ctrl}a", waitTime=0.15)
    auto.SendKeys("{Delete}", waitTime=0.15)
    time.sleep(0.2)  # let the clear render before anything reads or types


_value_pattern_ruled_out = False


def set_value_pattern_ruled_out(ruled_out: bool) -> None:
    """Told by the capability probe whether rung 1 can ever work on this build.

    The probe already writes its answer to `data/capabilities.json`; until this
    existed nothing read it, and every send paid 0.4s to re-discover a
    known-dead path."""
    global _value_pattern_ruled_out
    _value_pattern_ruled_out = bool(ruled_out)


def value_pattern_worth_trying() -> bool:
    return not _value_pattern_ruled_out


def _try_value_pattern(compose, text: str) -> bool:
    """Rung 1 — pure UI Automation. Known to no-op on current WhatsApp builds
    (the call succeeds, the text never appears), which is exactly why the result
    is verified through TextPattern rather than trusted."""
    try:
        pattern = compose.GetPattern(auto.PatternId.ValuePattern)
        if pattern is None:
            return False
        compose.SetFocus()
        pattern.SetValue(text)
    except Exception:  # noqa: BLE001
        return False
    return _compose_matches(compose, text, attempts=2, delay=0.2)


def _read_clipboard_text() -> Optional[str]:
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
        except Exception:  # noqa: BLE001 - another process holds it; back off
            time.sleep(0.05)
            continue
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return None
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception:  # noqa: BLE001
            return None
        finally:
            win32clipboard.CloseClipboard()
    return None


def _write_clipboard_text(text: str) -> bool:
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
            continue
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            win32clipboard.CloseClipboard()
    return False


def _paste_text(compose, text: str) -> bool:
    """Rung 2 — clipboard-assisted insertion (design Option C, used here as the
    fallback it was described as).

    A paste inserts the text verbatim in one action, where per-character typing
    costs 30ms a character, can drop keystrokes, and turns a newline into a
    keystroke WhatsApp's compose box doesn't reliably render as a line break.

    The user's own clipboard text is restored afterwards. Non-text clipboard
    contents (an image, say) cannot be preserved this way and are lost — the
    trade for being able to insert long text at all. Never raises; returning
    False just moves to the next rung."""
    try:
        previous = _read_clipboard_text()
        if not _write_clipboard_text(text):
            return False
        try:
            auto.SendKeys("{Ctrl}v", waitTime=0.2)
            # Polled rather than a single read: a false "paste failed" is
            # expensive, because the caller then types the message a SECOND time
            # on top of a paste that actually worked.
            return _compose_matches(compose, text)
        finally:
            if previous is not None:
                _write_clipboard_text(previous)
    except Exception:  # noqa: BLE001
        logger.warning("Pasting into the compose box failed", exc_info=True)
        return False


def _send_unicode_text(text: str, interval: float = 0.03) -> None:
    """Rung 3 — per-character Unicode input, correctly handling characters above
    U+FFFF.

    `uiautomation.SendKeys` sends each character as a single 16-bit
    KEYEVENTF_UNICODE scan code, which silently truncates an astral character
    like "💖" (U+1F496) to its low 16 bits — confirmed live: it arrived as
    "\\uf496", a string that matches nothing. Windows represents such characters
    as a UTF-16 surrogate pair, two 16-bit units each sent as its own event;
    that's what this does.

    The 30ms interval is measured, not guessed: at 10ms some applications drop
    keystrokes ("the quick brown fox 12345" arrived in Notepad as "the quick
    brown oox 55555"); at 30ms the same phrase arrived exactly, repeatedly."""
    _require_uia()
    for char in text:
        codepoint = ord(char)
        if codepoint > 0xFFFF:
            codepoint -= 0x10000
            units = (0xD800 + (codepoint >> 10), 0xDC00 + (codepoint & 0x3FF))
        else:
            units = (codepoint,)
        for unit in units:
            flag = auto.KeyboardEventFlag.KeyUnicode
            auto.SendInput(
                auto.KeyboardInput(0, unit, flag | auto.KeyboardEventFlag.KeyDown),
                auto.KeyboardInput(0, unit, flag | auto.KeyboardEventFlag.KeyUp),
            )
        time.sleep(interval)


def set_compose_text_sync(window_handle: int, text: str,
                          use_clipboard: bool = True) -> tuple[bool, str]:
    """Put `text` into the compose box, climbing down the ladder until one rung
    verifies. Returns (ok, strategy)."""
    _require_uia()
    compose = _find_compose_element(window_handle)
    if compose is None:
        return False, ""

    # Rung 1 needs no foreground at all — try it before disturbing the desktop,
    # but only where the capability probe has not already ruled it out. On a
    # build whose provider discards SetValue this costs 0.4s of guaranteed
    # failure per send, and it writes to the box before the paste that follows.
    if text and value_pattern_worth_trying() and _try_value_pattern(compose, text):
        return True, "uia-value-pattern"

    # Clearing a box that is already empty is the common case after a send, and
    # it does not need the foreground, a click, or any input at all. Checking
    # first saves ~2s per send.
    if not text and _compose_is_blank(compose):
        return True, "already-empty"

    if not ensure_foreground(window_handle):
        logger.warning("WhatsApp is not in the foreground; not typing (input would go elsewhere)")
        return False, ""

    try:
        # Already correct (a retry after a send that didn't take)? Leave it —
        # re-inserting risks losing it or doubling it up.
        if text and _normalize_compose_text(_read_compose_text(compose)) == _normalize_compose_text(text):
            return True, "already-present"

        focus_compose_caret(compose)

        if not _compose_is_blank(compose):
            _clear_compose(compose)

        if not text:
            return True, "cleared"

        if use_clipboard and _paste_text(compose, text):
            return True, "clipboard-paste"

        if use_clipboard:
            logger.warning("compose: paste did not verify — falling back to per-character input")
        # The paste may have landed partially, or fully with a readback too slow
        # to confirm. Typing now would append to it and leave the message
        # doubled, so the fallback starts from an empty box.
        if not _compose_is_blank(compose):
            _clear_compose(compose)
        _send_unicode_text(text)
        if _compose_matches(compose, text):
            return True, "unicode-input"
        return False, ""
    except Exception:  # noqa: BLE001
        logger.warning("Failed to set compose box text", exc_info=True)
        return False, ""


def _find_send_button(window_handle: int):
    """WhatsApp's Send button, which appears beside the compose box once it has
    text. A ButtonControl named exactly "Send" exposing InvokePattern. Anchored
    to `^Send$` so it can't match "Send document" in the attach menu."""
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None
    button = auto.Control(
        searchFromControl=root, searchDepth=40,
        ControlType=auto.ControlType.ButtonControl, RegexName=r"^Send$",
    )
    return button if button.Exists(1, 0.2) else None


def invoke_send_button_sync(window_handle: int) -> bool:
    """Send by INVOKING the Send button — the reliable path, and the pure-UIA
    one. Invoke is delivered straight to the control, so it doesn't depend on
    the OS foreground window, on the caret sitting inside the contenteditable,
    or on a keystroke arriving at all: the three things that made Enter
    silently no-op, leaving a message typed but unsent."""
    _require_uia()
    button = _find_send_button(window_handle)
    if button is None:
        logger.warning("send: no Send button found — falling back to Enter")
        return False
    try:
        pattern = button.GetPattern(auto.PatternId.InvokePattern)
        if pattern is None:
            return False
        pattern.Invoke()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Failed to invoke the Send button", exc_info=True)
        return False


def press_enter_sync(window_handle: int) -> bool:
    """Fallback send: place the caret in the box and press Enter. `SetFocus`
    alone is not enough — a physical click is what actually puts the caret
    inside the contenteditable so WhatsApp treats Enter as "send"."""
    _require_uia()
    compose = _find_compose_element(window_handle)
    if compose is None:
        return False
    if not ensure_foreground(window_handle):
        logger.warning("WhatsApp is not in the foreground; not pressing Enter")
        return False
    try:
        focus_control(compose)
        time.sleep(0.1)
        auto.SendKeys("{Enter}", waitTime=0.15)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Failed to press Enter in the compose box", exc_info=True)
        return False


def compose_is_empty_sync(window_handle: int) -> bool:
    _require_uia()
    compose = _find_compose_element(window_handle)
    if compose is None:
        return False
    try:
        return _compose_is_blank(compose)
    except Exception:  # noqa: BLE001
        return False


def read_compose_text_sync(window_handle: int) -> str:
    _require_uia()
    compose = _find_compose_element(window_handle)
    if compose is None:
        return ""
    try:
        return _read_compose_text(compose)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Search box
# ---------------------------------------------------------------------------


# WhatsApp's chat search matches on the name TEXT, and typing an emoji into it
# can filter to nothing — so "Papa 💜" is searched as "Papa" and results are
# still matched against the full original name.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D\U000024C2\U0000203C\U00002049]+"
)


def _search_query(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", _EMOJI_RE.sub("", name or "")).strip()
    return cleaned or (name or "").strip()


def _find_search_box(window_handle: int):
    """WhatsApp's chat-search box. Empty, its accessible name is "Search or
    start a new chat"; with a query typed the name becomes the query, so the
    empty-state match alone isn't enough. The fallback takes the top-most edit
    control that isn't the compose box or the locked-chats search."""
    from wadam.whatsapp.reader import _safe_children, _safe_control_type, _safe_name

    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None

    box = auto.Control(
        searchFromControl=root, searchDepth=40, ControlType=auto.ControlType.EditControl,
        RegexName=r"^Search or start",
    )
    if box.Exists(2, 0.3):
        return box

    best = None
    best_top = None

    def walk(ctrl, depth=0):
        nonlocal best, best_top
        if depth > 40:
            return
        if _safe_control_type(ctrl) == "EditControl":
            name = _safe_name(ctrl)
            if not name.startswith("Type a message") and name != "Search locked chats":
                try:
                    top = ctrl.BoundingRectangle.top
                except Exception:  # noqa: BLE001
                    top = 0
                if best_top is None or top < best_top:
                    best_top, best = top, ctrl
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(root)
    return best


def _search_box_query(window_handle: int) -> str:
    """What's currently typed in the search box. `ValuePattern.Value` holds the
    query — the box's Name stays empty even with a query typed — which makes
    this the reliable "is a search active?" signal, unlike the "Search results."
    grid that recent builds don't expose at all while searching."""
    _require_uia()
    box = _find_search_box(window_handle)
    if box is None:
        return ""
    try:
        value_pattern = box.GetPattern(auto.PatternId.ValuePattern)
        if value_pattern is not None:
            return (value_pattern.Value or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def clear_search_sync(window_handle: int) -> None:
    """Leave an active search so WhatsApp returns to the recents list.

    Gated on the search box actually HAVING a query, so this never presses
    Escape while a normal chat is open with an empty box (which would close the
    chat)."""
    _require_uia()
    if not _search_box_query(window_handle):
        return
    if not ensure_foreground(window_handle):
        return
    box = _find_search_box(window_handle)
    try:
        if box is not None:
            focus_control(box)
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Delete}", waitTime=0.1)
        auto.SendKeys("{Esc}", waitTime=0.1)
    except Exception:  # noqa: BLE001
        pass


def search_and_read_rows_sync(window_handle: int, query: str) -> list[ChatRow]:
    """Type `query` into the search box and read the results grid. Leaves the
    search active so the caller can open the result (opening a chat clears the
    search by itself)."""
    _require_uia()
    if not ensure_foreground(window_handle):
        return []
    box = _find_search_box(window_handle)
    if box is None:
        return []
    search_text = _search_query(query)
    try:
        focus_control(box)
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Delete}", waitTime=0.1)
        if search_text:
            _send_unicode_text(search_text)
            time.sleep(0.2)
        time.sleep(1.2)  # let the filtered results populate
    except Exception:  # noqa: BLE001
        logger.warning("WhatsApp search typing failed", exc_info=True)
        return []
    return read_chat_rows_sync(window_handle, SEARCH_RESULTS_GRID)


# ---------------------------------------------------------------------------
# Opening a chat
# ---------------------------------------------------------------------------


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _phone_key(value: str) -> str:
    """A comparable key for a phone number: its last 10 digits, so
    "+1 (555) 010-9423", "15550109423" and "5550109423" all match. Empty for anything that
    isn't phone-number-like."""
    digits = _digits(value)
    if len(digits) < 7:
        return ""
    non_phone = re.sub(r"[\d+\-\s().]", "", value or "")
    if len(non_phone) > 2:
        return ""
    return digits[-10:]


def looks_like_phone_number(value: str) -> bool:
    return _phone_key(value) != ""


def match_chat_row(rows: list[ChatRow], chat_name: str) -> Optional[ChatRow]:
    """Exact name first, then truncation-tolerant, then — for a phone number —
    by digits."""
    target = chat_name.strip().lower()
    for row in rows:
        if row.chat_name.strip().lower() == target:
            return row
    for row in rows:
        if chat_names_match(chat_name, row.chat_name):
            return row
    key = _phone_key(chat_name)
    if key:
        for row in rows:
            if _phone_key(row.chat_name) == key or key in _digits(row.raw_text):
                return row
    return None


def _first_real_result(rows: list[ChatRow]) -> Optional[ChatRow]:
    from wadam.whatsapp.reader import _is_search_section_header

    for row in rows:
        name = (row.chat_name or "").strip()
        if name and not is_system_or_list_view_title(name) and not _is_search_section_header(name):
            return row
    return None


def _row_matches_chat(row_raw_text: str, chat_name: str) -> bool:
    if not chat_name:
        return False
    parsed = parse_chat_row(row_raw_text).get("chat_name", "")
    return parsed.strip().lower() == chat_name.strip().lower() or chat_names_match(chat_name, parsed)


def _find_row_item(chat_list, row_raw_text: str, chat_name: str):
    for item in iter_grid_row_controls(chat_list):
        name = (item.Name or "").strip()
        if not name:
            continue
        if name == row_raw_text.strip() or _row_matches_chat(name, chat_name):
            return item
    return None


def _activate_via_pattern(item) -> str:
    """Open a chat row through UI Automation alone — no foreground change, no
    mouse. Returns the pattern that worked, or "" if none did.

    This is the preferred path and the reason the coordinate click below is a
    fallback rather than the mechanism: a pattern activation cannot land on the
    wrong row, and it works with WhatsApp behind other windows."""
    for pattern_id, name, call in (
        (auto.PatternId.InvokePattern, "invoke", lambda p: p.Invoke()),
        (auto.PatternId.SelectionItemPattern, "selection-item", lambda p: p.Select()),
        (auto.PatternId.LegacyIAccessiblePattern, "legacy-default-action",
         lambda p: p.DoDefaultAction()),
    ):
        try:
            pattern = item.GetPattern(pattern_id)
            if pattern is None:
                continue
            call(pattern)
            return name
        except Exception:  # noqa: BLE001 - pattern present but unsupported here
            continue
    return ""


def _click_point_inside(item, container, window_handle: int) -> bool:
    """Whether clicking the item's centre would actually hit it.

    The container grid's rectangle is NOT the viewport — Chromium reports the
    full virtual scroll content. A realized-but-scrolled-away row has real
    coordinates thousands of pixels below the window; clicking there clamps the
    cursor to the screen edge and hits the bottom-most visible row instead. So
    the test is against the intersection of the grid's rect and the window's
    actual on-screen rect."""
    try:
        r = item.BoundingRectangle
        c = container.BoundingRectangle
        win_left, win_top, win_right, win_bottom = win32gui.GetWindowRect(window_handle)
    except Exception:  # noqa: BLE001 - stale element / dead window
        return False
    if r.bottom - r.top < 8:
        return False  # collapsed/zero-height row — nothing real to click

    visible_left = max(c.left, win_left)
    visible_top = max(c.top, win_top)
    visible_right = min(c.right, win_right)
    visible_bottom = min(c.bottom, win_bottom)
    center_x = (r.left + r.right) // 2
    center_y = (r.top + r.bottom) // 2
    return visible_left <= center_x <= visible_right and (visible_top + 2) <= center_y <= (visible_bottom - 2)


def _click_item(item) -> bool:
    """**The only physical mouse action left in this application**, and it is
    recovery, not the normal path.

    It exists because switching conversations has no working alternative.
    Measured against a live WhatsApp window, every non-mouse route silently did
    nothing while reporting success:

        SelectionItemPattern.Select()          advertised, no-op
        LegacyIAccessiblePattern.DoDefaultAction()  advertised ("Double Click"), no-op
        InvokePattern                          not offered on chat rows
        search box + Enter / Down+Enter / Tab+Enter  no selection
        WM_SETTEXT / WM_CHAR to the Chromium HWND    no effect

    So the click stays, with the damage contained: the cursor is put back where
    the user left it, so the pointer flicks and returns rather than being
    abandoned somewhere else on screen. A chat that is already open never
    reaches here at all — which is the common case for a busy conversation."""
    origin = None
    try:
        origin = win32gui.GetCursorPos()
    except Exception:  # noqa: BLE001
        pass
    try:
        item.Click(simulateMove=False)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Failed to click chat row", exc_info=True)
        return False
    finally:
        if origin is not None:
            try:
                win32api.SetCursorPos(origin)
                if _metrics_hook is not None:
                    _metrics_hook.record_cursor_restore()
            except Exception:  # noqa: BLE001
                logger.debug("could not restore the cursor position", exc_info=True)


def chat_already_open(window_handle: int, target: str) -> bool:
    """Is `target` ALREADY the open conversation? A cheap accessibility read
    with no foreground change, so an open/send can short-circuit instead of
    trying (and possibly failing) to re-foreground the window."""
    if not target:
        return False
    active = get_active_conversation_name_sync(window_handle)
    if active:
        # A name came back, so the question is already answered — matching or
        # not. The header scan below costs ~2.5s and can only agree.
        return (active.strip().lower() == target.strip().lower()
                or chat_names_match(target, active))
    # No name at all: a read-only/announcement group has no compose box to take
    # the name from, so fall back to the (expensive) header read.
    return _conversation_header_matches(window_handle, target)


def _opened_chat_matches(window_handle: int, target: str) -> bool:
    """Confirm a click/invoke actually opened `target`, by reading which
    conversation the compose box now belongs to — and, when there is no compose
    box at all (announcement/read-only groups have none), by the conversation
    header instead."""
    if not target:
        return True  # nothing to verify against — trust the activation
    time.sleep(0.5)  # the compose placeholder swaps shortly after
    active = get_active_conversation_name_sync(window_handle)
    if active and (active.strip().lower() == target.strip().lower() or chat_names_match(target, active)):
        return True
    return _conversation_header_matches(window_handle, target)


def _conversation_header_matches(window_handle: int, target: str) -> bool:
    """Whether the open conversation's header carries `target` as its title. The
    header glues the title to a status line, so a prefix match is accepted —
    safe because the scan is confined to the header strip of the right panel."""
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return False
    try:
        win_left, win_top, win_right, _bottom = win32gui.GetWindowRect(window_handle)
    except Exception:  # noqa: BLE001
        return False
    header_bottom = win_top + 170
    divider_x = win_left + (win_right - win_left) // 3  # right of the chat-list panel
    wanted = target.strip().lower()
    found: list[bool] = []

    def walk(ctrl, depth: int = 0) -> None:
        if found or depth > 40:
            return
        try:
            control_type = ctrl.ControlTypeName
        except Exception:  # noqa: BLE001 - stale element
            return
        if control_type in ("TextControl", "ButtonControl"):
            try:
                rect = ctrl.BoundingRectangle
                name = (ctrl.Name or "").strip()
            except Exception:  # noqa: BLE001
                rect, name = None, ""
            if rect is not None and name and rect.top < header_bottom and rect.left > divider_x:
                low = name.lower()
                if low == wanted or low.startswith(wanted + " ") or chat_names_match(target, name):
                    found.append(True)
                    return
        try:
            children = ctrl.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            walk(child, depth + 1)

    walk(root)
    return bool(found)


def open_chat_sync(window_handle: int, row_raw_text: str, chat_name: str = "") -> bool:
    """Open `chat_name`'s conversation, verified.

    Order: already open → UIA pattern activation → viewport-checked coordinate
    click → search box. Every path that isn't "already open" is verified against
    the now-active conversation, so a wrong activation can't masquerade as a
    successful open."""
    _require_uia()
    target = chat_name.strip() or parse_chat_row(row_raw_text).get("chat_name", "")

    if target and chat_already_open(window_handle, target):
        return True

    for grid_name in (RECENTS_GRID, SEARCH_RESULTS_GRID):
        chat_list = find_chat_grid(window_handle, grid_name)
        if chat_list is None:
            continue
        item = _find_row_item(chat_list, row_raw_text, chat_name)
        if item is None:
            continue
        # Pure UIA first: no foreground change, no mouse, cannot hit the wrong
        # row even when the sidebar is scrolled elsewhere.
        pattern = _activate_via_pattern(item)
        if pattern and _opened_chat_matches(window_handle, target):
            logger.debug("opened %r via %s", target, pattern)
            return True
        if not ensure_foreground(window_handle):
            logger.warning("WhatsApp is not in the foreground; not clicking the chat row")
            return False
        if not _click_point_inside(item, chat_list, window_handle):
            break  # scrolled out of view — search brings it on screen
        if _click_item(item) and _opened_chat_matches(window_handle, target):
            return True
        break  # activated but landed wrong — recover via search

    if not target:
        return False
    search_and_read_rows_sync(window_handle, target)
    grid = find_chat_grid(window_handle, SEARCH_RESULTS_GRID)
    if grid is None:
        return False
    item = _find_row_item(grid, row_raw_text, target)
    if item is None:
        return False
    pattern = _activate_via_pattern(item)
    if pattern and _opened_chat_matches(window_handle, target):
        return True
    if not _click_point_inside(item, grid, window_handle):
        return False
    if not _click_item(item):
        return False
    return _opened_chat_matches(window_handle, target)


# ---------------------------------------------------------------------------
# The async surface
# ---------------------------------------------------------------------------


class WhatsAppSender:
    """The UI Automation transport. Implements `wadam.whatsapp.transport.Transport`."""

    def __init__(self, reader: WhatsAppReader, sta: StaAutomationThread,
                 use_clipboard: bool = True, metrics=None) -> None:
        self._reader = reader
        self._sta = sta  # must be the same STA thread the reader uses
        # Pasting borrows the clipboard for ~200ms and puts text contents back.
        # Some people would rather it never touched the clipboard at all; with
        # this off, text goes in character by character instead — slower, and
        # historically the path that drops keystrokes, but it leaves the
        # clipboard alone entirely.
        self._use_clipboard = use_clipboard
        self._metrics = metrics
        global _metrics_hook
        _metrics_hook = metrics

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            name="Windows UI Automation",
            requires_foreground=True,
            moves_cursor=True,          # only when switching conversations
            uses_clipboard=self._use_clipboard,
            requires_interactive_desktop=True,
            requires_whatsapp_running=True,
            notes=("Cursor moves only to switch conversations, and is restored. "
                   "The foreground is taken for 1-3s per send and handed back."),
        )

    async def resolve_chat_row_async(self, chat_name: str):
        """Find the chat: in the recents sidebar first, then — only if it isn't
        visible there — via the search box, so the common case never disturbs
        WhatsApp's UI. Returns (window_handle, ChatRow|None)."""
        window_handle = await self._reader.find_window_async()
        if window_handle is None:
            return None, None

        rows = await self._reader.read_chat_rows_async(window_handle)
        match = match_chat_row(rows, chat_name)
        if match is not None:
            return window_handle, match

        results = await self._sta.invoke_async(
            lambda: search_and_read_rows_sync(window_handle, chat_name)
        )
        match = match_chat_row(results, chat_name)
        if match is not None:
            return window_handle, match

        # A saved contact's search result shows the contact's NAME, not the
        # number, so digit matching finds nothing. A search for a specific
        # number is unambiguous, so take the top real result.
        if looks_like_phone_number(chat_name):
            top = _first_real_result(results)
            if top is not None:
                return window_handle, top

        await self._sta.invoke_async(lambda: clear_search_sync(window_handle))
        return window_handle, None

    async def open_and_read_async(self, chat_name: str, limit: int = 25):
        """Open a chat and read its recent messages. Holds the action lock for
        the whole sequence — opening a chat is exactly the operation that, run
        mid-send, made a message land in the wrong conversation."""
        async with self._sta.action_lock:
            return await self.open_and_read_locked(chat_name, limit)

    async def open_and_read_locked(self, chat_name: str, limit: int = 25):
        """The same thing for a caller that ALREADY holds the action lock.

        `batch()` holds it for a whole drain, and `asyncio.Lock` is not
        reentrant, so the locking version would deadlock there. The pre-send
        census needs exactly this: the baseline has to be read from the target
        chat, and the target chat may not be the one on screen."""
        window_handle, row = await self.resolve_chat_row_async(chat_name)
        if window_handle is None or row is None:
            return None, []
        opened = await self._sta.invoke_async(
            lambda: open_chat_sync(window_handle, row.raw_text, chat_name)
        )
        if not opened:
            return window_handle, []
        await asyncio.sleep(0.4)  # let the conversation render
        messages = await self._reader.read_recent_messages_async(window_handle, limit)
        return window_handle, messages

    @asynccontextmanager
    async def batch(self):
        """Hold the lock, the quiet moment and the foreground for a whole run.

        A backlog is one interruption, not twenty. Sending each queued message
        through `send_async` re-probes the session, waits for the desktop to go
        quiet, takes the foreground and hands it back — per message. For a
        drain of twenty that is nineteen needless waits and nineteen extra
        foreground changes, and the user's desktop flickers once per message
        instead of once per burst.

        Yields a `send(chat_name, text)` coroutine with the same signature and
        return type as `send_async`, so the delivery code does not care which
        it was given. If the session cannot accept input, every send in the
        batch fails with that reason rather than silently going nowhere."""
        state = session.probe(self._title_hint())
        blocked = (state.send_blocked_reason or "Sending is not possible right now."
                   if state.can_send == session.Health.BLOCKED else "")

        if blocked:
            async def refuse(chat_name: str, message_text: str) -> SendResult:
                return SendResult.failed(blocked)
            yield refuse
            return

        async with self._sta.action_lock:
            await self._wait_for_a_quiet_moment()
            previous = await self._sta.invoke_async(previous_foreground)

            # Which conversation the last send left open. Re-finding the chat
            # row costs ~3.5s, and for consecutive messages to one chat the
            # answer has not changed — the action lock is held for the whole
            # batch, so nothing else can switch conversations underneath it.
            still_open = {"chat": "", "hwnd": None}

            async def send(chat_name: str, message_text: str) -> SendResult:
                if not (message_text or "").strip():
                    return SendResult.failed("Nothing to send (the reply was empty).")
                started = time.monotonic()
                result = await self._send_locked(
                    chat_name, message_text,
                    assume_open=(still_open["chat"] == chat_name),
                    known_hwnd=still_open["hwnd"],
                )
                # Only a confirmed send proves the chat is still open. Anything
                # else and the next message pays for a full resolve rather than
                # trusting a guess.
                still_open["chat"] = chat_name if result.ok else ""
                if result.window_handle:
                    still_open["hwnd"] = result.window_handle
                return replace(
                    result,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    activated_window=True,
                )

            try:
                yield send
            finally:
                restored = await self._sta.invoke_async(
                    lambda: restore_foreground(previous))
                if self._metrics:
                    self._metrics.record_focus_restore(restored)

    def _title_hint(self) -> str:
        return (self._reader._title_hint
                if hasattr(self._reader, "_title_hint") else "WhatsApp")

    async def send_async(self, chat_name: str, message_text: str) -> SendResult:
        """Send `message_text` to `chat_name`, verified.

        Three things wrap the send itself, and each is there because this is a
        background service running on somebody's desktop:

        1. **A session preflight.** Keystrokes injected into a session with no
           input desktop return success and go nowhere, so a disconnected or
           locked session is detected and reported instead of producing a
           silent non-delivery. See `wadam.whatsapp.session`.
        2. **Waiting for a quiet moment.** Taking the foreground is the one
           interruption that cannot be avoided; taking it while someone is
           mid-sentence is avoidable. The send waits for a short idle gap,
           bounded so a busy machine still gets its messages out.
        3. **Giving the desktop back.** Whatever was in front before is
           reactivated afterwards.

        The whole sequence runs under the action lock so nothing else can change
        the open chat or steal foreground between open → fill → send."""
        if not (message_text or "").strip():
            return SendResult.failed("Nothing to send (the reply was empty).")

        state = session.probe(self._title_hint())
        if state.can_send == session.Health.BLOCKED:
            return SendResult.failed(state.send_blocked_reason or "Sending is not possible right now.")

        async with self._sta.action_lock:
            await self._wait_for_a_quiet_moment()
            previous = await self._sta.invoke_async(previous_foreground)
            started = time.monotonic()
            try:
                result = await self._send_locked(chat_name, message_text)
            finally:
                restored = await self._sta.invoke_async(lambda: restore_foreground(previous))
            if self._metrics:
                self._metrics.record_focus_restore(restored)
            return replace(
                result,
                duration_ms=int((time.monotonic() - started) * 1000),
                activated_window=True,
                foreground_restored=restored,
            )

    async def _wait_for_a_quiet_moment(self) -> None:
        """Hold a send back while the user is actively typing.

        Bounded on purpose. Waiting forever for an idle desktop would mean a
        busy machine never delivers anything, which is a worse failure than a
        brief interruption — so after `MAX_DEFER_SECONDS` the send goes ahead
        regardless and says so in the log."""
        waited = 0.0
        while waited < MAX_DEFER_SECONDS:
            idle = session.user_idle_seconds()
            if idle >= QUIET_IDLE_SECONDS:
                return
            await asyncio.sleep(0.5)
            waited += 0.5
        logger.info("sending after waiting %.0fs for an idle desktop — going ahead anyway",
                    waited)

    async def _send_locked(self, chat_name: str, message_text: str,
                           assume_open: bool = False,
                           known_hwnd: Optional[int] = None) -> SendResult:
        """`assume_open` skips finding and opening the chat row.

        Only ever set by `batch()`, and only after a send to this same chat has
        already succeeded. It is still CHECKED — the conversation's own name is
        read back (~0.8s) before anything is typed, and a mismatch falls through
        to the full path. Skipping the check as well would save another second
        and risk the one failure worth avoiding above all others: a message
        typed into somebody else's conversation."""
        _t = [("start", time.monotonic())]
        if assume_open and known_hwnd:
            # The window handle does not change within a batch, and the
            # conversation's own name is the cheapest possible proof that the
            # right chat is still open — one read, no tree walk for the row.
            active = await self._reader.get_active_conversation_name_async(known_hwnd)
            _t.append(("guard", time.monotonic()))
            if active and (active.strip().lower() == chat_name.strip().lower()
                           or chat_names_match(chat_name, active)):
                return await self._fill_and_send(known_hwnd, chat_name,
                                                 message_text, _t)
        window_handle, row = await self.resolve_chat_row_async(chat_name)
        _t.append(("resolve", time.monotonic()))
        if window_handle is None:
            return SendResult.failed("WhatsApp Desktop is not running.")
        if row is None:
            return SendResult.failed(f"Chat '{chat_name}' could not be found.")

        opened = await self._sta.invoke_async(
            lambda: open_chat_sync(window_handle, row.raw_text, chat_name)
        )
        if not opened:
            return SendResult.failed(f"Could not open chat '{chat_name}'.")

        _t.append(("open", time.monotonic()))
        await asyncio.sleep(0.3)  # let the compose box swap to the new conversation

        active = await self._reader.get_active_conversation_name_async(window_handle)
        _t.append(("active", time.monotonic()))
        if active is None and not await self._sta.invoke_async(
            lambda: _conversation_header_matches(window_handle, chat_name)
        ):
            return SendResult.failed("Compose box not found after opening the chat.")

        return await self._fill_and_send(window_handle, chat_name, message_text, _t)

    async def _fill_and_send(self, window_handle: int, chat_name: str,
                             message_text: str, _t: list) -> SendResult:
        """Everything from "the right chat is open" to "the box came back empty".

        Shared by the normal path and by `batch()`'s already-open fast path, so
        the two cannot drift apart in how they fill, send or confirm."""
        filled, strategy = await self._sta.invoke_async(
            lambda: set_compose_text_sync(window_handle, message_text, self._use_clipboard)
        )
        _t.append(("fill", time.monotonic()))
        if not filled:
            # Clear up after a PARTIAL fill too: returning straight out leaves
            # whatever landed sitting in the box for the user to find, and the
            # next attempt appends to it.
            await self._sta.invoke_async(lambda: set_compose_text_sync(window_handle, "", self._use_clipboard))
            return SendResult.failed("Could not put the message into the compose box.")

        last_problem = ""
        for attempt in range(3):
            invoked = await self._sta.invoke_async(lambda: invoke_send_button_sync(window_handle))
            how = "send-button-invoke"
            if not invoked:
                how = "enter-key"
                invoked = await self._sta.invoke_async(lambda: press_enter_sync(window_handle))
            if not invoked:
                last_problem = "no Send button, and Enter could not be delivered"
            else:
                # WhatsApp clears the compose box when a message is actually
                # delivered — that empty box is the proof. Polled, because the
                # clear lags the click. "Still has text" is a genuine failure,
                # not a soft success: reporting it as sent is how a typed-but-
                # unsent message gets marked SENT and never retried.
                for _ in range(10):
                    await asyncio.sleep(0.25)
                    if await self._sta.invoke_async(lambda: compose_is_empty_sync(window_handle)):
                        _t.append(("confirm", time.monotonic()))
                        logger.debug("send phases: %s", " ".join(
                            f"{name}={int((t - _t[i][1]) * 1000)}ms"
                            for i, (name, t) in enumerate(_t[1:])))
                        return SendResult.succeeded(f"{strategy} + {how}",
                                                    window_handle=window_handle)
                leftover = await self._sta.invoke_async(
                    lambda: read_compose_text_sync(window_handle)
                )
                # Codepoints, because "still had text" alone can't distinguish
                # "it never sent" from "an invisible leftover makes an empty box
                # look full" — and those need opposite fixes.
                logger.warning(
                    "send: box not empty after %s — %d chars, codepoints %s",
                    how, len(leftover), [hex(ord(c)) for c in leftover[:12]],
                )
                last_problem = f"the compose box still had text after {how}"

            if attempt < 2:
                logger.warning("send attempt %d did not clear the box (%s) — retrying",
                               attempt + 1, last_problem)
                current = await self._sta.invoke_async(lambda: read_compose_text_sync(window_handle))
                # Normalized, not exact: an exact compare never matched a
                # multi-line message, so every attempt re-inserted the whole
                # thing instead of just pressing Send again.
                if _normalize_compose_text(current) != _normalize_compose_text(message_text):
                    await self._sta.invoke_async(
                        lambda: set_compose_text_sync(window_handle, message_text, self._use_clipboard)
                    )

        # Leave nothing half-written in the chat for the user to find (or for
        # the next message to be appended to).
        await self._sta.invoke_async(lambda: set_compose_text_sync(window_handle, "", self._use_clipboard))
        return SendResult.failed(f"Message was composed but not sent — {last_problem}.", strategy)
