"""UI behaviour, driven headlessly through Qt's offscreen platform.

These are not screenshot tests. They assert the things that were wrong or could
silently go wrong: that ticking a chat toggles it and clicking one does not,
that the badge counts work rather than WhatsApp's unread messages, that search
filters without a refresh, and that a rebuild every three seconds does not
destroy the user's selection.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from wadam.domain.models import ChatConfig, chat_id_for  # noqa: E402
from wadam.ui import theme  # noqa: E402
from wadam.ui.chat_list import ChatListPanel  # noqa: E402
from wadam.domain.webhook_url import validate_webhook_url  # noqa: E402
from wadam.ui.chat_details import ChatDetailsPanel  # noqa: E402
from wadam.ui.widgets import CHAT_ROLE  # noqa: E402


@pytest.fixture(scope="session")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.apply("dark"))
    return instance


def chat(name: str, webhook: str = "", automation: bool = False, **kwargs) -> ChatConfig:
    return ChatConfig(chat_id=chat_id_for(name), chat_name=name, webhook_url=webhook,
                      automation_enabled=automation, **kwargs)


def rebuild(panel: ChatListPanel, chats, found: bool = True) -> None:
    panel.set_chats(list(chats), found)
    panel.refresh(force=True)


# ---------------------------------------------------------------------------
# The webhook field
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Validation and feedback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "", "https://example.com/hook", "http://127.0.0.1:8000/x?a=1",
])
def test_valid_urls_are_accepted(url):
    ok, problem = validate_webhook_url(url)
    assert ok, problem


@pytest.mark.parametrize("url,fragment", [
    ("example.com/hook", "http://"),      # the commonest typo
    ("htp://example.com", "http://"),
    ("ftp://example.com/x", "http://"),
    ("https://", "no host"),
    ("https://exa mple.com/x", "space"),
])
def test_invalid_urls_are_rejected_with_a_reason(url, fragment):
    ok, problem = validate_webhook_url(url)
    assert not ok
    assert fragment in problem





# ---------------------------------------------------------------------------
# The chat list
# ---------------------------------------------------------------------------


def make_list(app, chats) -> ChatListPanel:
    panel = ChatListPanel()
    panel.set_chats(chats, whatsapp_found=True)
    return panel


def visible_names(panel: ChatListPanel) -> list[str]:
    return [panel._list.item(i).data(CHAT_ROLE).chat_name for i in range(panel._list.count())]


def test_search_filters_instantly_with_no_refresh(app):
    panel = make_list(app, [chat("Alice"), chat("Bob"), chat("Alicia"), chat("Team chat")])

    panel._search.setText("ali")           # textChanged fires the filter itself
    # Compared as a set: the list is ordered by recency, not alphabetically.
    assert set(visible_names(panel)) == {"Alice", "Alicia"}

    panel._search.setText("")
    assert len(visible_names(panel)) == 4


def test_search_also_matches_the_message_preview(app):
    panel = make_list(app, [
        chat("Alice", last_message_preview="see you at six"),
        chat("Bob", last_message_preview="thanks"),
    ])
    panel._search.setText("six")
    assert visible_names(panel) == ["Alice"]


def test_pinned_and_unread_chats_sort_first(app):
    panel = make_list(app, [
        chat("Zoe"),
        chat("Pinned one", is_pinned=True),
        chat("Unread one", unread_count=4),
    ])
    assert visible_names(panel)[:2] == ["Pinned one", "Unread one"]


def test_an_unchanged_snapshot_does_not_rebuild_the_list(app):
    """Rebuilding every three seconds would reset the scroll position and
    flicker the selection under the user."""
    chats = [chat("Alice"), chat("Bob")]
    panel = make_list(app, chats)
    panel.select_chat(chats[0].chat_id)
    first_item = panel._list.item(0)

    panel.set_chats(chats, whatsapp_found=True)
    assert panel._list.item(0) is first_item, "the list was rebuilt for no reason"

    chats[1].unread_count = 2
    panel.set_chats(chats, whatsapp_found=True)
    assert panel._list.item(0) is not first_item, "a real change must redraw"


def test_selection_survives_a_rebuild(app):
    chats = [chat("Alice"), chat("Bob")]
    panel = make_list(app, chats)
    panel.select_chat(chats[1].chat_id)

    chats[0].last_message_preview = "something new"
    panel.set_chats(chats, whatsapp_found=True)

    current = panel._list.currentItem()
    assert current is not None
    assert current.data(CHAT_ROLE).chat_id == chats[1].chat_id


def test_selecting_a_row_announces_the_chat_id(app):
    chats = [chat("Alice"), chat("Bob")]
    panel = make_list(app, chats)
    announced: list[str] = []
    panel.chat_selected.connect(announced.append)

    # Against the row's own data rather than the input order — the list sorts
    # by recency, so row 1 is not necessarily chats[1].
    expected = panel._list.item(1).data(CHAT_ROLE).chat_id
    panel._list.setCurrentRow(1)
    assert announced == [expected]


def test_the_rail_cannot_be_collapsed(app):
    panel = ChatListPanel()
    assert panel.minimumWidth() >= 320
    assert panel.maximumWidth() <= 560


def test_the_status_dot_reflects_the_connection(app):
    panel = make_list(app, [chat("Alice")])
    assert theme.ACCENT in panel._status_dot.styleSheet()
    assert "WhatsApp connected" in panel._profile_meta.text()

    panel.set_chats([chat("Alice")], whatsapp_found=False)
    assert theme.DANGER in panel._status_dot.styleSheet()
    assert "waiting for WhatsApp" in panel._profile_meta.text()


def test_ctrl_f_focuses_search(app):
    panel = make_list(app, [chat("Alice")])
    panel.focus_search()
    assert panel._search.hasFocus() or panel.focusWidget() is panel._search


# ---------------------------------------------------------------------------
# Theming
# ---------------------------------------------------------------------------


def test_both_themes_produce_a_complete_stylesheet():
    for scheme in ("dark", "light"):
        sheet = theme.apply(scheme)
        assert theme.current_scheme == scheme
        assert "{{" not in sheet and "}}" not in sheet, "an unformatted brace escaped"
        # Every colour placeholder resolved to a real value.
        assert "None" not in sheet
        assert theme.TEXT and theme.ACCENT and theme.PANEL_BG
    theme.apply("dark")


def test_light_and_dark_actually_differ():
    dark = theme.apply("dark")
    dark_text = theme.TEXT
    light = theme.apply("light")
    assert theme.TEXT != dark_text
    assert dark != light
    theme.apply("dark")


def test_a_chats_avatar_colour_is_stable():
    # The same contact keeps the same colour between runs and across themes,
    # because it is derived from the name rather than from paint order.
    first = theme.avatar_color("Aarav Sharma")
    theme.apply("light")
    assert theme.avatar_color("Aarav Sharma") == first
    theme.apply("dark")
    assert theme.initials("Aarav Sharma") == "AS"
    assert theme.initials("Papa") == "PA"
    assert theme.initials("") == "#"


# ---------------------------------------------------------------------------
# The checkbox, the badge, and the details panel
# ---------------------------------------------------------------------------


def _click_at(panel: ChatListPanel, point) -> None:
    """Deliver a real mouse release to the list viewport."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QMouseEvent

    event = QMouseEvent(QEvent.MouseButtonRelease, point, Qt.LeftButton,
                        Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(panel._list.viewport(), event)


def test_ticking_the_checkbox_toggles_automation(app):
    """The one control in the product."""
    from wadam.ui.widgets import checkbox_rect

    panel = ChatListPanel()
    rebuild(panel, [chat("Alice")])
    seen = []
    panel.automation_toggled.connect(lambda cid, on: seen.append((cid, on)))

    item = panel._list.item(0)
    _click_at(panel, checkbox_rect(panel._list.visualItemRect(item)).center())

    assert seen == [(chat_id_for("Alice"), True)]


def test_ticking_an_enabled_chat_turns_it_off(app):
    from wadam.ui.widgets import checkbox_rect

    panel = ChatListPanel()
    rebuild(panel, [chat("Alice", automation=True)])
    seen = []
    panel.automation_toggled.connect(lambda cid, on: seen.append((cid, on)))

    item = panel._list.item(0)
    _click_at(panel, checkbox_rect(panel._list.visualItemRect(item)).center())

    assert seen == [(chat_id_for("Alice"), False)]


def test_clicking_the_row_selects_without_toggling(app):
    """Selecting a chat must never switch its automation by accident."""
    panel = ChatListPanel()
    rebuild(panel, [chat("Alice")])
    toggles = []
    panel.automation_toggled.connect(lambda cid, on: toggles.append((cid, on)))

    row = panel._list.visualItemRect(panel._list.item(0))
    _click_at(panel, row.center())        # middle of the row, far from the box

    assert toggles == [], "clicking the row must not toggle automation"


def test_the_refresh_button_asks_for_a_refresh(app):
    panel = ChatListPanel()
    asked = []
    panel.refresh_requested.connect(lambda: asked.append(True))
    panel._refresh.click()
    assert asked == [True]


def test_the_badge_counts_pending_work_not_unread_messages(app):
    """WhatsApp's own unread count is not shown — the user can see that in
    WhatsApp. The badge means "arrived and not yet through the round trip"."""
    from wadam.ui.widgets import CHAT_ROLE

    panel = ChatListPanel()
    waiting = chat("Alice", automation=True)
    waiting.unread_count = 9        # WhatsApp says nine
    waiting.pending_count = 2       # two of them are still mid-flight
    rebuild(panel, [waiting])

    rendered = panel._list.item(0).data(CHAT_ROLE)
    assert rendered.pending_count == 2
    assert rendered.unread_count == 9, "the field is kept, it is simply not drawn"


def test_the_details_panel_shows_the_generated_webhook(app):
    panel = ChatDetailsPanel()
    panel.set_chat(chat("Alice", webhook="https://noteify.org/ntext/whook/?15551234567"))
    assert panel._title.text() == "Alice"
    assert panel._webhook.text() == "https://noteify.org/ntext/whook/?15551234567"


def test_a_chat_with_no_number_is_addressed_by_name(app):
    """Every chat gets a usable webhook the moment it is discovered."""
    panel = ChatDetailsPanel()
    panel.set_chat(chat("Alice", webhook="https://noteify.org/ntext/whook/?Alice"))
    assert panel._webhook.text() == "https://noteify.org/ntext/whook/?Alice"
    # One explanation, under the field that would improve it.
    assert "number" in panel._phone_hint.text().lower()


def test_the_details_panel_is_empty_until_a_chat_is_picked(app):
    panel = ChatDetailsPanel()
    panel.set_chat(None)
    assert panel.current_chat_id() == ""


def test_every_chat_paints_whether_or_not_it_is_ticked(app):
    """An unchecked row once rendered completely blank.

    `_paint_checkbox` referenced a colour that did not exist, the exception
    escaped `paint()`, and Qt abandoned the row — so exactly the chats with
    automation OFF vanished from the list while the count still said 5."""
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem
    from wadam.ui.widgets import ChatItemDelegate, ROW_HEIGHT

    panel = ChatListPanel()
    rebuild(panel, [chat("On", automation=True), chat("Off", automation=False)])
    assert panel._list.count() == 2

    delegate = ChatItemDelegate(panel._list)
    image = QImage(320, ROW_HEIGHT, QImage.Format_ARGB32)
    for row in range(panel._list.count()):
        painter = QPainter(image)
        option = QStyleOptionViewItem()
        option.rect = panel._list.visualItemRect(panel._list.item(row))
        # Must not raise: a raising paint() is what produced the blank row.
        delegate.paint(painter, option, panel._list.model().index(row, 0))
        painter.end()


def test_typing_a_number_saves_it_as_digits(app):
    panel = ChatDetailsPanel()
    panel.set_chat(chat("Alice"))
    saved = []
    panel.phone_saved.connect(lambda cid, num: saved.append((cid, num)))

    panel._phone.setText("+91 94231 55555")
    panel._save_phone()

    assert saved == [(chat_id_for("Alice"), "919423155555")]
    assert panel._phone.text() == "919423155555"


def test_the_once_a_second_refresh_does_not_eat_a_half_typed_number(app):
    """The panel re-renders every second. Overwriting the field on each pass
    made it impossible to type a number at all."""
    panel = ChatDetailsPanel()
    alice = chat("Alice")
    panel.set_chat(alice)
    panel._phone.setFocus()
    panel._phone.setText("9142")          # mid-typing

    panel.set_chat(alice)                 # the tick fires

    assert panel._phone.text() == "9142"


def test_clearing_the_number_clears_the_webhook(app):
    panel = ChatDetailsPanel()
    saved = []
    panel.set_chat(chat("Alice", webhook="https://n.test/?91", phone_number="91"))
    panel.phone_saved.connect(lambda cid, num: saved.append((cid, num)))

    panel._phone.setText("")
    panel._save_phone()

    assert saved == [(chat_id_for("Alice"), "")]


def test_the_webhook_box_is_prefilled_and_editable(app):
    panel = ChatDetailsPanel()
    panel.set_chat(chat("Alice", webhook="https://noteify.org/ntext/whook/?Alice"))
    assert panel._webhook.text() == "https://noteify.org/ntext/whook/?Alice"
    assert panel._webhook.isReadOnly() is False

    saved = []
    panel.webhook_saved.connect(lambda cid, url: saved.append((cid, url)))
    panel._webhook.setText("https://elsewhere.test/hook")
    panel._save_webhook()

    assert saved == [(chat_id_for("Alice"), "https://elsewhere.test/hook")]


def test_an_invalid_webhook_is_not_saved_and_is_not_thrown_away(app):
    """Reverting the box would mean retyping a long URL because of one typo."""
    panel = ChatDetailsPanel()
    panel.set_chat(chat("Alice", webhook="https://n.test/?Alice"))
    saved = []
    panel.webhook_saved.connect(lambda cid, url: saved.append((cid, url)))

    panel._webhook.setText("htp://broken")
    panel._save_webhook()

    assert saved == [], "an invalid URL must not reach the engine"
    assert panel._webhook.text() == "htp://broken", "what was typed stays put"
    assert "http" in panel._webhook_hint.text()


def test_clearing_the_webhook_returns_the_chat_to_the_default(app):
    panel = ChatDetailsPanel()
    panel.set_chat(chat("Alice", webhook="https://elsewhere.test/hook"))
    saved = []
    panel.webhook_saved.connect(lambda cid, url: saved.append((cid, url)))

    panel._webhook.setText("")
    panel._save_webhook()

    assert saved == [(chat_id_for("Alice"), "")]


def test_the_webhook_box_survives_the_once_a_second_refresh(app):
    panel = ChatDetailsPanel()
    alice = chat("Alice", webhook="https://n.test/?Alice")
    panel.set_chat(alice)
    panel._webhook.setText("https://half-typed")

    panel.set_chat(alice)          # the tick fires

    assert panel._webhook.text() == "https://half-typed"


def test_closing_the_window_shuts_everything_down(app, tmp_path):
    """closeEvent referenced a timer that had been deleted, so it raised on
    every close and skipped the engine and repository shutdown beneath it.
    Qt swallows the exception into a traceback, so nothing failed loudly."""
    from wadam.config import Settings
    from wadam.storage.json_backup import JsonBackupStore
    from wadam.storage.repository import Repository
    from wadam.ui.engine_host import EngineHost
    from wadam.ui.main_window import MainWindow
    from tests.test_storage import FakeMongo

    settings = Settings(mongodb_uri="mongodb://localhost:27017",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    host = EngineHost(settings, repository)          # not started: no threads
    window = MainWindow(settings, repository, host)

    class _Event:
        def __init__(self): self.accepted = False
        def accept(self): self.accepted = True

    event = _Event()
    window.closeEvent(event)                          # must not raise

    assert event.accepted, "the close must be accepted, not abandoned mid-way"
