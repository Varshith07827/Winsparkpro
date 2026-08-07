"""The WhatsApp Desktop look, in both themes.

Colours, spacing and proportions are taken from WhatsApp Desktop so the window
reads as the same application: a chat rail on the left with a header strip above
it, a distinct conversation area on the right, teal accents, 49px avatar
circles. The right panel is where the resemblance stops on purpose — instead of
a conversation it holds that chat's automation configuration.

**How the theme is applied.** `apply(scheme)` rebinds this module's colour
globals and returns a stylesheet. Widgets read `theme.ACCENT` and friends at
paint time, so a theme change is picked up by everything that paints, with no
call-site changes anywhere. The handful of places that set an inline stylesheet
once (avatar circles) expose a `restyle()` for the same reason.

The scheme follows the operating system by default and switches live when the
system does.
"""

from __future__ import annotations

from typing import Literal

Scheme = Literal["dark", "light"]

_DARK = {
    "PANEL_BG": "#111b21",         # left rail body
    "HEADER_BG": "#202c33",        # header strips, search field
    "CONVERSATION_BG": "#0b141a",  # right panel body
    "CARD_BG": "#182229",          # raised cards inside the right panel
    "HOVER_BG": "#202c33",
    "SELECTED_BG": "#2a3942",
    "DIVIDER": "#222d34",
    "BORDER": "#2a3942",
    "TEXT": "#e9edef",
    "TEXT_MUTED": "#8696a0",
    "TEXT_FAINT": "#667781",
    "ACCENT": "#00a884",           # WhatsApp teal
    "ACCENT_HOVER": "#06cf9c",
    "ACCENT_TEXT": "#111b21",
    "LINK": "#53bdeb",
    "DANGER": "#f15c6d",
    "DANGER_BG": "#2a2126",
    "WARNING": "#ffb02e",
}

_LIGHT = {
    "PANEL_BG": "#ffffff",
    "HEADER_BG": "#f0f2f5",
    "CONVERSATION_BG": "#f7f8fa",
    "CARD_BG": "#ffffff",
    "HOVER_BG": "#f5f6f6",
    "SELECTED_BG": "#f0f2f5",
    "DIVIDER": "#e9edef",
    "BORDER": "#d1d7db",
    "TEXT": "#111b21",
    "TEXT_MUTED": "#667781",
    "TEXT_FAINT": "#8696a0",
    "ACCENT": "#008069",           # WhatsApp's light-theme teal is a shade deeper,
    "ACCENT_HOVER": "#017561",     # for contrast against white
    "ACCENT_TEXT": "#ffffff",
    "LINK": "#027eb5",
    "DANGER": "#c8102e",
    "DANGER_BG": "#fdf0f2",
    "WARNING": "#946200",
}

# Avatar circle colours, assigned per chat by a hash of the name so the same
# contact keeps the same colour between runs. Deliberately identical in both
# themes — they carry white initials and read correctly either way, and a
# contact whose colour changed with the theme would be harder to recognise.
AVATAR_COLORS = (
    "#6b7c85", "#c4785c", "#5b8c85", "#8b6f9e", "#4a7c9e",
    "#9e7c4a", "#5c8c5c", "#8c5c6f", "#7c6b9e", "#4a8c8c",
)

# Current scheme's colours, bound at import and rebound by apply().
PANEL_BG = HEADER_BG = CONVERSATION_BG = CARD_BG = HOVER_BG = SELECTED_BG = ""
DIVIDER = BORDER = TEXT = TEXT_MUTED = TEXT_FAINT = ""
ACCENT = ACCENT_HOVER = ACCENT_TEXT = LINK = DANGER = DANGER_BG = WARNING = ""

current_scheme: Scheme = "dark"


def avatar_color(name: str) -> str:
    total = sum(ord(c) for c in (name or "?"))
    return AVATAR_COLORS[total % len(AVATAR_COLORS)]


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace("+", " ").split() if p[:1].isalnum()]
    if not parts:
        return "#"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def apply(scheme: Scheme = "dark") -> str:
    """Bind the palette for `scheme` and return the matching stylesheet."""
    global current_scheme
    palette = _LIGHT if scheme == "light" else _DARK
    current_scheme = "light" if scheme == "light" else "dark"
    globals().update(palette)
    return stylesheet()


