"""Row-parser tests against real WhatsApp Desktop row strings.

These strings were captured from a live window rather than invented — the
parser is a heuristic over an undelimited string, so tests written from
imagination would only prove the heuristic agrees with itself.
"""

from wadam.whatsapp.row_parser import parse_chat_row


def test_unread_prefix_and_name_split():
    parsed = parse_chat_row(
        "4 unread messages Vishnu Cr Gvp Yesterday ekada grp names navi unaye"
    )
    assert parsed["unread_count"] == 4
    assert parsed["chat_name"] == "Vishnu Cr Gvp"
    assert parsed["timestamp_text"] == "Yesterday"
    assert parsed["last_message"] == "ekada grp names navi unaye"


def test_pinned_flag_is_stripped_from_the_message():
    parsed = parse_chat_row(
        "CSE - C Yesterday Chaitu: https://chat.whatsapp.com/abc Pinned chat"
    )
    assert parsed["is_pinned"] is True
    assert parsed["chat_name"] == "CSE - C"
    assert parsed["last_message"].endswith("/abc")
    # A speaker prefix in the preview is the only group hint a row carries.
    assert parsed["looks_like_group"] is True


def test_view_status_prefix_is_not_part_of_the_name():
    parsed = parse_chat_row("View status 2 unread messages Hasini 10:42 am hey")
    assert parsed["unread_count"] == 2
    assert parsed["chat_name"] == "Hasini"
    assert parsed["timestamp_text"] == "10:42 am"


def test_multiple_trailing_flags():
    parsed = parse_chat_row("Family Today ok Muted chat Pinned chat")
    assert parsed["is_muted"] is True
    assert parsed["is_pinned"] is True
    assert parsed["chat_name"] == "Family"
    assert parsed["last_message"] == "ok"


def test_row_with_no_anchor_is_all_name():
    parsed = parse_chat_row("Archived")
    assert parsed["chat_name"] == "Archived"
    assert parsed["timestamp_text"] == ""
    assert parsed["last_message"] == ""


def test_own_message_preview_is_not_a_group_hint():
    parsed = parse_chat_row("Alice 09:15 am You: on my way")
    assert parsed["looks_like_group"] is False


# ---------------------------------------------------------------------------
# Sender name / role badge inside a bubble
# ---------------------------------------------------------------------------


class _FakePart:
    """The shape `_iter_message_parts` yields for a plain text control."""

    def __init__(self, name: str) -> None:
        self.Name = name


class _FakeChild:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.ControlTypeName = "TextControl"

    def GetChildren(self):
        return []


class _FakeRow:
    def __init__(self, names) -> None:
        self._children = [_FakeChild(n) for n in names]

    def GetChildren(self):
        return list(self._children)


class _FakeLabel:
    def __init__(self, row) -> None:
        self._row = row

    def GetParentControl(self):
        return self._row


def _bubble(names, sender_label="You:"):
    from wadam.whatsapp.reader import _extract_bubble_text

    return _extract_bubble_text(_FakeLabel(_FakeRow(names)), sender_label)


def test_a_role_badge_is_not_part_of_the_message():
    """Measured live in a Community chat: a delivered message read back as
    "You Community admin Resolver check by name", so the census never matched
    it and every send to that chat was marked UNVERIFIED despite arriving."""
    assert _bubble(["You", "Community admin", "Resolver check by name"]) == \
        "Resolver check by name"


def test_a_bare_sender_name_is_dropped_too():
    assert _bubble(["You", "Hello there"]) == "Hello there"


def test_an_ordinary_bubble_is_untouched():
    assert _bubble(["Hello there"]) == "Hello there"


def test_a_message_that_is_only_a_badge_word_survives():
    """Stripping is from the FRONT and never takes the last part, so a message
    whose entire text is "Admin" is still readable."""
    assert _bubble(["Admin"]) == "Admin"
    assert _bubble(["You", "Admin"]) == "Admin"


def test_the_group_senders_own_name_is_dropped():
    assert _bubble(["Manohar", "Sure sir"], sender_label="Manohar:") == "Sure sir"


# ---------------------------------------------------------------------------
# Direction detection
# ---------------------------------------------------------------------------


def test_the_details_button_names_the_author():
    from wadam.whatsapp.reader import author_from_details_button

    assert author_from_details_button("Open chat details for You") == "You"
    assert author_from_details_button("Open chat details for Nagen US") == "Nagen US"
    assert author_from_details_button("9:21 pm Delivered") == ""
    assert author_from_details_button("") == ""


class _Ctrl:
    def __init__(self, kind, name, children=()):
        self.ControlTypeName = kind
        self.Name = name
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


def test_a_bubble_is_ours_when_the_details_button_says_you():
    """The bug this encodes cost real money in confusion.

    Measured live in a Community chat: only the two NEWEST bubbles carried the
    "You:" group, while all 100 carried "Open chat details for You". The rest
    fell through to alignment, which put our bubbles at x=1005 against a
    threshold of 1154 — so our own sent messages were read as INCOMING, stored
    as incoming, and would have been posted to the webhook and replied to."""
    from wadam.whatsapp.reader import _bubble_item_is_ours

    bubble = _Ctrl("DataItemControl", "", [
        _Ctrl("ButtonControl", "Open chat details for You"),
        _Ctrl("TextControl", "Hello Note #5"),
    ])
    assert _bubble_item_is_ours(bubble) is True


def test_a_bubble_from_someone_else_is_not_ours():
    from wadam.whatsapp.reader import _bubble_item_is_ours

    bubble = _Ctrl("DataItemControl", "", [
        _Ctrl("ButtonControl", "Open chat details for Nagen US"),
        _Ctrl("TextControl", "Good morning"),
    ])
    # None, not False: no OUR-label found, so alignment decides.
    assert _bubble_item_is_ours(bubble) is None


def test_the_you_group_label_still_counts():
    from wadam.whatsapp.reader import _bubble_item_is_ours

    bubble = _Ctrl("DataItemControl", "", [
        _Ctrl("GroupControl", "You:"),
        _Ctrl("TextControl", "Hello"),
    ])
    assert _bubble_item_is_ours(bubble) is True
