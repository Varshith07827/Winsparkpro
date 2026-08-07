"""Custom painting for the chat list.

A `QStyledItemDelegate` rather than one widget per row: the sidebar routinely
holds several hundred chats, and building a widget tree for each of them costs
memory and layout time on every refresh, where painting costs neither. It also
gets the WhatsApp geometry exactly — 72px rows, a 49px avatar circle, name and
preview stacked, timestamp and badges right-aligned.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from wadam.ui import theme

CHAT_ROLE = Qt.UserRole + 1

ROW_HEIGHT = 72
AVATAR_SIZE = 49
PADDING_LEFT = 13
TEXT_LEFT = PADDING_LEFT + AVATAR_SIZE + 13
PADDING_RIGHT = 14


class ChatItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt naming
        return QSize(200, ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802 - Qt naming
        chat = index.data(CHAT_ROLE)
        if chat is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect: QRect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        background = (
            theme.SELECTED_BG if selected else theme.HOVER_BG if hovered else theme.PANEL_BG
        )
        painter.fillRect(rect, QColor(background))

        # Row separator, inset to start under the text like WhatsApp's.
        painter.setPen(QPen(QColor(theme.DIVIDER), 1))
        painter.drawLine(rect.left() + TEXT_LEFT, rect.bottom(), rect.right(), rect.bottom())

        self._paint_avatar(painter, rect, chat)

        right_edge = rect.right() - PADDING_RIGHT
        right_edge = self._paint_timestamp(painter, rect, chat, right_edge)
        badge_left = self._paint_badges(painter, rect, chat)

        self._paint_name(painter, rect, chat, right_edge)
        self._paint_preview(painter, rect, chat, badge_left)

        painter.restore()

    # -- pieces ------------------------------------------------------------

    def _paint_avatar(self, painter: QPainter, rect: QRect, chat) -> None:
        top = rect.top() + (ROW_HEIGHT - AVATAR_SIZE) // 2
        circle = QRect(rect.left() + PADDING_LEFT, top, AVATAR_SIZE, AVATAR_SIZE)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(theme.avatar_color(chat.chat_name))))
        painter.drawEllipse(circle)

        font = QFont(painter.font())
        font.setPointSizeF(13.0)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#e9edef")))
        painter.drawText(circle, Qt.AlignCenter, theme.initials(chat.chat_name))

    def _paint_timestamp(self, painter: QPainter, rect: QRect, chat, right_edge: int) -> int:
        text = chat.timestamp_text or ""
        if not text:
            return right_edge
        font = QFont(painter.font())
        font.setPointSizeF(8.0)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text)
        painter.setPen(QPen(QColor(theme.ACCENT if chat.unread_count else theme.TEXT_MUTED)))
        box = QRect(right_edge - width, rect.top() + 14, width, metrics.height())
        painter.drawText(box, Qt.AlignRight | Qt.AlignVCenter, text)
        return right_edge - width - 8

    def _paint_badges(self, painter: QPainter, rect: QRect, chat) -> int:
        """Unread count, then the automation and webhook badges, laid out from
        the right edge inwards. Returns the x the preview text must stop at."""
        x = rect.right() - PADDING_RIGHT
        y = rect.top() + 40

        if chat.unread_count:
            x = self._draw_pill(painter, x, y, str(chat.unread_count),
                                fill=theme.ACCENT, text_color=theme.ACCENT_TEXT, bold=True)
            x -= 6
        if chat.automation_enabled:
            x = self._draw_pill(painter, x, y, "AUTO",
                                fill=theme.ACCENT, text_color=theme.ACCENT_TEXT)
            x -= 5
        if (chat.webhook_url or "").strip():
            # The badge carries this chat's connection status: red when the last
            # webhook call failed, so a broken endpoint is visible in the list
            # rather than only after clicking through to the panel.
            status = (chat.last_webhook_status or "").lower()
            failing = bool(status) and any(
                word in status for word in ("fail", "timeout", "error")
            )
            colour = theme.DANGER if failing else theme.LINK
            x = self._draw_pill(painter, x, y, "HOOK",
                                fill="", text_color=colour, border=colour)
            x -= 5
        return x

    def _draw_pill(self, painter: QPainter, right_x: int, center_y: int, text: str,
                   fill: str = "", text_color: str = theme.TEXT, border: str = "",
                   bold: bool = False) -> int:
        font = QFont(painter.font())
        font.setPointSizeF(7.0)
        font.setWeight(QFont.Bold if bold else QFont.DemiBold)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        width = max(metrics.horizontalAdvance(text) + 12, 18)
        height = 17
        box = QRect(right_x - width, center_y - height // 2, width, height)

        painter.setPen(QPen(QColor(border)) if border else Qt.NoPen)
        painter.setBrush(QBrush(QColor(fill)) if fill else Qt.NoBrush)
        painter.drawRoundedRect(box, height / 2, height / 2)

        painter.setPen(QPen(QColor(text_color)))
        painter.drawText(box, Qt.AlignCenter, text)
        return box.left()

    def _paint_name(self, painter: QPainter, rect: QRect, chat, right_edge: int) -> None:
        font = QFont(painter.font())
        font.setPointSizeF(10.5)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        left = rect.left() + TEXT_LEFT
        width = max(20, right_edge - left)
        painter.setPen(QPen(QColor(theme.TEXT)))
        elided = metrics.elidedText(chat.chat_name or "(unnamed)", Qt.ElideRight, width)
        painter.drawText(QRect(left, rect.top() + 12, width, metrics.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, elided)

    def _paint_preview(self, painter: QPainter, rect: QRect, chat, right_edge: int) -> None:
        font = QFont(painter.font())
        font.setPointSizeF(9.0)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        left = rect.left() + TEXT_LEFT
        width = max(20, right_edge - left - 8)

        text = (chat.last_message_preview or "").replace("\n", " ").strip()
        if not text:
            text = "No messages read yet"
            painter.setPen(QPen(QColor(theme.TEXT_FAINT)))
        else:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        elided = metrics.elidedText(text, Qt.ElideRight, width)
        painter.drawText(QRect(left, rect.top() + 33, width, metrics.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, elided)
