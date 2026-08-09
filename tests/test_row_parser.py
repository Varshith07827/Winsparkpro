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


# ---------------------------------------------------------------------------
# Raw message integrity — sender must never bleed into the body
# ---------------------------------------------------------------------------


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Node:
    """A UIA control shaped like the ones WhatsApp actually produces."""

    def __init__(self, kind, name="", rect=None, children=()):
        self.ControlTypeName = kind
        self.Name = name
        self.BoundingRectangle = rect or _Rect(0, 0, 0, 0)
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


def _live_bubble(sender=None, body_lines=(), quoted=None, sender_phone=None):
    """A bubble laid out the way the live tree is: the sender name inside a
    ButtonControl, an optional phone group ON THE SENDER'S LINE, and each body
    line in its own group BELOW. Geometry copied from a real capture."""
    kids = []
    if sender is not None:
        kids.append(_Node("ButtonControl", sender, _Rect(623, 214, 1215, 240), [
            _Node("TextControl", "~ ", _Rect(623, 218, 639, 236)),
            _Node("TextControl", sender.replace("Maybe ", ""), _Rect(638, 218, 691, 236)),
        ]))
    if sender_phone is not None:
        kids.append(_Node("GroupControl", "", _Rect(1214, 214, 1331, 240), [
            _Node("TextControl", sender_phone, _Rect(1214, 219, 1331, 237)),
        ]))
    if quoted is not None:
        kids.append(_Node("ButtonControl", "Quoted message", _Rect(615, 251, 1336, 310), [
            _Node("TextControl", quoted, _Rect(631, 260, 803, 278)),
        ]))
    top = 332
    for line in body_lines:
        kids.append(_Node("GroupControl", "", _Rect(623, top, 1331, top + 20), [
            _Node("TextControl", line, _Rect(623, top + 2, 696, top + 18)),
        ]))
        top += 26
    kids.append(_Node("GroupControl", "", _Rect(1261, top, 1331, top + 18), [
        _Node("TextControl", "10:44 PM", _Rect(1261, top + 1, 1331, top + 17)),
    ]))
    return _Node("DataItemControl", "", _Rect(527, 206, 1905, top + 30), kids)


def _body(**kw):
    from wadam.whatsapp.reader import _bubble_item_content

    content = _bubble_item_content(_live_bubble(**kw))
    return content[0] if content else None


def test_the_acceptance_case_a_partially_saved_contact():
    """Sender "Pritam +91 63032 31690", message "Ok mam".

    Measured live before the fix: the webhook received
    "Pritam +91 63032 31690 Ok mam"."""
    assert _body(sender="Maybe Pritam", sender_phone="+91 63032 31690",
                 body_lines=["Ok mam"]) == "Ok mam"


def test_a_genuine_message_that_looks_like_a_sender_header_is_untouched():
    """The other half of the acceptance criterion. If someone actually types
    "Pritam +91 63032 31690 Ok mam", that IS the message and must survive."""
    text = "Pritam +91 63032 31690 Ok mam"
    assert _body(sender="Dittakavi Saritha", body_lines=[text]) == text


def test_a_message_that_is_only_a_phone_number_survives():
    assert _body(sender="Manohar Sripati", body_lines=["+91 63032 31690"]) == "+91 63032 31690"


def test_a_message_beginning_with_the_senders_own_name_survives():
    assert _body(sender="Pritam", body_lines=["Pritam will send it"]) == "Pritam will send it"


def test_a_message_identical_to_the_sender_name_survives():
    assert _body(sender="Pritam", body_lines=["Pritam"]) == "Pritam"


def test_a_quoted_reply_does_not_leak_into_the_body():
    assert _body(sender="Manohar Sripati", quoted="Dittakavi Saritha Work",
                 body_lines=["Sure sir"]) == "Sure sir"


def test_a_multiline_message_keeps_every_line():
    assert _body(sender="Nagen US", body_lines=["line one", "line two", "line three"]) == \
        "line one line two line three"


def test_a_unicode_sender_does_not_corrupt_the_body():
    assert _body(sender="Ελληνικά Χρήστης", sender_phone="+30 210 1234567",
                 body_lines=["Καλημέρα"]) == "Καλημέρα"


def test_a_sender_whose_name_contains_digits():
    assert _body(sender="Agent 007", sender_phone="+44 7700 900123",
                 body_lines=["Mission accepted"]) == "Mission accepted"


def test_a_saved_contact_with_no_phone_shown():
    assert _body(sender="Dittakavi Saritha", body_lines=["Manohar good morning"]) == \
        "Manohar good morning"


def test_an_unsaved_number_as_the_sender():
    assert _body(sender="+91 63032 31690", sender_phone="+91 63032 31690",
                 body_lines=["hello there"]) == "hello there"


def test_a_bubble_with_no_sender_button_at_all():
    """Our own messages carry no sender button; the body must still read."""
    assert _body(body_lines=["Hello Note #7"]) == "Hello Note #7"
