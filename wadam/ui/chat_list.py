"""The left rail — WhatsApp Desktop's chat list, rebuilt.

Profile strip with a live status indicator, search box, and the chat rows
themselves: avatar, name, last message, timestamp, unread badge, and the two
badges this application adds — AUTO (automation is on) and HOOK (a webhook is
configured, coloured by whether its last call succeeded).

The rail does not collapse. It has a minimum and maximum width, so dragging the
splitter resizes it within WhatsApp-like proportions and can never hide it.

The list is rebuilt from an engine snapshot every three seconds. Two things make
that cheap enough to do naively: rows are painted by a delegate rather than
built as widgets, and the rebuild is skipped when the visible data hasn't
changed — otherwise the selection, scroll position and search focus would
flicker under the user twice a minute for no reason.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,

    QVBoxLayout,
    QWidget,
)

from wadam.domain.models import ChatConfig
from wadam.ui import theme
from wadam.ui.widgets import CHAT_ROLE, ChatItemDelegate, checkbox_rect

MIN_WIDTH = 320
MAX_WIDTH = 560


def _sort_key(chat: ChatConfig):
    """Most recently active first, then by name.

    This used to approximate WhatsApp's own sidebar order — pinned first, then
    unread, then recency — reconstructed from what each scraped row carried.
    Pin state and unread counts came from the sidebar and OpenWA's
    `message.received` does not carry either, so the two leading terms would
    now be constants. Recency is what is left, and it is what actually matters
    in a list of chats being answered.
    """
    return (
        -(chat.updated_at.timestamp() if chat.updated_at else 0),
        chat.chat_name.lower(),
    )


class ChatListPanel(QWidget):
    chat_selected = Signal(str)          # chat_id
    automation_toggled = Signal(str, bool)   # chat_id, enabled

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatRail")
        self.setMinimumWidth(MIN_WIDTH)
        self.setMaximumWidth(MAX_WIDTH)
        self._chats: list[ChatConfig] = []
        self._render_signature: tuple = ()
        self._selected_id = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_search())

        self._list = QListWidget()
        self._list.setObjectName("chatList")
        self._list.setItemDelegate(ChatItemDelegate(self._list))
        self._list.setMouseTracking(True)
        self._list.setUniformItemSizes(True)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.currentItemChanged.connect(self._on_current_changed)
        # The checkbox is painted, not a real widget, so the click has to be
        # routed by geometry. `checkbox_rect` is shared with the painter so the
        # target and the picture cannot drift apart.
        self._list.viewport().installEventFilter(self)
        layout.addWidget(self._list, 1)

        # Keyboard: Ctrl+F (and Ctrl+K, the habit from every chat application)
        # focuses search; Down from the search box moves into the list, where
        # the arrow keys already navigate; Escape clears the filter.
        for sequence in ("Ctrl+F", "Ctrl+K"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self.focus_search)
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self._search)
        escape.activated.connect(self._clear_search)
        self.setTabOrder(self._search, self._list)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("railHeader")
        header.setFixedHeight(60)
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(12)

        self._rail_avatar = QLabel("WA")
        self._rail_avatar.setFixedSize(40, 40)
        self._rail_avatar.setAlignment(Qt.AlignCenter)
        row.addWidget(self._rail_avatar)

        text_column = QVBoxLayout()
        text_column.setSpacing(0)
        self._profile_name = QLabel("Automation Manager")
        self._profile_name.setObjectName("profileName")

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        # A coloured dot beside the counts: green when WhatsApp is connected,
        # red when it isn't. A status line you have to read is a status line
        # people stop reading.
        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(10)
        self._profile_meta = QLabel("starting…")
        self._profile_meta.setObjectName("profileMeta")
        meta_row.addWidget(self._status_dot)
        meta_row.addWidget(self._profile_meta, 1)

        text_column.addWidget(self._profile_name)
        text_column.addLayout(meta_row)
        row.addLayout(text_column, 1)

        # There was a manual refresh button here, for the moment somebody
        # wanted to be sure the list was current. It existed because the list
        # was a three-second scrape that could genuinely lag. The list is now
        # driven by webhook deliveries and repaints the instant one lands, so
        # the button had nothing left to do that waiting would not.
        # Styles the avatar only. NOT restyle() — that refreshes the list, which
        # does not exist yet at header-construction time.
        self._style_rail_avatar()
        return header

    def _build_search(self) -> QWidget:
        wrapper = QWidget()
        wrapper.setObjectName("chatRail")
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(12, 8, 12, 8)
        self._search = QLineEdit()
        self._search.setObjectName("searchField")
        self._search.setPlaceholderText("Search chats   (Ctrl+F)")
        self._search.setClearButtonEnabled(True)
        # Instant filtering — every keystroke re-filters, no refresh anywhere.
        self._search.textChanged.connect(lambda _t: self.refresh(force=True))
        row.addWidget(self._search)
        return wrapper

    # -- data --------------------------------------------------------------

    def set_chats(self, chats: list[ChatConfig], whatsapp_found: bool) -> None:
        self._chats = chats
        automated = sum(1 for c in chats if c.automation_enabled)
        connection = "WhatsApp connected" if whatsapp_found else "waiting for WhatsApp"
        self._profile_meta.setText(f"{len(chats)} chats · {automated} automated · {connection}")
        self._status_dot.setStyleSheet(
            f"color: {theme.ACCENT if whatsapp_found else theme.DANGER};"
        )
        self.refresh()

    def refresh(self, force: bool = False) -> None:
        visible = self._visible_chats()
        # Rebuilding an unchanged list would reset the scroll position and
        # flicker the selection every poll, which is worse than useless.
        signature = tuple(
            (c.chat_id, c.chat_name, c.last_message_preview,
             c.automation_enabled, bool(c.last_error))
            for c in visible
        )
        if not force and signature == self._render_signature:
            return
        self._render_signature = signature

        self._list.blockSignals(True)
        self._list.clear()
        for chat in visible:
            item = QListWidgetItem()
            item.setData(CHAT_ROLE, chat)
            item.setToolTip(self._tooltip_for(chat))
            self._list.addItem(item)
            if chat.chat_id == self._selected_id:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)

    @staticmethod
    def _has_error(chat: ChatConfig) -> bool:
        """Did the last thing this chat attempted fail?

        Was `_webhook_failing`, reading a per-chat webhook's last status. There
        is one webhook now and it belongs to the session, not the chat, so what
        is worth surfacing in the list is the chat's own last error — a send
        that did not go.
        """
        return bool(chat.last_error)

    def _visible_chats(self) -> list[ChatConfig]:
        query = self._search.text().strip().lower()
        chats = sorted(self._chats, key=_sort_key)
        if not query:
            return chats
        return [
            c for c in chats
            if query in c.chat_name.lower() or query in (c.last_message_preview or "").lower()
        ]

    @staticmethod
    def _tooltip_for(chat: ChatConfig) -> str:
        lines = [chat.chat_name]
        lines.append("Automation: " + ("ON" if chat.automation_enabled else "OFF"))
        lines.append(chat.chat_id)
        if chat.last_error:
            lines.append("Last error: " + chat.last_error)
        lines.append("Messages stored: " + str(chat.messages_stored))
        # A warning that unticking deletes the chat's records used to sit here,
        # said before the click rather than only in the dialog after it.
        # Unticking no longer deletes anything, so it would now be a lie.
        return "\n".join(lines)

    # -- selection ---------------------------------------------------------

    def select_chat(self, chat_id: str) -> None:
        self._selected_id = chat_id
        for index in range(self._list.count()):
            item = self._list.item(index)
            chat = item.data(CHAT_ROLE)
            if chat is not None and chat.chat_id == chat_id:
                self._list.setCurrentItem(item)
                return

    def focus_search(self) -> None:
        self._search.setFocus(Qt.ShortcutFocusReason)
        self._search.selectAll()

    def _clear_search(self) -> None:
        if self._search.text():
            self._search.clear()
        else:
            self._list.setFocus(Qt.ShortcutFocusReason)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        """Turn a click on the painted checkbox into a toggle.

        Clicking anywhere else selects the chat, which is the rule the
        specification asks for: selecting a chat must never switch its
        automation on or off by accident."""
        if watched is self._list.viewport() and event.type() == QEvent.MouseButtonRelease:
            if event.button() == Qt.LeftButton:
                point = event.position().toPoint()
                item = self._list.itemAt(point)
                if item is not None:
                    chat = item.data(CHAT_ROLE)
                    box = checkbox_rect(self._list.visualItemRect(item))
                    if chat is not None and box.contains(point):
                        self.automation_toggled.emit(
                            chat.chat_id, not chat.automation_enabled)
                        return True     # consumed: do NOT also select the chat
        return super().eventFilter(watched, event)

    def _on_current_changed(self, current: Optional[QListWidgetItem], _previous) -> None:
        if current is None:
            return
        chat = current.data(CHAT_ROLE)
        if chat is not None and chat.chat_id != self._selected_id:
            self._selected_id = chat.chat_id
            self.chat_selected.emit(chat.chat_id)

    @property
    def selected_id(self) -> str:
        return self._selected_id

    def _style_rail_avatar(self) -> None:
        self._rail_avatar.setStyleSheet(
            f"background: {theme.ACCENT}; color: {theme.ACCENT_TEXT};"
            f"border-radius: 20px; font-weight: 700;"
        )

    def restyle(self) -> None:
        """Re-apply inline styles after a light/dark change, then repaint the
        rows with the new palette."""
        self._style_rail_avatar()
        self._render_signature = ()
        self.refresh(force=True)
