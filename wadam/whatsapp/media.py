"""Getting an attachment's BYTES out of WhatsApp.

The reader can name an attachment but never hand over its contents: WhatsApp
Desktop is WebView2 and media lives as blobs inside IndexedDB, so there is no
file on disk to copy. Measured on this machine — no media-shaped file anywhere
under the app's package, 2.6 MB of IndexedDB and nothing else.

So the only way to get bytes is to make WhatsApp write them out, using the
controls it already offers. Two shapes, both confirmed live:

    a document   the bubble has an action strip directly beneath it
                 'Save as...'  when already downloaded
                 'Download'    when not — then 'Save as...' appears

    a photo      no inline action at all, only 'Open picture'
                 -> the viewer that opens carries its own 'Save as...'
                 -> Escape closes the viewer and leaves the chat as it was

**The two routes differ in a way that matters.** A document's inline
`Save as...` writes straight into the browser's download folder, silently. The
picture viewer's `Save as...` opens a real Save As dialog and waits.

That is not a detail: the first attempt treated both the same, so the photo run
opened a dialog, timed out waiting for a file that was never going to appear,
and then pressed Escape — which cancelled the save. A dialog left open is worse
still, because it is modal and WhatsApp is unusable behind it.

So a save waits for EITHER outcome — a file, or a dialog — and drives the
dialog when one appears. Any dialog still standing at the end is closed, but
that is the failure path, not the normal one.

The action buttons are paired to their bubble by GEOMETRY, not by walking the
bubble's subtree: WhatsApp renders the strip as a sibling row about six pixels
below, so a subtree walk finds nothing at all.

Everything here is an INTERACTION. It foregrounds the window, opens overlays
and presses Escape, so it belongs on the worker under the action lock, never in
the passive poll.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto

    _UIA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off-Windows
    _UIA_AVAILABLE = False

#: The strip sits directly under its bubble. Measured: a six-pixel gap, so the
#: allowance is generous enough for a different DPI and tight enough that the
#: next message's strip can never be picked up.
ACTION_GAP = 30

#: How long to wait for a file to appear after asking for one. A 49 MB document
#: needs longer than a photo, and an attachment that never arrives must not
#: hold the worker forever.
SAVE_TIMEOUT = 45.0
SAVE_POLL = 0.4

#: How long to wait for 'Save as...' to replace 'Download' on a bubble that had
#: to be fetched first.
DOWNLOAD_TIMEOUT = 90.0

SAVE_ACTION = "save as"
DOWNLOAD_ACTION = "download"
OPEN_PICTURE_ACTIONS = ("open picture", "open photo")

#: A size suffix is what tells a media bubble from an ordinary one — the name
#: reads 'ENV .env ENV · 2 kB 11:15 AM Read'. Photos carry no size, and are
#: found through their 'Open picture' button instead.
_SIZE_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:bytes|kB|KB|MB|GB)\b")
#: Leading type tag and trailing timestamp/receipt, stripped to get a filename.
_TYPE_PREFIX_RE = re.compile(r"^[A-Z0-9]{1,6}\s+")
_TRAILING_RE = re.compile(
    r"\s*[·•]?\s*\d+(?:[.,]\d+)?\s*(?:bytes|kB|KB|MB|GB)\b.*$", re.IGNORECASE)
#: The type tag appears at BOTH ends — 'ENV .env ENV · 2 kB 11:15 AM Read' —
#: so stripping only the leading one produced files called '.env ENV'.
_TYPE_SUFFIX_RE = re.compile(r"\s+[A-Z0-9]{1,6}$")
#: Characters Windows will not accept in a filename. An attachment is named by
#: whoever sent it, so the name is not handed to the filesystem unexamined.
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str) -> str:
    # Trailing dots and spaces only. Stripping BOTH ends turned `.env` into
    # `env`, which is a different file — a leading dot is part of the name.
    cleaned = _UNSAFE_RE.sub("_", (name or "").strip()).rstrip(". ")
    return cleaned[:120]


def _wait_for_path(path: Path, timeout: float) -> bool:
    """Wait for one exact file, settled.

    Used after driving the dialog, where the destination is known rather than
    guessed at. Size-settling matters: a large attachment appears immediately
    and fills in afterwards."""
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        time.sleep(SAVE_POLL)
        if not path.exists():
            continue
        size = path.stat().st_size
        if size and size == last:
            return True
        last = size
    return path.exists()


@dataclass(frozen=True)
class MediaTarget:
    """One attachment on screen, and the control that will produce a file."""

    label: str          #: the bubble's accessible name, as WhatsApp wrote it
    action: str         #: 'Save as...', 'Download' or 'Open picture'
    control: object     #: the button to invoke
    is_photo: bool = False

    @property
    def suggested_name(self) -> str:
        """A filename from the bubble's own text.

        WhatsApp names the saved file itself and does it lossily — a `.env`
        attachment landed as `env`, with the dot gone. The bubble carries the
        real name, so it is taken from there instead."""
        name = _TRAILING_RE.sub("", self.label).strip()
        name = _TYPE_PREFIX_RE.sub("", name).strip()
        # WhatsApp writes the type tag TWICE — 'ENV .env ENV · 2 kB …' — so
        # stripping only the leading one left files called '.env ENV'.
        name = _TYPE_SUFFIX_RE.sub("", name).strip()
        if self.is_photo:
            # A photo's only label is its button text — 'Open picture' — which
            # is not a filename. Named by when it was saved instead, and the
            # extension comes from the dialog rather than a guess about format.
            return time.strftime("photo-%Y%m%d-%H%M%S")
        return _safe_filename(name) or "attachment"

    @property
    def needs_download_first(self) -> bool:
        return self.action.lower().rstrip(".").strip() == DOWNLOAD_ACTION


def downloads_folder() -> Path:
    """Where `Save as...` puts things. The browser's download folder, which is
    a Windows setting rather than something this application chooses."""
    return Path(os.path.expandvars(r"%USERPROFILE%\Downloads"))


def _action_of(name: str) -> str:
    return (name or "").lower().rstrip(".").strip()


def find_media_targets_sync(window_handle: int) -> list[MediaTarget]:
    """Every attachment visible in the open conversation, with its action.

    Read-only: it walks the tree and presses nothing."""
    if not _UIA_AVAILABLE:
        return []
    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return []
    try:
        rect = root.BoundingRectangle
        pane_left = rect.left + (rect.right - rect.left) * 0.33
    except Exception:  # noqa: BLE001
        return []

    bubbles: list[tuple[str, object]] = []
    actions: list[tuple[str, object, object]] = []
    photos: list[tuple[str, object]] = []

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 40:
            return
        for child in ctrl.GetChildren():
            try:
                kind = child.ControlTypeName
                name = (child.Name or "").strip()
                rect_ = child.BoundingRectangle
            except Exception:  # noqa: BLE001
                continue
            if name and rect_.left >= pane_left:
                verb = _action_of(name)
                if kind == "ButtonControl" and verb in OPEN_PICTURE_ACTIONS:
                    photos.append((name, child))
                elif kind == "ButtonControl" and verb in (SAVE_ACTION, DOWNLOAD_ACTION):
                    actions.append((name, rect_, child))
                elif _SIZE_RE.search(name) and len(name) > 12:
                    # The bubble itself, not the bare '49 MB' label beside it.
                    bubbles.append((name, rect_))
            walk(child, depth + 1)

    walk(root)

    targets: list[MediaTarget] = []
    seen: set[tuple[str, str]] = set()
    for label, brect in bubbles:
        for action_name, arect, control in actions:
            below = 0 <= (arect.top - brect.bottom) <= ACTION_GAP
            overlaps = arect.left < brect.right and arect.right > brect.left
            if below and overlaps:
                key = (label, _action_of(action_name))
                if key in seen:
                    continue
                seen.add(key)
                targets.append(MediaTarget(label=label, action=action_name,
                                           control=control))
    for label, control in photos:
        key = (label, "photo")
        if key in seen:
            continue
        seen.add(key)
        targets.append(MediaTarget(label=label, action=label, control=control,
                                   is_photo=True))
    return targets


def _invoke(control) -> bool:
    try:
        pattern = control.GetPattern(auto.PatternId.InvokePattern)
        if pattern is not None:
            pattern.Invoke()
            return True
        control.Click(simulateMove=False)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("could not invoke a media control", exc_info=True)
        return False


def _save_buttons(root) -> list[tuple[tuple, object]]:
    """Every 'Save as...' button in the window, keyed by position.

    Position is the key because there is no stable identity here and the
    conversation behind an overlay keeps its own buttons: searching the whole
    window for the first 'Save as...' after opening a picture found the
    DOCUMENT's button further up the chat and saved that instead — a photo came
    out as 1757 bytes of somebody's `.env`."""
    found: list[tuple[tuple, object]] = []

    def walk(ctrl, depth: int = 0) -> None:
        if depth > 40:
            return
        for child in ctrl.GetChildren():
            try:
                if (child.ControlTypeName == "ButtonControl"
                        and _action_of(child.Name or "") == SAVE_ACTION):
                    rect = child.BoundingRectangle
                    found.append(((rect.left, rect.top, rect.right, rect.bottom), child))
            except Exception:  # noqa: BLE001
                pass
            walk(child, depth + 1)

    walk(root)
    return found


