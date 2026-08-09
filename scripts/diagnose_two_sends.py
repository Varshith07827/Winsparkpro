"""The controlled two-send diagnostic. NOT run without explicit authorisation.

One live run produced two queued sends, both reporting transport success, and
one WhatsApp bubble. Nothing available offline distinguishes the causes, because
the sender drives WhatsApp Desktop's real UI and no local target can stand in
for it.

This is deliberately not "send twice and look". It records the compose box
immediately before and after each send, and reads the conversation
independently between them, so the observation maps to exactly one cause:

    compose never contained #2          -> winSpark transport/input problem
    compose contained #2, no bubble     -> WhatsApp/UI delivery problem
    bubble exists, reader missed it     -> reader/verification problem
    two bubbles exist                   -> the earlier result was a race
    one bubble, repeatedly              -> genuine sender reliability issue

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

    for attempt in (1, 2):
        print(f"--- send #{attempt} ---")
        print(f"  compose before fill : {compose()!r}")
        filled, strategy = S.set_compose_text_sync(hwnd, TEXT, True)
        print(f"  fill reported       : {filled} ({strategy})")
        print(f"  compose after fill  : {compose()!r}")

        invoked = S.invoke_send_button_sync(hwnd)
        method = "send-button-invoke" if invoked else "enter-key"
        if not invoked:
            invoked = S.press_enter_sync(hwnd)
        print(f"  send invoked        : {invoked} ({method})")
        time.sleep(1.0)
        print(f"  compose after send  : {compose()!r}")

        time.sleep(1.5)
        count = bubbles()
        print(f"  bubbles now         : {count} (was {baseline + attempt - 1})")
        print(f"  this send landed    : {count == baseline + attempt}\n")

    final = bubbles()
    print("=" * 58)
    print(f"  baseline {baseline}   after two sends {final}   expected {baseline + 2}")
    if final == baseline + 2:
        print("  -> both landed. The earlier single-bubble result was a race.")
    elif final == baseline + 1:
        print("  -> one landed. Read the per-send compose lines above:")
        print("     compose held the text  -> WhatsApp accepted and dropped it")
        print("     compose never held it  -> winSpark never typed it")
    else:
        print("  -> neither landed. Transport is reporting success falsely.")
    print("  Nothing was retried.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
