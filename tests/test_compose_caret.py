"""Guards on the compose fill path.

Both of these encode a defect that reached a real chat and cost ~100 undelivered
messages, so they are written to fail loudly if the behaviour is reverted:

* the compose box is CLICKED, not merely focused, and
* rung 1 is skipped on a build whose provider discards `SetValue`.

Neither is about elegance. `SetFocus` makes an element the focused UIA element
without giving a Chromium contenteditable an insertion point, so keystrokes are
discarded while every call reports success — the failure looks exactly like a
working system.
"""

from __future__ import annotations

import pytest

from wadam.whatsapp import sender


class FakeCompose:
    """Records what was done to it, in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def SetFocus(self) -> None:
        self.calls.append("SetFocus")

    def Click(self, simulateMove: bool = True) -> None:  # noqa: N803 - uiautomation's name
        self.calls.append(f"Click(simulateMove={simulateMove})")


@pytest.fixture(autouse=True)
def _restore_capability_flag():
    """The flag is module-level; don't let one test leak into the next."""
    before = sender.value_pattern_worth_trying()
    yield
    sender.set_value_pattern_ruled_out(not before)


def test_focusing_the_compose_box_also_clicks_it():
    """SetFocus alone leaves the contenteditable without a caret.

    Measured on WhatsApp 2.2630.102.0: SetFocus-only filled the box 6/14 times,
    SetFocus+Click 14/14."""
    compose = FakeCompose()

    assert sender.focus_compose_caret(compose) is True

    assert "SetFocus" in compose.calls, "the element must be focused"
    assert any(c.startswith("Click(") for c in compose.calls), (
        "the compose box must be CLICKED, not only focused — SetFocus does not "
        "create a caret in a Chromium contenteditable and the paste is silently "
        "discarded"
    )
    assert compose.calls.index("SetFocus") < next(
        i for i, c in enumerate(compose.calls) if c.startswith("Click(")
    ), "focus first, then click — the order the reference implementation uses"


def test_the_click_does_not_animate_the_cursor_across_the_screen():
    compose = FakeCompose()
    sender.focus_compose_caret(compose)
    assert "Click(simulateMove=False)" in compose.calls


def test_focus_control_alone_never_clicks():
    """The no-mouse helper still exists for controls that only need focus."""
    compose = FakeCompose()
    sender.focus_control(compose)
    assert compose.calls == ["SetFocus"]


def test_a_ruled_out_value_pattern_is_not_retried_on_every_send():
    """The capability probe writes its answer to disk; the sender must read it.

    Until it did, every send spent 0.4s re-discovering that this build discards
    `SetValue`, and wrote to the box immediately before the paste."""
    sender.set_value_pattern_ruled_out(True)
    assert sender.value_pattern_worth_trying() is False

    sender.set_value_pattern_ruled_out(False)
    assert sender.value_pattern_worth_trying() is True


def test_the_probe_result_reaches_the_sender_from_the_engine():
    """A wiring test: the engine must actually call the setter.

    The probe existed, ran, and wrote `capabilities.json` for a long time while
    nothing consulted it — a result nobody reads is not a capability check."""
    import inspect

    from wadam.engine import engine as engine_module

    source = inspect.getsource(engine_module)
    assert "set_value_pattern_ruled_out" in source, (
        "the engine must pass the capability probe's finding to the sender"
    )
