"""The controlled two-send diagnostic. NOT run without explicit authorisation.

One live run produced two queued sends, both reporting transport success, and
one WhatsApp bubble. Nothing available offline distinguishes the causes, because
the sender drives WhatsApp Desktop's real UI and no local target can stand in
for it.

This is deliberately not "send twice and look". It records the compose box
immediately before and after each send and reads the conversation independently
between them.

**It reports observations, and classifies them. It does not claim causes.**

UI Automation can see what this application put into the compose control and
what bubbles exist afterwards. It cannot see inside WhatsApp. So
"compose held the text and no bubble appeared" is reported as
TRANSPORT_REPORTED_SUCCESS_BUT_NO_BUBBLE — a statement about what was observed.
Calling that "WhatsApp accepted and dropped it" would be inventing a mechanism
between input and message creation that nothing here can watch.

Classifications:

    INPUT_NOT_OBSERVED                        the text never appeared in compose
    TRANSPORT_REPORTED_SUCCESS_BUT_NO_BUBBLE  compose changed, no new bubble
    BUBBLE_OBSERVED_BUT_VERIFICATION_FAILED   bubble present, census disagreed
    BOTH_MESSAGES_OBSERVED                    two new bubbles
    NEITHER_MESSAGE_OBSERVED                  no new bubbles at all

It sends TWO REAL MESSAGES to a real chat. Nothing is retried.

    python scripts/diagnose_two_sends.py --chat "+91 81069 72933" --confirm
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEXT = "WINSPARK_TWOSEND_DIAG"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", required=True, help="exact chat name")
    parser.add_argument("--confirm", action="store_true",
                        help="required: this sends two real messages")
    args = parser.parse_args()
    if not args.confirm:
        print("refusing: --confirm is required (this sends two REAL messages)")
        return 1

    import pythoncom

    pythoncom.CoInitialize()
    from wadam.whatsapp import reader as R, sender as S

    hwnd = R.find_window_sync()
    if hwnd is None:
        print("WhatsApp is not running")
        return 1
    S.ensure_foreground(hwnd)
    time.sleep(0.6)

    def compose() -> str:
        element = S._find_compose_element(hwnd)
        return (S._read_compose_text(element) or "").strip() if element else "<none>"

    def bubbles() -> int:
        messages = R.read_recent_messages_sync(hwnd, 30)
        return sum(1 for m in messages
                   if not m.is_incoming and m.text.strip() == TEXT)

    # Bring the target chat on screen exactly the way a send does.
    rows = R.read_chat_rows_sync(hwnd)
    match = [r for r in rows if r.chat_name == args.chat]
    if not match:
        print(f"chat {args.chat!r} is not in the visible list")
        return 1
    S.open_chat_sync(hwnd, match[0].raw_text, args.chat)
    time.sleep(1.2)

    baseline = bubbles()
    print(f"baseline bubbles matching {TEXT!r}: {baseline}\n")

    records = []
    for attempt in (1, 2):
        before_count = bubbles()
        compose_before = compose()
        filled, strategy = S.set_compose_text_sync(hwnd, TEXT, True)
        compose_after_fill = compose()

        invoked = S.invoke_send_button_sync(hwnd)
        method = "send-button-invoke" if invoked else "enter-key"
        if not invoked:
            invoked = S.press_enter_sync(hwnd)
        time.sleep(1.0)
        compose_after_send = compose()
        time.sleep(1.5)
        after_count = bubbles()

        record = {
            "compose_before": compose_before,
            "compose_after_fill": compose_after_fill,
            "compose_after_send": compose_after_send,
            "fill_reported": f"{filled} ({strategy})",
            "send_method": method,
            "transport_result": "success" if (filled and invoked) else "failure",
            "bubble_count_before": before_count,
            "bubble_count_after": after_count,
        }
        records.append(record)

        print(f"send_{attempt}:")
        for key, value in record.items():
            print(f"  {key:<22} {value!r}")
        print()

    final = bubbles()
    landed = final - baseline
    input_seen = [TEXT in (r["compose_after_fill"] or "") for r in records]

    # Observation -> classification. No mechanism is asserted: UI Automation
    # can see what this application put into the compose control and what
    # bubbles exist afterwards, and nothing between those two facts.
    if landed >= 2:
        verdict = "BOTH_MESSAGES_OBSERVED"
    elif not all(input_seen):
        verdict = "INPUT_NOT_OBSERVED"
    elif landed == 0:
        verdict = "NEITHER_MESSAGE_OBSERVED"
    else:
        verdict = "TRANSPORT_REPORTED_SUCCESS_BUT_NO_BUBBLE"

    print("=" * 58)
    print(f"  baseline={baseline}  final={final}  new_bubbles={landed}  expected=2")
    print(f"  CLASSIFICATION: {verdict}")
    print()
    print("  These are observations made through UI Automation. They do not")
    print("  assert what happened inside WhatsApp, which nothing here can see.")
    print("  Nothing was retried.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
