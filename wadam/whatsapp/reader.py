"""Reading WhatsApp Desktop through UI Automation. No OCR, no screenshots.

WhatsApp Desktop is Chromium, and its "Chat list" is a virtualized DataGrid: a
plain tree walk (`GetChildren()`) sees zero rows, but `GridPattern.GetItem(row, 0)`
returns rows whose accessible Name already contains the contact name, last
message preview, timestamp and unread count. That is the whole basis of this
reader — the accessibility tree hands over everything the sidebar shows, with
no pixel inspection anywhere.

Two live-verified caveats shape the code:

* `GridPattern.RowCount` reports the list's full logical row count, but
  `GetItem()` only succeeds for rows Chromium has realized near the current
  scroll position — past that it raises COMError. So a read returns the chats
  currently on screen plus a nearby buffer, not the entire history. The deep
  read scrolls to reach further.
* "Search results." is the mirror image: `GetItem` throws for every row, but
  all rows are present as direct `DataItem` children. Hence
  `_iter_grid_row_controls` tries both.

WhatsApp re-renders its React tree whenever a chat is switched or a message
arrives, which kills UIA elements mid-walk; reading a property on one then
raises COMError. Those are transient — the next poll succeeds — so every sync
read absorbs them and returns a benign default rather than failing a whole
cycle.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from wadam.whatsapp.row_parser import parse_chat_row
from wadam.whatsapp.sta_thread import StaAutomationThread

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    import win32gui
    import win32process

    _WIN32_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _WIN32_AVAILABLE = False

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

try:
    from _ctypes import COMError
except ImportError:  # pragma: no cover - off-Windows
    class COMError(Exception):  # type: ignore[no-redef]
        pass


_WHATSAPP_PROCESS_NAMES = {"whatsapp.exe", "whatsapp.root.exe"}

RECENTS_GRID = "Chat list"
SEARCH_RESULTS_GRID = "Search results."


class WhatsAppUnavailableError(RuntimeError):
    """Raised when uiautomation/pywin32 aren't available (i.e. not on Windows)."""


def _absorb_com_errors(default):
    def wrap(func):
        @functools.wraps(func)
        def guarded(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except COMError:
                logger.debug("transient UIA COMError in %s — returning default", func.__name__)
                return default() if callable(default) else default
        return guarded
    return wrap


_SELF_SENDER_LABEL = "You:"
# A time PREFIX, used to DROP a timestamp from message text.
_MESSAGE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[ap]m\b", re.IGNORECASE)
# A full time LABEL ("9:21 pm"), used to lift a bubble's own timestamp out.
_TIME_LABEL_RE = re.compile(r"^\d{1,2}:\d{2}\s*[ap]m$", re.IGNORECASE)
# A bare voice/video duration ("0:12"), distinguished from a clock time.
_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")

MEDIA_PHOTO = "photo"
MEDIA_VOICE = "voice"
MEDIA_VIDEO = "video"
MEDIA_DOCUMENT = "document"
MEDIA_STICKER = "sticker"
MEDIA_GIF = "gif"

# Signal words in a bubble's control names that identify each attachment kind.
# Order matters: more specific kinds precede the generic photo.
_MEDIA_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MEDIA_VOICE, ("voice message", "play voice", "voice note")),
    (MEDIA_VIDEO, ("play video", "video message")),
    (MEDIA_GIF, ("gif",)),
    (MEDIA_STICKER, ("sticker",)),
    (MEDIA_DOCUMENT, ("download", "document", "open document")),
    (MEDIA_PHOTO, ("open photo", "photo", "image", "view photo")),
)

_MEDIA_PLACEHOLDERS = {
    MEDIA_PHOTO: "Photo",
    MEDIA_VOICE: "Voice note",
    MEDIA_VIDEO: "Video",
    MEDIA_DOCUMENT: "Document",
    MEDIA_STICKER: "Sticker",
    MEDIA_GIF: "GIF",
}

_FILENAME_RE = re.compile(r"^[\w .()\-]+\.[A-Za-z0-9]{1,8}$")

