"""Windows session, desktop and input state — the preconditions for automation.

Reading WhatsApp needs almost nothing: the UI Automation tree is served by the
target process and can be walked from any session, whether or not anyone is
looking at the screen.

**Sending needs an interactive desktop**, and this module is where that is
checked rather than discovered as a mysterious failure. The chain of
requirements, each verifiable here:

1. The process must be in a session that **has a desktop** (`WinSta0\\Default`).
2. That desktop must currently be **receiving input** — `OpenInputDesktop`
   succeeds only for the desktop the session's input is attached to. A locked
   workstation switches input to `WinSta0\\Winlogon`; a disconnected RDP session
   has no input desktop at all.
3. Keystrokes go to whatever window the OS considers **foreground**, so the
   target window must be activated first.

`SendInput` — which every keystroke ultimately uses — injects into the *calling
session's* input queue. When that session has no attached input desktop the
call still returns success and the events go nowhere. That silent success is
why a disconnected RDP session produces "sending stopped" with no error, and
why this module exists: to say so up front instead of letting a send fail
invisibly.

Everything here is read-only. Nothing in this module changes focus, moves the
cursor, or injects input.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

try:
    import win32gui

    _WIN32 = True
except ImportError:  # pragma: no cover - off-Windows
    _WIN32 = False

SM_REMOTESESSION = 0x1000
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
NO_CONSOLE_SESSION = 0xFFFFFFFF


class Health:
    OK = "ok"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SessionState:
    """A snapshot of everything that decides whether a send can work."""

    # -- process / session -------------------------------------------------
    session_id: int = -1
    console_session_id: int = -1
    is_console_session: bool = False
    is_remote_session: bool = False

    # -- desktop -----------------------------------------------------------
    input_desktop: str = ""          # "Default" when interactive, "" when none
    has_input_desktop: bool = False

    # -- automation --------------------------------------------------------
    uia_available: bool = False
    uia_error: str = ""

    # -- WhatsApp ----------------------------------------------------------
    whatsapp_found: bool = False
    whatsapp_hwnd: int = 0
    whatsapp_minimised: bool = False
    whatsapp_foreground: bool = False

    # -- user --------------------------------------------------------------
    user_idle_seconds: float = 0.0

    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- verdicts ----------------------------------------------------------

    @property
    def can_read(self) -> str:
        """Reading needs the tree and the window — not a desktop, not focus."""
        if not self.uia_available:
            return Health.BLOCKED
        if not self.whatsapp_found:
            return Health.BLOCKED
        return Health.OK

    @property
    def can_send(self) -> str:
        """Sending needs everything reading needs, plus an interactive desktop
        to inject keystrokes into."""
        if self.can_read == Health.BLOCKED:
            return Health.BLOCKED
        if not self.has_input_desktop:
            return Health.BLOCKED
        if self.whatsapp_minimised:
            # Recoverable: the window is restored as part of a send.
            return Health.DEGRADED
        return Health.OK

    @property
    def send_blocked_reason(self) -> str:
        if not self.uia_available:
            return f"UI Automation unavailable — {self.uia_error or 'not initialised'}"
        if not self.whatsapp_found:
            return "WhatsApp Desktop is not running"
        if not self.has_input_desktop:
            if self.is_remote_session:
                return ("This RDP session has no input desktop — the session is disconnected "
                        "or locked. Keystrokes injected now would silently go nowhere, so "
                        "sending is held until it reconnects.")
            return ("The session has no input desktop (locked or switched). Sending is held "
                    "until it returns.")
        return ""

    def summary(self) -> list[tuple[str, str, str]]:
        """(label, value, health) rows for the status display."""
        return [
            ("WhatsApp",
             "Connected" if self.whatsapp_found else "Not running",
             Health.OK if self.whatsapp_found else Health.BLOCKED),
            ("Desktop session",
             self._desktop_text(),
             Health.OK if self.has_input_desktop else Health.BLOCKED),
            ("Session type",
             f"RDP (session {self.session_id})" if self.is_remote_session
             else f"Console (session {self.session_id})",
             Health.OK if not self.is_remote_session else Health.DEGRADED),
            ("UI Automation",
             "Available" if self.uia_available else (self.uia_error or "Unavailable"),
             Health.OK if self.uia_available else Health.BLOCKED),
            ("Sender",
             {Health.OK: "Ready", Health.DEGRADED: "Ready (window minimised)",
              Health.BLOCKED: "Blocked"}[self.can_send],
             self.can_send),
        ]

    def _desktop_text(self) -> str:
        if self.has_input_desktop:
            return f"Active ({self.input_desktop})"
        return "Disconnected / locked"


def _session_ids() -> tuple[int, int]:
    kernel32 = ctypes.windll.kernel32
    session_id = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id))
    try:
        console = kernel32.WTSGetActiveConsoleSessionId()
    except Exception:  # noqa: BLE001
        console = NO_CONSOLE_SESSION
    return session_id.value, console


def _input_desktop_name() -> str:
    """The desktop currently receiving input, or "" when there isn't one.

    `OpenInputDesktop` is the honest test. It fails when the session's input is
    not attached to any desktop this process may open — a disconnected RDP
    session, a locked workstation (input is on `Winlogon`), or a UAC secure
    desktop. That is precisely the condition under which injected keystrokes
    vanish, which makes it the right precondition for a send."""
    user32 = ctypes.windll.user32
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if user32.GetUserObjectInformationW(
            handle, UOI_NAME, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
        ):
            return buffer.value
        return "unknown"
    finally:
        user32.CloseDesktop(handle)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def user_idle_seconds() -> float:
    """How long since the user last touched keyboard or mouse.

    Used to pick a moment for the one unavoidable interruption. `GetLastInputInfo`
    reports the calling *session's* input, which is what matters here. Returns
    0.0 when it can't be determined, so an unknown state is treated as "the user
    is busy" rather than as permission to interrupt."""
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        elapsed_ms = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, elapsed_ms / 1000.0)
    except Exception:  # noqa: BLE001
        return 0.0