def _new_save_button(window_handle: int, before: set):
    """The save control that APPEARED — i.e. the overlay's own, not one that
    was already on screen behind it."""
    for position, control in _save_buttons(auto.ControlFromHandle(window_handle)):
        if position not in before:
            return control
    return None


def _wait_for_new_file(folder: Path, before: set, timeout: float) -> Optional[Path]:
    """The file `Save as...` wrote, once it has stopped growing.

    Size-settling matters: a large attachment appears immediately and fills in
    afterwards, so moving it on sight would move a fragment."""
    deadline = time.monotonic() + timeout
    candidate: Optional[Path] = None
    last_size = -1
    while time.monotonic() < deadline:
        time.sleep(SAVE_POLL)
        try:
            current = {p for p in folder.iterdir() if p.is_file()}
        except OSError:
            continue
        fresh = [p for p in current if p.name not in before
                 and not p.name.endswith((".crdownload", ".tmp", ".partial"))]
        if not fresh:
            continue
        candidate = max(fresh, key=lambda p: p.stat().st_mtime)
        size = candidate.stat().st_size
        if size and size == last_size:
            return candidate
        last_size = size
    return candidate


def save_media_sync(window_handle: int, target: MediaTarget,
                    destination: Path) -> Optional[Path]:
    """Make WhatsApp write one attachment out, and move it where we want it.

    Returns the final path, or None. Any viewer this opened is closed before
    returning, whatever happened — an overlay left up is the user's WhatsApp
    made unusable."""
    if not _UIA_AVAILABLE:
        return None
    downloads = downloads_folder()
    if not downloads.is_dir():
        logger.warning("no download folder at %s — cannot save media", downloads)
        return None
    try:
        before = {p.name for p in downloads.iterdir() if p.is_file()}
    except OSError:
        return None

    root = auto.ControlFromHandle(window_handle)
    if root is None:
        return None

    opened_viewer = False
    try:
        if target.is_photo:
            # A photo has no inline action; its viewer carries the save. The
            # buttons already on screen are noted first so the one that APPEARS
            # can be told from the ones the conversation behind already had.
            existing = {pos for pos, _ in _save_buttons(root)}
            if not _invoke(target.control):
                return None
            opened_viewer = True
            deadline = time.monotonic() + 10
            button = None
            while time.monotonic() < deadline:
                time.sleep(0.5)
                button = _new_save_button(window_handle, existing)
                if button is not None:
                    break
            if button is None:
                logger.warning("the picture viewer offered no save control")
                return None
            if not _invoke(button):
                return None
        elif target.needs_download_first:
            if not _invoke(target.control):
                return None
            # 'Download' fetches into WhatsApp; the save only becomes available
            # once it has finished, which for a large file is not immediate.
            existing = {pos for pos, _ in _save_buttons(root)}
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT
            button = None
            while time.monotonic() < deadline:
                time.sleep(1.0)
                button = _new_save_button(window_handle, existing)
                if button is not None:
                    break
            if button is None:
                logger.warning("download did not produce a save control in time")
                return None
            if not _invoke(button):
                return None
        else:
            if not _invoke(target.control):
                return None

        # Either outcome is legitimate, and which one depends on the route: a
        # document saves silently, the picture viewer asks. Waiting for only
        # one of them is what made the first photo attempt cancel itself.
        destination.mkdir(parents=True, exist_ok=True)
        wanted = _unique(destination / (target.suggested_name or "attachment"))
        deadline = time.monotonic() + SAVE_TIMEOUT
        while time.monotonic() < deadline:
            dialog = find_save_dialog()
            if dialog is not None:
                saved_to = _drive_save_dialog(dialog, wanted)
                if saved_to is None:
                    return None
                if _wait_for_path(saved_to, SAVE_TIMEOUT):
                    return saved_to
                return None
            landed = _wait_for_new_file(downloads, before, 2.0)
            if landed is not None:
                try:
                    shutil.move(str(landed), str(wanted))
                except OSError as ex:
                    logger.error("could not move %s to %s: %s", landed, wanted, ex)
                    return None
                return wanted
        logger.warning("neither a file nor a save dialog appeared")
        return None
    finally:
        # The failure path only. A dialog still standing means the save did not
        # go through, and leaving a modal window up makes WhatsApp unusable.
        stuck = find_save_dialog()
        if stuck is not None:
            logger.warning("closing a save dialog that was left open")
            try:
                auto.SendKeys("{Esc}", waitTime=0.2)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        if opened_viewer:
            try:
                auto.SendKeys("{Esc}", waitTime=0.2)
            except Exception:  # noqa: BLE001
                logger.debug("could not close the picture viewer", exc_info=True)
            time.sleep(0.5)