# WhatsApp renders chrome INTO the message list that nobody actually sent: the
# encryption banner, date dividers, "You deleted this message". They read back
# exactly like messages, so without this filter they get stored, sent to the
# webhook, and answered.
_CHROME_SUBSTRINGS = (
    "end-to-end encrypted",
    "click to learn more",
    "tap to learn more",
    "this business works with other companies",
)
_CHROME_PREFIXES = (
    "you changed the group name",
    "you changed this group's icon",
    "you created group",
    "your security code with",
    "messages and calls are",
    "messages to yourself are",
)
# Whole-line matches only — a date divider reads as the bare word "Today", but
# "Today I went out" is a real message.
_CHROME_EXACT = frozenset({
    "today", "yesterday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "you deleted this message", "this message was deleted",
})

# System placeholders that render as a bubble but carry nothing readable.
_SYSTEM_NOTICE_PREFIXES = ("You received a view once message",)

_NON_SENDER_BUTTON_WORDS = ("forward", "delivered", "read", "download", "play", "react", "reply")
_MARKER_TEXTS = {"Read", "Edited"}

# Section labels WhatsApp injects into the search grid (they aren't chats).
_SEARCH_SECTION_HEADERS = {
    "chats", "groups in common", "messages", "contacts",
    "contacts on whatsapp", "other contacts",
}


def is_system_notice(text: str) -> bool:
    """Is this WhatsApp's own UI chrome rather than something a person sent?"""
    value = (text or "").strip().lower().rstrip(".")
    if not value:
        return False
    if value in _CHROME_EXACT:
        return True
    if any(value.startswith(p) for p in _CHROME_PREFIXES):
        return True
    return any(s in value for s in _CHROME_SUBSTRINGS)


def media_placeholder(media_kind: str, media_note: str = "") -> str:
    """The readable stand-in stored as a media message's text, e.g.
    "[Voice note · 0:12]" or "[Document: report.pdf]"."""
    label = _MEDIA_PLACEHOLDERS.get(media_kind)
    if not label:
        return ""
    note = (media_note or "").strip()
    if media_kind == MEDIA_VOICE and note:
        return f"[{label} · {note}]"
    if media_kind == MEDIA_DOCUMENT and note:
        return f"[{label}: {note}]"
    if media_kind == MEDIA_PHOTO and note:
        return f"[{label}] {note}"
    return f"[{label}]"


@dataclass(frozen=True)
class WhatsAppMessage:
    """One message bubble read from the open conversation.

    `media_kind`/`media_note` describe an attachment the accessibility tree can
    only *name*, never hand over the bytes of. `text` already carries a readable
    placeholder for media, so the webhook payload is never empty for a photo or
    voice note. `time_text` is the bubble's own clock label as WhatsApp renders
    it — the message's moment, not the moment we noticed it."""

    sender: str
    text: str
    is_incoming: bool
    media_kind: str = ""
    media_note: str = ""
    time_text: str = ""


@dataclass(frozen=True)
class ChatRow:
    """One row of the sidebar chat list."""

    chat_name: str
    timestamp_text: str = ""
    last_message: str = ""
    unread_count: int = 0
    is_pinned: bool = False
    is_muted: bool = False
    is_starred: bool = False
    is_draft: bool = False
    looks_like_group: bool = False
    raw_text: str = ""


class WhatsAppReader:
    """The async face of the reader. Every method marshals onto the STA thread."""

    def __init__(self, sta: StaAutomationThread, window_title_hint: str = "WhatsApp") -> None:
        self._sta = sta
        self._title_hint = window_title_hint

    async def find_window_async(self) -> Optional[int]:
        return await self._sta.invoke_async(lambda: find_window_sync(self._title_hint))

    async def read_chat_rows_async(self, window_handle: int) -> list[ChatRow]:
        return await self._sta.invoke_async(lambda: read_chat_rows_sync(window_handle))

    async def read_chat_rows_deep_async(self, window_handle: int, max_scrolls: int = 8) -> list[ChatRow]:
        return await self._sta.invoke_async(
            lambda: read_chat_rows_deep_sync(window_handle, max_scrolls)
        )

    async def get_active_conversation_name_async(self, window_handle: int) -> Optional[str]:
        return await self._sta.invoke_async(lambda: get_active_conversation_name_sync(window_handle))

    async def read_recent_messages_async(self, window_handle: int, limit: int = 25) -> list[WhatsAppMessage]:
        return await self._sta.invoke_async(lambda: read_recent_messages_sync(window_handle, limit))

    async def read_open_conversation_async(self, window_handle: int, limit: int = 25):
        """(active_conversation_name, recent_messages) for whatever chat is
        already open, in one STA round-trip. Opens nothing and takes no
        foreground — the cheap read the poll leans on."""
        return await self._sta.invoke_async(lambda: read_open_conversation_sync(window_handle, limit))

    async def window_is_alive_async(self, window_handle: int) -> bool:
        return await self._sta.invoke_async(lambda: window_is_alive_sync(window_handle))