def probe(window_title_hint: str = "WhatsApp") -> SessionState:
    """Read the whole precondition chain. Cheap enough to call every cycle."""
    if not _WIN32:
        return SessionState(uia_available=False, uia_error="Windows-only APIs unavailable")

    notes: list[str] = []
    session_id, console = _session_ids()
    desktop = _input_desktop_name()
    remote = bool(ctypes.windll.user32.GetSystemMetrics(SM_REMOTESESSION))

    uia_available, uia_error = False, ""
    try:
        import uiautomation as auto

        # Touching the root proves the client side of UI Automation is alive,
        # without depending on WhatsApp being there.
        uia_available = auto.GetRootControl() is not None
    except Exception as ex:  # noqa: BLE001
        uia_error = f"{type(ex).__name__}: {ex}"

    hwnd = 0
    try:
        from wadam.whatsapp.reader import find_window_sync

        hwnd = find_window_sync(window_title_hint) or 0
    except Exception as ex:  # noqa: BLE001
        notes.append(f"window lookup failed: {ex}")

    minimised = foreground = False
    if hwnd:
        try:
            minimised = bool(win32gui.IsIconic(hwnd))
            foreground = win32gui.GetForegroundWindow() == hwnd
        except Exception:  # noqa: BLE001
            pass

    if remote and session_id != console:
        notes.append(
            "Running in an RDP session that is not the console session. Sending works "
            "while the RDP client is connected and the session unlocked; it is held "
            "automatically when it is not."
        )
    if desktop and desktop.lower() != "default":
        notes.append(f"Input is on the {desktop!r} desktop (locked or a secure prompt).")

    return SessionState(
        session_id=session_id,
        console_session_id=console if console != NO_CONSOLE_SESSION else -1,
        is_console_session=session_id == console,
        is_remote_session=remote,
        input_desktop=desktop,
        has_input_desktop=bool(desktop),
        uia_available=uia_available,
        uia_error=uia_error,
        whatsapp_found=bool(hwnd),
        whatsapp_hwnd=hwnd,
        whatsapp_minimised=minimised,
        whatsapp_foreground=foreground,
        user_idle_seconds=user_idle_seconds(),
        notes=tuple(notes),
    )
