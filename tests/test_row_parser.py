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