# ---------------------------------------------------------------------------
# Window discovery
# ---------------------------------------------------------------------------


def _require_win32() -> None:
    if not _WIN32_AVAILABLE:
        raise WhatsAppUnavailableError(
            "uiautomation + pywin32 are required, and are only available on Windows."
        )


def find_window_sync(title_hint: str = "WhatsApp") -> Optional[int]:
    """The WhatsApp Desktop main window handle, or None.

    Matched by PROCESS first (WhatsApp.exe), because the window title changes
    with the open chat and with unread counts. `title_hint` only breaks a tie
    when the process owns several visible titled windows."""
    _require_win32()
    candidates: list[int] = []

    def _callback(hwnd: int, _: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        if psutil is None:
            return True
        try:
            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 - process gone between enum and query
            return True
        if name.lower() in _WHATSAPP_PROCESS_NAMES and win32gui.GetWindowText(hwnd):
            candidates.append(hwnd)
        return True

    win32gui.EnumWindows(_callback, None)
    if not candidates:
        return None
    if len(candidates) > 1 and title_hint:
        hint = title_hint.strip().lower()
        for hwnd in candidates:
            if hint in (win32gui.GetWindowText(hwnd) or "").lower():
                return hwnd
    return candidates[0]


def window_is_alive_sync(window_handle: int) -> bool:
    _require_win32()
    try:
        return bool(win32gui.IsWindow(window_handle) and win32gui.IsWindowVisible(window_handle))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Chat list
# ---------------------------------------------------------------------------


def _is_search_section_header(name: str) -> bool:
    return name.strip().lower() in _SEARCH_SECTION_HEADERS


def iter_grid_row_controls(chat_list) -> list:
    """Row controls of a chat-list-style DataGrid, via whichever mechanism that
    particular grid supports (see the module docstring)."""
    controls: list = []
    grid = chat_list.GetPattern(auto.PatternId.GridPattern)
    if grid is not None:
        try:
            row_count = grid.RowCount
        except Exception:  # noqa: BLE001
            row_count = 0
        for row_index in range(row_count):
            try:
                item = grid.GetItem(row_index, 0)
            except Exception:  # noqa: BLE001 - past the realized range
                break
            if item is not None:
                controls.append(item)
    if controls:
        return controls
    return [c for c in chat_list.GetChildren() if (c.Name or "").strip()]


def find_chat_grid(window_handle: int, grid_name: str = RECENTS_GRID):
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None
    chat_list = auto.Control(
        searchFromControl=root, searchDepth=40, Name=grid_name,
        ControlType=auto.ControlType.DataGridControl,
    )
    return chat_list if chat_list.Exists(2, 0.3) else None


def _row_from_control(item) -> Optional[ChatRow]:
    name = (item.Name or "").strip()
    if not name or _is_search_section_header(name):
        return None
    parsed = parse_chat_row(name)
    if not parsed["chat_name"]:
        return None
    return ChatRow(**parsed)


@_absorb_com_errors(list)
def read_chat_rows_sync(window_handle: int, grid_name: str = RECENTS_GRID) -> list[ChatRow]:
    _require_win32()
    chat_list = find_chat_grid(window_handle, grid_name)
    if chat_list is None and grid_name == RECENTS_GRID:
        # An active search HIDES the recents grid entirely on recent WhatsApp
        # builds — no chat DataGrid exists at all while a query sits in the
        # search box.
        #
        # This used to clear the search here, which meant the three-second POLL
        # could activate WhatsApp and inject Ctrl+A/Delete/Escape — a passive
        # read stealing the user's focus mid-keystroke. Reading is now
        # unconditionally input-free: the search results grid is read instead,
        # and clearing the search is left to the send path, which is allowed to
        # interact and is accounted for.
        search_results = find_chat_grid(window_handle, SEARCH_RESULTS_GRID)
        if search_results is not None:
            logger.debug("recents hidden by an active search — reading the results grid")
            chat_list = search_results
    if chat_list is None:
        return []

    rows: list[ChatRow] = []
    for item in iter_grid_row_controls(chat_list):
        row = _row_from_control(item)
        if row is not None:
            rows.append(row)
    return rows


def _scroll_chat_list_sync(chat_list, down: bool = True) -> bool:
    try:
        scroll = chat_list.GetPattern(auto.PatternId.ScrollPattern)
    except Exception:  # noqa: BLE001
        return False
    if scroll is None:
        return False
    try:
        if not scroll.VerticallyScrollable:
            return False
        amount = auto.ScrollAmount.LargeIncrement if down else auto.ScrollAmount.LargeDecrement
        scroll.Scroll(auto.ScrollAmount.NoAmount, amount)
        return True
    except Exception:  # noqa: BLE001
        return False


def _scroll_chat_list_to_top_sync(chat_list) -> None:
    try:
        scroll = chat_list.GetPattern(auto.PatternId.ScrollPattern)
        if scroll is not None and scroll.VerticallyScrollable:
            scroll.SetScrollPercent(-1, 0)
    except Exception:  # noqa: BLE001
        pass


@_absorb_com_errors(list)
def read_chat_rows_deep_sync(window_handle: int, max_scrolls: int = 8,
                             grid_name: str = RECENTS_GRID) -> list[ChatRow]:
    """Scroll the sidebar to reach chats below the initially-realized window,
    accumulating rows across steps (deduped by name, first-seen order kept).
    Stops as soon as a scroll surfaces nothing new, then returns the list to the
    top so the user's sidebar isn't left scrolled down.

    Used once at startup — the ordinary three-second poll stays shallow and
    cheap."""
    _require_win32()
    chat_list = find_chat_grid(window_handle, grid_name)
    if chat_list is None:
        return []

    by_name: dict[str, ChatRow] = {}
    order: list[str] = []

    def ingest() -> None:
        for item in iter_grid_row_controls(chat_list):
            row = _row_from_control(item)
            if row is not None and row.chat_name not in by_name:
                by_name[row.chat_name] = row
                order.append(row.chat_name)

    ingest()
    for _ in range(max(0, max_scrolls)):
        before = len(order)
        if not _scroll_chat_list_sync(chat_list, down=True):
            break
        time.sleep(0.35)  # let Chromium realize the next window of rows
        ingest()
        if len(order) == before:
            break
    _scroll_chat_list_to_top_sync(chat_list)
    return [by_name[name] for name in order]


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


@_absorb_com_errors(None)
def get_active_conversation_name_sync(window_handle: int) -> Optional[str]:
    """Which conversation is open, read from the compose box's accessible name
    ("Type a message to Alice")."""
    _require_win32()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None

    compose = auto.Control(
        searchFromControl=root, searchDepth=40, ControlType=auto.ControlType.EditControl,
        RegexName=r"^Type a message",
    )
    if not compose.Exists(1, 0.2):
        return None

    match = re.match(r"^Type a message to (.+)$", compose.Name or "")
    if not match:
        return None
    # Groups read as "Type a message to group <Name>"; show just the name.
    return re.sub(r"^group ", "", match.group(1))


# A dead element (WhatsApp re-rendered mid-walk) reads as empty/leafless rather
# than raising, so one stale node skips instead of failing the whole read.
def _safe_name(ctrl) -> str:
    try:
        return ctrl.Name or ""
    except Exception:  # noqa: BLE001
        return ""


def _safe_control_type(ctrl) -> str:
    try:
        return ctrl.ControlTypeName
    except Exception:  # noqa: BLE001
        return ""


def _safe_children(ctrl) -> list:
    try:
        return ctrl.GetChildren()
    except Exception:  # noqa: BLE001
        return []


@_absorb_com_errors(list)
def read_recent_messages_sync(window_handle: int, limit: int = 25) -> list[WhatsAppMessage]:
    """The most recent message bubbles from the open conversation, oldest-first.

    Two shapes, both confirmed live:
    - One-to-one chats tag each bubble with a GroupControl named "You:" or
      "<Contact>:".
    - Group chats carry no such label on other people's messages; each bubble is
      a DataItem row and who-sent-it comes from left/right alignment.

    The labelled read runs first. In a group it sees only OUR side (only our
    bubbles carry the label), so when it finds no incoming messages the bubble
    read is trusted instead if it saw more."""
    _require_win32()
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return []

    messages = _read_labeled_messages(root)
    if not messages or not any(m.is_incoming for m in messages):
        bubbles = _read_bubble_messages(root)
        if len(bubbles) > len(messages):
            messages = bubbles
    # Drop WhatsApp's own notices at the one point BOTH shapes pass through, so
    # nothing downstream ever stores or answers them.
    messages = [m for m in messages if not is_system_notice(m.text)]
    return messages[-max(1, limit):]


def read_open_conversation_sync(window_handle: int, limit: int = 25):
    active = get_active_conversation_name_sync(window_handle)
    messages = read_recent_messages_sync(window_handle, limit)
    return active, messages


def _read_labeled_messages(root) -> list[WhatsAppMessage]:
    """One-to-one shape. WhatsApp renders the conversation into the tree twice,
    so rows are deduped by on-screen position + text and sorted top-to-bottom so
    [-1] really is the newest."""
    labels: list = []

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 45:
            return
        if _safe_control_type(ctrl) == "GroupControl":
            name = _safe_name(ctrl).rstrip()
            if name.endswith(":") and name != "Infobar Container":
                labels.append((ctrl, name))
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(root)

    rows: list[tuple[int, WhatsAppMessage]] = []
    seen: set[tuple[int, str]] = set()
    for label, sender_label in labels:
        sender_label = sender_label.strip()
        base_text = _extract_bubble_text(label)
        # Media and the timestamp live on the whole bubble row (the label's
        # parent), not on the sender-label group.
        try:
            row = label.GetParentControl() or label
        except Exception:  # noqa: BLE001
            row = label
        display, media_kind, media_note, time_text = _enrich_media(row, base_text)
        if not display:
            continue
        try:
            top = label.BoundingRectangle.top
        except Exception:  # noqa: BLE001
            top = 0
        key = (top, display[:24])
        if key in seen:
            continue
        seen.add(key)
        rows.append((top, WhatsAppMessage(
            sender=sender_label.rstrip(":").strip(),
            text=display,
            is_incoming=sender_label != _SELF_SENDER_LABEL,
            media_kind=media_kind,
            media_note=media_note,
            time_text=time_text,
        )))
    rows.sort(key=lambda r: r[0])
    return [m for _top, m in rows]


def _read_bubble_messages(root) -> list[WhatsAppMessage]:
    """Group shape: each message is a leaf DataItem row outside the chat list,
    and who-sent-it is inferred from horizontal alignment (our messages hug the
    right side of the conversation pane)."""
    try:
        rect = root.BoundingRectangle
        win_left, win_width = rect.left, rect.right - rect.left
    except Exception:  # noqa: BLE001
        win_left, win_width = 0, 0
    # Text sitting right of ~60% of the window width is a message we sent. The
    # DataItem row spans the full width, so alignment must come from the text
    # controls inside it, not the row.
    outgoing_x = (win_left + win_width * 0.60) if win_width else None

    rows: list[tuple] = []
    seen: set[tuple[int, str]] = set()

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 50:
            return
        control_type = _safe_control_type(ctrl)
        if control_type == "DataGridControl" and _safe_name(ctrl) in (RECENTS_GRID, SEARCH_RESULTS_GRID):
            return  # the sidebar / search results, not messages
        if control_type == "DataItemControl" and not _contains_dataitem(ctrl):
            content = _bubble_item_content(ctrl)
            if content is not None:
                text, center_x, top, sender_hint, is_ours, media_kind, media_note, time_text = content
                # Our own bubble's "You" label sometimes surfaces as its own leaf
                # row; it isn't a message.
                if text.strip() == "You":
                    return
                key = (top, text[:24])
                if key not in seen:
                    seen.add(key)
                    rows.append((top, text, center_x, sender_hint, is_ours,
                                 media_kind, media_note, time_text))
            return  # leaf message row — don't descend further
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(root)
    # Tree-walk order is not visual order. On screen newer messages are lower,
    # so sort by vertical position — "the newest incoming message" depends on it.
    rows.sort(key=lambda r: r[0])

    messages: list[WhatsAppMessage] = []
    carried_sender = ""
    for _top, text, center_x, sender_hint, is_ours, media_kind, media_note, time_text in rows:
        if is_ours is True:  # a definitive "You:" label beats the alignment guess
            is_incoming = False
        else:
            is_incoming = not (outgoing_x is not None and center_x is not None and center_x > outgoing_x)
        if is_incoming:
            # In a group only the first message of a sender's run carries the
            # name; follow-ups inherit it, and our own message ends the run.
            if sender_hint:
                carried_sender = sender_hint
            sender = carried_sender
        else:
            sender = "You"
            carried_sender = ""
        messages.append(WhatsAppMessage(
            sender=sender, text=text, is_incoming=is_incoming,
            media_kind=media_kind, media_note=media_note, time_text=time_text,
        ))
    return messages


def _contains_dataitem(ctrl, depth: int = 0) -> bool:
    if depth > 12:
        return False
    for child in _safe_children(ctrl):
        if _safe_control_type(child) == "DataItemControl":
            return True
        if _contains_dataitem(child, depth + 1):
            return True
    return False


def _bubble_item_sender(item) -> str:
    """The sender name shown on a group bubble, if this row carries one. The
    name/avatar render as clickable buttons on the FIRST message of a person's
    run; status buttons ("9:21 pm Delivered", "Forward media") are filtered."""
    def walk(ctrl, depth=0):
        if depth > 8:
            return None
        if _safe_control_type(ctrl) == "ButtonControl":
            name = _safe_name(ctrl).strip()
            lowered = name.lower()
            if (
                name
                and len(name) <= 40
                and not _MESSAGE_TIME_RE.match(name)
                and not any(w in lowered for w in _NON_SENDER_BUTTON_WORDS)
            ):
                return name
        for child in _safe_children(ctrl):
            found = walk(child, depth + 1)
            if found:
                return found
        return None

    return walk(item) or ""


def _bubble_item_is_ours(item) -> Optional[bool]:
    """True when the row carries the definitive "You:" label; None when there's
    no label at all — alignment decides then."""
    def walk(ctrl, depth=0):
        if depth > 8:
            return False
        if _safe_control_type(ctrl) == "GroupControl" and _safe_name(ctrl).strip() == _SELF_SENDER_LABEL:
            return True
        return any(walk(child, depth + 1) for child in _safe_children(ctrl))

    return True if walk(item) else None


def _iter_message_parts(ctrl, depth: int = 0) -> list:
    """The row's content in document order: ("text", control) for TextControls
    and ("emoji", name) for inline emoji. WhatsApp renders emoji INSIDE a text
    message as ImageControls between the text runs, so reading only TextControls
    silently drops them. Quoted-reply subtrees are skipped, and WhatsApp's own
    "wds-ic-…" icon glyphs aren't emoji."""
    if depth > 8:
        return []
    control_type = _safe_control_type(ctrl)
    if control_type == "ButtonControl" and _safe_name(ctrl).startswith("Quoted"):
        return []
    found: list = []
    if control_type == "TextControl":
        found.append(("text", ctrl))
    elif control_type == "ImageControl":
        name = _safe_name(ctrl).strip()
        if name and len(name) <= 8 and not name.startswith("wds-ic"):
            found.append(("emoji", name))
    for child in _safe_children(ctrl):
        found.extend(_iter_message_parts(child, depth + 1))
    return found


def _iter_signal_names(ctrl, depth: int = 0) -> list[str]:
    """Lowercased names of the controls inside a bubble — the raw signal media
    classification matches against."""
    if depth > 10:
        return []
    names: list[str] = []
    if _safe_control_type(ctrl) in ("ButtonControl", "TextControl", "ImageControl", "GroupControl"):
        name = _safe_name(ctrl).strip().lower()
        if name:
            names.append(name)
    for child in _safe_children(ctrl):
        names.extend(_iter_signal_names(child, depth + 1))
    return names


def _media_note_for(kind: str, names: list[str], message_text: str) -> str:
    tokens = [t for t in re.split(r"\s+", message_text.strip()) if t]
    if kind in (MEDIA_VOICE, MEDIA_VIDEO):
        for candidate in tokens + names:
            if _DURATION_RE.match(candidate):
                return candidate
        return ""
    if kind == MEDIA_DOCUMENT:
        for candidate in tokens + names:
            if _FILENAME_RE.match(candidate):
                return candidate
        return ""
    label = _MEDIA_PLACEHOLDERS.get(kind, "").lower()
    caption = [t for t in tokens if t.lower() != label and not _DURATION_RE.match(t)]
    return " ".join(caption).strip()


def _classify_media(row, message_text: str) -> tuple[str, str]:
    names = _iter_signal_names(row)
    for kind, signals in _MEDIA_SIGNALS:
        if any(any(sig in name for name in names) for sig in signals):
            return kind, _media_note_for(kind, names, message_text)
    return "", ""


def _bubble_time(row) -> str:
    result = [""]

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 10 or result[0]:
            return
        if _TIME_LABEL_RE.match(_safe_name(ctrl).strip()):
            result[0] = _safe_name(ctrl).strip()
            return
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(row)
    return result[0]


def _largest_image_rect(row) -> Optional[tuple[int, int, int, int]]:
    """Screen rect of the biggest image in a bubble, ignoring tiny emoji/icon
    glyphs. Used only to judge alignment for a media message that has no text
    controls to measure."""
    best: Optional[tuple[int, tuple[int, int, int, int]]] = None

    def walk(ctrl, depth: int = 0) -> None:
        nonlocal best
        if depth > 10:
            return
        if _safe_control_type(ctrl) == "ImageControl":
            try:
                r = ctrl.BoundingRectangle
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > 400 and (best is None or area > best[0]):
                    best = (area, (int(r.left), int(r.top), int(r.right), int(r.bottom)))
            except Exception:  # noqa: BLE001
                pass
        for child in _safe_children(ctrl):
            walk(child, depth + 1)

    walk(row)
    return best[1] if best else None


def _media_center_x(row) -> Optional[float]:
    rect = _largest_image_rect(row)
    return (rect[0] + rect[2]) / 2 if rect else None


def _enrich_media(row, base_text: str):
    """Fold attachment + timestamp detail into a message:
    (display_text, media_kind, media_note, time_text)."""
    time_text = _bubble_time(row)
    kind, note = _classify_media(row, base_text)
    if not kind:
        return base_text, "", "", time_text
    display = media_placeholder(kind, note) or base_text
    return display, kind, note, time_text


def _bubble_item_content(item):
    """Content of a bubble row, or None when it carries no message.

    The horizontal centre comes from the TEXT controls (which are left/right
    aligned), not the full-width row. Media is folded in BEFORE the empty-text
    check: a photo or voice bubble has no text parts, so dropping empty rows
    first would silently discard every media message."""
    parts: list[str] = []
    lefts: list[int] = []
    rights: list[int] = []
    for kind_tag, payload in _iter_message_parts(item):
        if kind_tag == "emoji":
            parts.append(payload)
            continue
        value = _safe_name(payload).strip()
        if not value or value in _MARKER_TEXTS or _MESSAGE_TIME_RE.match(value):
            continue
        parts.append(value)
        try:
            r = payload.BoundingRectangle
            lefts.append(r.left)
            rights.append(r.right)
        except Exception:  # noqa: BLE001
            pass

    text = " ".join(parts).strip()
    if text.startswith(_SYSTEM_NOTICE_PREFIXES):
        return None
    display, media_kind, media_note, time_text = _enrich_media(item, text)
    if not display:
        return None
    center_x = (min(lefts) + max(rights)) / 2 if lefts and rights else _media_center_x(item)
    try:
        top = int(item.BoundingRectangle.top)
    except Exception:  # noqa: BLE001
        top = 0
    return (display, center_x, top, _bubble_item_sender(item),
            _bubble_item_is_ours(item), media_kind, media_note, time_text)


def _extract_bubble_text(sender_label_control) -> str:
    """Join the message TextControls alongside a sender label in its parent row,
    skipping the label itself, the timestamp, the "Read" marker and any
    quoted-reply preview."""
    try:
        row = sender_label_control.GetParentControl()
    except Exception:  # noqa: BLE001
        return ""
    if row is None:
        return ""

    parts: list[str] = []
    for child in _safe_children(row):
        child_name = _safe_name(child).rstrip()
        if child_name.endswith(":"):
            continue  # the sender label group
        if _safe_control_type(child) == "ButtonControl" and child_name.startswith("Quoted"):
            continue  # the quoted original of a reply, not the new text
        for kind_tag, payload in _iter_message_parts(child):
            if kind_tag == "emoji":
                parts.append(payload)
                continue
            value = _safe_name(payload).strip()
            if not value or value in _MARKER_TEXTS or _MESSAGE_TIME_RE.match(value):
                continue
            parts.append(value)
    return " ".join(parts).strip()