def find_save_dialog():
    """The Save As dialog, if one is on screen.

    Found by window CLASS (`#32770`, the Windows common dialog) rather than by
    title, because the title is localised and a match on the word "save" would
    also catch an ordinary window that happens to be called something else."""
    try:
        import win32gui
    except ImportError:  # pragma: no cover - off-Windows
        return None
    handles: list[int] = []

    def visit(handle, _param):
        try:
            if win32gui.IsWindowVisible(handle) and win32gui.GetClassName(handle) == "#32770":
                handles.append(handle)
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:  # noqa: BLE001
        return None
    return handles[0] if handles else None


def _drive_save_dialog(handle: int, target: Path) -> Optional[Path]:
    """Type an exact path into the dialog and confirm it.

    Giving it the FULL path rather than a filename means the destination is
    settled here, so nothing has to be moved out of the download folder
    afterwards and there is no window in which a second save could collide."""
    try:
        dialog = auto.ControlFromHandle(handle)
        if dialog is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        edit = dialog.EditControl(searchDepth=8)
        if not edit.Exists(4, 0.4):
            logger.warning("the save dialog had no filename field")
            return None
        # Keep the dialog's own extension when ours has none. WhatsApp knows
        # whether it is writing a jpg or a png; typing the wrong extension
        # would mislabel the file rather than convert it.
        if not target.suffix:
            try:
                prefilled = (edit.GetValuePattern().Value or "").strip()
            except Exception:  # noqa: BLE001
                prefilled = ""
            suffix = Path(prefilled).suffix
            if suffix:
                target = target.with_suffix(suffix)
        try:
            edit.GetValuePattern().SetValue(str(target))
        except Exception:  # noqa: BLE001
            edit.Click(simulateMove=False)
            auto.SendKeys("{Ctrl}a", waitTime=0.1)
            auto.SendKeys(str(target), waitTime=0.05)
        time.sleep(0.3)
        auto.SendKeys("{Enter}", waitTime=0.2)
        return target
    except Exception:  # noqa: BLE001
        logger.debug("could not drive the save dialog", exc_info=True)
        return None


def _unique(path: Path) -> Path:
    """`report.pdf`, `report (2).pdf`, … — two people sending the same filename
    is ordinary, and silently overwriting the first is not recoverable."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")