def detect_scheme(app=None) -> Scheme:
    """The operating system's preference, or dark when it can't be determined.

    `QStyleHints.colorScheme()` exists from Qt 6.5; on anything older this
    falls back rather than refusing to start, because a theme is not worth a
    crash."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        hints = (app or QApplication.instance()).styleHints()
        return "light" if hints.colorScheme() == Qt.ColorScheme.Light else "dark"
    except Exception:  # noqa: BLE001 - older Qt, or no application yet
        return "dark"


def stylesheet() -> str:
    return f"""
QWidget {{
    background: {CONVERSATION_BG};
    color: {TEXT};
    font-family: "Segoe UI", "Helvetica Neue", sans-serif;
    font-size: 10pt;
}}

QMainWindow, QDialog {{ background: {CONVERSATION_BG}; }}

/* ---- left rail ---- */
#chatRail {{ background: {PANEL_BG}; border-right: 1px solid {DIVIDER}; }}
#railHeader, #panelHeader {{ background: {HEADER_BG}; }}
#railTitle {{ font-size: 12pt; font-weight: 600; color: {TEXT}; }}
#profileName {{ font-size: 11pt; font-weight: 600; color: {TEXT}; }}
#profileMeta {{ color: {TEXT_MUTED}; font-size: 9pt; }}

#searchField {{
    background: {HEADER_BG};
    border: 1px solid {DIVIDER};
    border-radius: 8px;
    padding: 7px 12px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
}}
#searchField:focus {{ border-color: {ACCENT}; }}

QListWidget#chatList {{
    background: {PANEL_BG};
    border: none;
    outline: none;
}}
QListWidget#chatList::item {{ border: none; padding: 0px; }}

/* ---- right panel ---- */
#configPanel {{ background: {CONVERSATION_BG}; }}
#configTitle {{ font-size: 13pt; font-weight: 600; color: {TEXT}; }}
#configSubtitle {{ color: {TEXT_MUTED}; font-size: 9pt; }}
#sectionTitle {{
    color: {TEXT_MUTED};
    font-size: 9pt;
    font-weight: 600;
    letter-spacing: 1px;
}}

QFrame#card {{
    background: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QLabel {{ background: transparent; }}
#fieldLabel {{ color: {TEXT_MUTED}; font-size: 9pt; }}
#fieldValue {{ color: {TEXT}; }}
#fieldValueMuted {{ color: {TEXT_FAINT}; }}
#fieldMono {{ color: {TEXT_MUTED}; font-family: "Consolas", "Courier New", monospace; }}
#statusOk {{ color: {ACCENT}; font-weight: 600; }}
#statusBad {{ color: {DANGER}; font-weight: 600; }}
#statusWarn {{ color: {WARNING}; font-weight: 600; }}

QLineEdit, QPlainTextEdit {{
    background: {HEADER_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
}}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {{
    background: {CONVERSATION_BG};
    color: {TEXT_MUTED};
}}

QPushButton {{
    background: {HEADER_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: {SELECTED_BG}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {CONVERSATION_BG}; }}

QPushButton#primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: {ACCENT_TEXT};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: {BORDER}; border-color: {BORDER}; color: {TEXT_FAINT}; }}

QPushButton#danger {{ color: {DANGER}; }}
QPushButton#danger:hover {{ background: {DANGER_BG}; }}

/* The global ON/OFF switch. Checked = automation on everywhere. */
QPushButton#globalToggle {{
    background: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 13px;
    padding: 5px 16px;
    color: {TEXT_MUTED};
    font-weight: 700;
    font-size: 9pt;
}}
QPushButton#globalToggle:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: {ACCENT_TEXT};
}}

QCheckBox {{ color: {TEXT}; spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {HEADER_BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
/* The rail's scrollbar groove would otherwise inherit the window background,
   drawing a strip in a different colour down the edge of the chat list. */
#chatRail QScrollBar:vertical {{ background: {PANEL_BG}; }}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollArea {{ border: none; background: transparent; }}

QSplitter::handle {{ background: {DIVIDER}; width: 1px; }}

#statusBar {{ background: {HEADER_BG}; color: {TEXT_MUTED}; }}
#statusBar QLabel {{ color: {TEXT_MUTED}; font-size: 9pt; }}

QToolTip {{
    background: {HEADER_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 6px;
}}
"""


# Bind the default so importing the module is enough to paint with.
apply("dark")
