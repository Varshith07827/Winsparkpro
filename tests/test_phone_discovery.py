"""Reading a saved contact's number from the contact-info panel.

This capability was documented as impossible. It was not — the probes that
"proved" it scanned an accessibility tree that had not rendered yet. The
correction is in docs/LIMITATIONS.md; these tests pin the behaviour and, more
importantly, the two ways the probe damaged the UI while being written.
"""

from __future__ import annotations

import pytest

from wadam.whatsapp import sender as S


class _Node:
    def __init__(self, kind, name="", children=()):
        self.ControlTypeName = kind
        self.Name = name
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


def _tree(*names):
    return _Node("PaneControl", "", [_Node("TextControl", n) for n in names])


# ---------------------------------------------------------------------------
# Recognising a number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "+91 79811 49423", "917981149423", "+91-79811-49423", "+1 (555) 123-4567",
])
def test_a_phone_number_is_recognised(text):
    assert S._PHONE_TEXT_RE.fullmatch(text)


@pytest.mark.parametrize("text", [
    "Varshith", "9:21 pm", "Contact info", "Online", "", "3",
    "WINSPARK_E2E_TEST_84721",
])
def test_things_that_are_not_phone_numbers(text):
    assert not S._PHONE_TEXT_RE.fullmatch(text)


def test_a_number_inside_a_sentence_is_not_matched():
    """fullmatch, not search: a message mentioning a number is not the
    contact's number."""
    assert not S._PHONE_TEXT_RE.fullmatch("call me on +91 79811 49423 later")


# ---------------------------------------------------------------------------
# The panel is a TOGGLE, and the probe must leave the UI as it found it
# ---------------------------------------------------------------------------


def test_an_already_open_panel_is_recognised(monkeypatch):
    """Invoking the button again would CLOSE it, and the number would go with
    it. Measured live: the first probe returned nothing for exactly this
    reason, and a second one from a closed start returned the number."""
    monkeypatch.setattr(S.auto, "ControlFromHandle",
                        lambda _h: _tree("Contact info", "+91 79811 49423"))
    assert S._contact_panel_open(0) is True


def test_a_closed_panel_is_recognised(monkeypatch):
    monkeypatch.setattr(S.auto, "ControlFromHandle",
                        lambda _h: _tree("Chats", "Varshith"))
    assert S._contact_panel_open(0) is False


def test_the_probe_never_presses_escape_blindly():
    """The fix for the toggle was briefly an unconditional Escape "to
    normalise". With no panel open, Escape closes the CONVERSATION — the next
    step then found no chat and no button at all. The source must not contain
    an Escape that runs before the open-state check."""
    import inspect

    source = inspect.getsource(S.read_contact_number_sync)
    before_check = source.split("_contact_panel_open")[0]
    assert "{Esc}" not in before_check, (
        "Escape before knowing the panel state closes the user's conversation"
    )


def test_escape_is_only_sent_when_the_probe_opened_the_panel():
    import inspect

    source = inspect.getsource(S.read_contact_number_sync)
    assert "if opened_here:" in source
    assert source.index("if opened_here:") < source.index('"{Esc}"')


# ---------------------------------------------------------------------------
# Finding the number
# ---------------------------------------------------------------------------


def test_the_number_is_found_in_the_panel(monkeypatch):
    monkeypatch.setattr(S.auto, "ControlFromHandle",
                        lambda _h: _tree("Contact info", "Varshith",
                                         "+91 79811 49423", "Block"))
    assert S._scan_for_phone(0) == "+91 79811 49423"


def test_no_number_present_returns_empty(monkeypatch):
    monkeypatch.setattr(S.auto, "ControlFromHandle",
                        lambda _h: _tree("Contact info", "Varshith", "Block"))
    assert S._scan_for_phone(0) == ""


def test_a_missing_button_is_not_an_error(monkeypatch):
    """A chat with no header button must return "" rather than raise — a
    lookup failure can never be allowed to break a scan."""
    monkeypatch.setattr(S, "_find_profile_button", lambda _h: None)
    assert S.read_contact_number_sync(0) == ""
