"""What this build of WhatsApp can actually be driven with.

Every automation decision in this application rests on one uncomfortable fact:
**WhatsApp's UI Automation provider advertises write patterns it does not
implement.** `IUIAutomationValuePattern::SetValue` returns `S_OK`, reports
`IsReadOnly = False`, and discards the call — verified through raw COM with the
Python wrapper removed from the picture, so it is the provider, not the binding.

That is a property of a *version*, not of WhatsApp forever. If a future build
implements it, sending becomes completely invisible: no focus change, no
keystrokes, no cursor. Nobody should have to notice that and edit code.

So capabilities are **measured at startup, cached against the WhatsApp version
string, and re-measured when that version changes**. The sender consults the
result: rungs known to be dead are skipped (saving a second of pointless
retrying per send), and a rung that starts working is adopted automatically.

Two of the five cannot be tested without side effects, and are recorded as
`presence only` rather than pretended about:

* `SelectionItemPattern.Select()` on a chat row would switch the user's
  conversation.
* `InvokePattern.Invoke()` on the Send button would send a real message.

Their *presence* is checked; whether they work is learned from real use and
recorded by the sender.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROBE_TEXT = "​"  # zero-width space: shortest possible, invisible if it lands


@dataclass(frozen=True)
class Capabilities:
    """What worked, when, and against which build."""

    whatsapp_version: str = ""
    probed_at: str = ""

    # Tested for real (write something, read it back, put it back).
    value_pattern_write: bool = False
    legacy_set_value: bool = False

    # Presence only — exercising these has side effects the user would see.
    selection_item_present: bool = False
    invoke_send_present: bool = False
    text_pattern_read: bool = False

    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def headless_send_possible(self) -> bool:
        """True when text can reach the compose box with no input simulation.

        This is the single question the whole design turns on. While it is
        False, sending needs focus and keystrokes; the moment it is True, the
        entire input path can be skipped."""
        return self.value_pattern_write or self.legacy_set_value

    def summary(self) -> str:
        if self.headless_send_possible:
            how = "ValuePattern" if self.value_pattern_write else "LegacyIAccessible"
            return f"headless sending AVAILABLE via {how} on {self.whatsapp_version}"
        return (f"headless sending unavailable on {self.whatsapp_version} — "
                f"the provider accepts write calls and discards them")


def whatsapp_version() -> str:
    """The installed package version, used as the cache key.

    Read from the MSIX package rather than the window title: the title says
    "WhatsApp Beta" on this machine while the package is
    5319275A.WhatsAppDesktop 2.2630.102.0, and it is the build that determines
    behaviour."""
    try:
        import subprocess

        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage -Name '*WhatsApp*' | Select-Object -First 1).Version"],
            capture_output=True, text=True, timeout=15,
        )
        version = (result.stdout or "").strip()
        if version:
            return version
    except Exception as ex:  # noqa: BLE001
        logger.debug("could not read the WhatsApp package version: %s", ex)
    return "unknown"


def _probe_write(compose, pattern_id, setter_name: str) -> tuple[bool, str]:
    """Write, read back, restore. Returns (worked, note).

    Deliberately refuses to run unless the compose box is empty — probing must
    never destroy something the user was in the middle of typing."""
    import uiautomation as auto

    try:
        text_pattern = compose.GetPattern(auto.PatternId.TextPattern)
        before = (text_pattern.DocumentRange.GetText(-1) or "").strip() if text_pattern else ""
        if before:
            return False, "skipped — the compose box was not empty"

        pattern = compose.GetPattern(pattern_id)
        if pattern is None:
            return False, "pattern not offered"
        getattr(pattern, setter_name)(PROBE_TEXT)
        time.sleep(0.5)
        after = (text_pattern.DocumentRange.GetText(-1) or "") if text_pattern else ""
        worked = PROBE_TEXT in after
        if worked:
            try:
                getattr(pattern, setter_name)("")
            except Exception:  # noqa: BLE001
                pass
            return True, "write accepted and observed"
        return False, "call returned success, text never appeared (provider no-op)"
    except Exception as ex:  # noqa: BLE001
        return False, f"{type(ex).__name__}: {ex}"


def probe(window_handle: int) -> Capabilities:
    """Measure this build. Safe to call at startup; touches nothing if the
    compose box has content."""
    version = whatsapp_version()
    notes: list[str] = []
    try:
        import uiautomation as auto
    except ImportError:
        return Capabilities(whatsapp_version=version, notes=("uiautomation unavailable",))

    root = auto.ControlFromHandle(window_handle) if window_handle else None
    if root is None:
        return Capabilities(whatsapp_version=version, notes=("WhatsApp window not found",))

    compose = auto.Control(searchFromControl=root, searchDepth=40,
                           ControlType=auto.ControlType.EditControl,
                           RegexName=r"^Type a message")
    if not compose.Exists(2, 0.3):
        return Capabilities(whatsapp_version=version,
                            notes=("no conversation open — compose box not present",))

    value_ok, value_note = _probe_write(compose, auto.PatternId.ValuePattern, "SetValue")
    legacy_ok, legacy_note = _probe_write(
        compose, auto.PatternId.LegacyIAccessiblePattern, "SetValue")
    notes.append(f"ValuePattern.SetValue: {value_note}")
    notes.append(f"LegacyIAccessible.SetValue: {legacy_note}")

    text_ok = False
    try:
        text_ok = compose.GetPattern(auto.PatternId.TextPattern) is not None
    except Exception:  # noqa: BLE001
        pass

    # Presence only — see the module docstring for why these are not exercised.
    selection_present = invoke_present = False
    try:
        from wadam.whatsapp.reader import find_chat_grid, iter_grid_row_controls

        grid = find_chat_grid(window_handle)
        rows = iter_grid_row_controls(grid) if grid is not None else []
        if rows:
            selection_present = rows[0].GetPattern(auto.PatternId.SelectionItemPattern) is not None
    except Exception as ex:  # noqa: BLE001
        notes.append(f"chat-row probe failed: {ex}")
    try:
        button = auto.Control(searchFromControl=root, searchDepth=40,
                              ControlType=auto.ControlType.ButtonControl, RegexName=r"^Send$")
        invoke_present = (button.Exists(1, 0.2)
                          and button.GetPattern(auto.PatternId.InvokePattern) is not None)
    except Exception:  # noqa: BLE001
        pass
    if not invoke_present:
        notes.append("Send button absent (it only exists while the box has text) — "
                     "InvokePattern presence unknown until a send")

    return Capabilities(
        whatsapp_version=version,
        probed_at=datetime.now(timezone.utc).isoformat(),
        value_pattern_write=value_ok,
        legacy_set_value=legacy_ok,
        selection_item_present=selection_present,
        invoke_send_present=invoke_present,
        text_pattern_read=text_ok,
        notes=tuple(notes),
    )


class CapabilityStore:
    """Remembers the last probe so a restart doesn't re-measure, and notices a
    WhatsApp upgrade so it does."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._cached: Optional[Capabilities] = None

    def load(self) -> Optional[Capabilities]:
        if self._cached is not None:
            return self._cached
        try:
            if not self._path.is_file():
                return None
            data = json.loads(self._path.read_text(encoding="utf-8"))
            data["notes"] = tuple(data.get("notes", ()))
            self._cached = Capabilities(**data)
            return self._cached
        except Exception as ex:  # noqa: BLE001
            logger.debug("could not read cached capabilities: %s", ex)
            return None

    def save(self, capabilities: Capabilities) -> None:
        self._cached = capabilities
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(capabilities)
            payload["notes"] = list(capabilities.notes)
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as ex:  # noqa: BLE001
            logger.warning("could not cache capabilities: %s", ex)

    def refresh_if_needed(self, window_handle: int) -> Capabilities:
        """Probe when there is no cache, or when WhatsApp has been updated
        underneath it. **This is what makes a WhatsApp update change behaviour
        automatically** rather than waiting for someone to notice."""
        cached = self.load()
        current_version = whatsapp_version()
        if cached is not None and cached.whatsapp_version == current_version:
            return cached
        if cached is not None:
            logger.info("WhatsApp changed %s -> %s — re-probing automation capabilities",
                        cached.whatsapp_version, current_version)
        measured = probe(window_handle)
        self.save(measured)
        logger.info("capability probe: %s", measured.summary())
        return measured
