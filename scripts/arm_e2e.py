"""Arm the one controlled end-to-end test — or refuse to.

Run this once an UNSAVED-number chat exists (someone not in the address book
messages this WhatsApp, so the sidebar shows their number as the chat name and
the existing resolver persists it by itself).

It refuses to arm unless every safety condition holds. That is the point: a
previous attempt sent about thirty unintended messages to a real contact
because a relay was enabled and a test endpoint answered its polls. Nothing was
verified beforehand; everything is now.

    python scripts/arm_e2e.py            # check and arm
    python scripts/arm_e2e.py --check    # check only, change nothing

Then start the application with the guard set:

    set WADAM_ONLY_ORIGIN=webhook_reply
    python run.py
"""

from __future__ import annotations

import socket
import sys

from wadam.config import load_settings
from wadam.domain.models import phone_digits
from wadam.domain.webhook_url import webhook_url_for
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.mongo import MongoStore
from wadam.storage.repository import Repository

CAPTURE = "http://127.0.0.1:8799/?{phone_number}"


def _listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _capture_is_post_only() -> tuple[bool, str]:
    """A GET must return 204 with no body. That method is what caused the
    incident: the relay polls with GET, and a body is a message to send."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(CAPTURE.replace("{phone_number}", "probe"),
                                     method="GET")
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            body = response.read()
            if response.status == 204 and not body:
                return True, "GET -> 204, no body"
            return False, f"GET -> {response.status}, {len(body)} bytes of body"
    except urllib.error.URLError as ex:
        return False, f"capture endpoint unreachable: {ex}"


def main() -> int:
    check_only = "--check" in sys.argv
    settings = load_settings()
    store = MongoStore(settings.mongodb_uri, settings.database_name)
    store.connect()
    backup = JsonBackupStore(settings.json_backup_folder, 0)
    backup.ensure_folder()
    repo = Repository(settings, store, backup)
    repo.start()

    problems: list[str] = []
    try:
        # 1. A chat the APPLICATION resolved a number for. Never one typed in.
        candidates = [c for c in repo.list_chats()
                      if c.phone_number and phone_digits(c.chat_name) == c.phone_number]
        if not candidates:
            problems.append(
                "no chat whose NAME is a phone number — the application has not "
                "discovered a number by itself. Have somebody not in the address "
                "book send a message, then run this again. Do NOT type a number in."
            )
        chat = candidates[0] if candidates else None

        # 2. Nothing else may send.
        if settings.relay_enabled:
            problems.append("RELAY_ENABLED is true — the relay can send")
        if settings.api_port or _listening(8765):
            problems.append(f"the send API is reachable (api_port={settings.api_port})")

        ok, detail = _capture_is_post_only()
        if not ok:
            problems.append(f"capture endpoint is not POST-only: {detail}")

        print("=" * 62)
        for problem in problems:
            print(f"  REFUSED: {problem}")
        if problems:
            print("=" * 62)
            print("  not armed — nothing was changed")
            return 1

        assert chat is not None
        wanted = webhook_url_for(CAPTURE, chat.phone_number, "", chat.chat_name)
        if not check_only:
            for other in repo.list_chats():
                if other.chat_id != chat.chat_id and other.automation_enabled:
                    other.automation_enabled = False
                    repo.save_chat(other)
            chat.automation_enabled = True
            chat.webhook_override = wanted
            chat.webhook_url = wanted
            repo.save_chat(chat)
            repo.flush_json(True)

        print(f"  relay                      = OFF")
        print(f"  api                        = OFF")
        print(f"  allowed_send_origin        = webhook_reply (set WADAM_ONLY_ORIGIN)")
        print(f"  active_test_chat           = {chat.chat_name}")
        print(f"  phone_number               = {chat.phone_number}  (discovered, not entered)")
        print(f"  webhook                    = {wanted}")
        print(f"  capture_endpoint           = POST only ({detail})")
        print(f"  unexpected send protection = ARMED")
        print("=" * 62)
        print("  ARMED" if not check_only else "  would arm (--check: nothing changed)")
        return 0
    finally:
        repo.stop()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
