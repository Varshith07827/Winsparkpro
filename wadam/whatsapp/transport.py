"""The sending transport interface.

Everything above this line — the pipeline, the relay, the send API — asks for
"deliver this text to this chat and tell me whether it arrived". Nothing above
this line should know that the answer currently involves Windows UI Automation,
because that is an implementation detail of *one* transport and the least
durable part of the design.

    MessagePipeline ─┐
    RelayService ────┼──▶ Transport.send(chat, text) -> SendResult
    SendApiHost  ────┘         │
                               ├── UiAutomationTransport   (today)
                               ├── BusinessApiTransport    (docs/SENDING.md, Option D)
                               └── …whatever comes next

The interface is deliberately narrow. It does not expose "open a chat", "focus
the box" or "press send" — those are steps a *particular* transport happens to
have, and a Business Platform transport has none of them. What every transport
must do is deliver, verify, and report honestly what it cost.

`describe_capabilities()` exists so the application can tell the user what a
transport can and cannot do without hard-coding assumptions about it — the
UI Automation one answers "I need the foreground for a moment"; an API
transport would answer "I need nothing".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TransportCapabilities:
    """What using this transport costs the person at the keyboard."""

    name: str
    requires_foreground: bool
    moves_cursor: bool
    uses_clipboard: bool
    requires_interactive_desktop: bool
    requires_whatsapp_running: bool
    notes: str = ""

    def describe(self) -> str:
        costs = []
        if self.requires_foreground:
            costs.append("takes the foreground briefly")
        if self.moves_cursor:
            costs.append("may move the cursor")
        if self.uses_clipboard:
            costs.append("may use the clipboard")
        if self.requires_interactive_desktop:
            costs.append("needs an unlocked, connected session")
        return f"{self.name}: " + (", ".join(costs) if costs else "no user-visible effect")


@runtime_checkable
class Transport(Protocol):
    """What the pipeline needs from anything that can deliver a message."""

    async def send(self, chat_name: str, text: str):
        """Deliver `text` to `chat_name`.

        Must return a `SendResult`-shaped object whose `ok` is True **only when
        delivery was positively verified** — never when the attempt merely did
        not raise. Every caller in this application treats `ok` as proof, and
        records a message as sent on that basis alone.
        """
        ...

    def capabilities(self) -> TransportCapabilities:
        ...
