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


def test_a_group_panel_is_recognised_as_open(monkeypatch):
    """A community's panel is titled "Community info", not "Contact info".
    Measured live on Noteify. Recognising only the contact title meant an
    already-open group panel looked closed, so the probe invoked the toggle and
    closed it — the defect fixed for contacts, still live for groups."""
    for title in ("Contact info", "Group info", "Community info", "Channel info"):
        monkeypatch.setattr(S.auto, "ControlFromHandle",
                            lambda _h, t=title: _tree(t, "+91 79811 49423"))
        assert S._contact_panel_open(0) is True, f"{title} should count as open"


def test_the_panel_decides_whether_a_number_exists_not_the_is_group_flag():
    """`is_group` is inferred from whether a sidebar preview carries a speaker
    prefix, and it is wrong often enough to matter — measured, a 1:1 chat whose
    number was successfully read from a "Contact info" panel was flagged as a
    group. Skipping discovery on that flag would have refused a number the
    application had already proved it could read."""
    import inspect

    from wadam.engine.engine import AutomationEngine

    source = inspect.getsource(AutomationEngine.discover_phone_number)
    assert "if chat.is_group:" not in source, (
        "the decision must come from the panel, not from a guess about the chat"
    )


def test_a_groupish_panel_stops_the_probe_early(monkeypatch):
    """A community panel names itself. Waiting out the full timeout to learn
    there is no number would cost seconds on every group scan."""
    assert set(S.GROUPISH_PANEL_TITLES) <= set(S.CONTACT_PANEL_TITLES)
    for title in S.GROUPISH_PANEL_TITLES:
        monkeypatch.setattr(S.auto, "ControlFromHandle",
                            lambda _h, t=title: _tree(t, "Add members"))
        assert S._panel_title(0) == title


def test_a_contact_panel_is_not_treated_as_groupish(monkeypatch):
    monkeypatch.setattr(S.auto, "ControlFromHandle",
                        lambda _h: _tree("Contact info", "+91 79811 49423"))
    assert S._panel_title(0) == "Contact info"
    assert S._panel_title(0) not in S.GROUPISH_PANEL_TITLES


# ---------------------------------------------------------------------------
# "We looked and there was nothing" is worth remembering
# ---------------------------------------------------------------------------


class _Recorder:
    """A sender that counts how many times the panel was opened."""

    def __init__(self, answer=""):
        self.answer = answer
        self.probes = 0

    async def resolve_phone_number_async(self, _chat_name):
        self.probes += 1
        return self.answer


def _engine_with(monkeypatch, tmp_path, sender):
    import asyncio

    from wadam.config import Settings
    from wadam.engine.engine import AutomationEngine
    from wadam.storage.json_backup import JsonBackupStore
    from wadam.storage.repository import Repository
    from tests.test_storage import FakeMongo

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repo = Repository(settings, FakeMongo(), backup)
    repo.start()
    engine = AutomationEngine(settings, repo, lambda _s: None)
    engine._sender = sender
    return engine, repo


def _chat(repo, name="Noteify"):
    from wadam.domain.models import ChatConfig, chat_id_for

    chat = ChatConfig(chat_id=chat_id_for(name), chat_name=name, seeded=True)
    repo.save_chat(chat)
    return chat


def test_a_chat_with_no_number_is_probed_once_not_every_scan(monkeypatch, tmp_path):
    """A community has no number. Without a marker its panel was opened and
    closed on every scan — measured at ~9 seconds a time, visible on screen."""
    import asyncio

    sender = _Recorder(answer="")
    engine, repo = _engine_with(monkeypatch, tmp_path, sender)
    try:
        chat = _chat(repo)
        for _ in range(5):
            asyncio.run(engine.discover_phone_number(chat.chat_id))
        assert sender.probes == 1, "the panel must be opened once, not five times"
        assert repo.get_chat(chat.chat_id).phone_probed_at is not None
    finally:
        repo.stop()


def test_a_found_number_also_stops_further_probing(monkeypatch, tmp_path):
    import asyncio

    sender = _Recorder(answer="+91 79811 49423")
    engine, repo = _engine_with(monkeypatch, tmp_path, sender)
    try:
        chat = _chat(repo, "Varshith")
        assert asyncio.run(engine.discover_phone_number(chat.chat_id)) == "917981149423"
        asyncio.run(engine.discover_phone_number(chat.chat_id))
        assert sender.probes == 1
        assert repo.get_chat(chat.chat_id).phone_number == "917981149423"
    finally:
        repo.stop()


def test_clearing_the_number_re_arms_discovery(monkeypatch, tmp_path):
    """Emptying the field must not leave the chat permanently given up on."""
    import asyncio

    sender = _Recorder(answer="+91 79811 49423")
    engine, repo = _engine_with(monkeypatch, tmp_path, sender)
    try:
        chat = _chat(repo, "Varshith")
        asyncio.run(engine.discover_phone_number(chat.chat_id))
        assert sender.probes == 1

        asyncio.run(engine.set_chat_phone_number(chat.chat_id, ""))
        assert repo.get_chat(chat.chat_id).phone_probed_at is None

        asyncio.run(engine.discover_phone_number(chat.chat_id))
        assert sender.probes == 2, "clearing the number should allow another look"
    finally:
        repo.stop()


def test_the_marker_survives_a_restart(tmp_path):
    """Otherwise every restart re-probes every group."""
    from wadam.config import Settings
    from wadam.domain.models import ChatConfig, chat_id_for, utcnow
    from wadam.storage.json_backup import JsonBackupStore
    from wadam.storage.repository import Repository
    from tests.test_storage import FakeMongo

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repo = Repository(settings, FakeMongo(), backup)
    repo.start()
    chat = ChatConfig(chat_id=chat_id_for("Noteify"), chat_name="Noteify",
                      phone_probed_at=utcnow(), seeded=True)
    repo.save_chat(chat)
    repo.flush_json(force=True)

    restarted = Repository(settings, repo._mongo, JsonBackupStore(tmp_path, 0))
    restarted.start()
    try:
        assert restarted.get_chat(chat.chat_id).phone_probed_at is not None
    finally:
        restarted.stop()
        repo.stop()
