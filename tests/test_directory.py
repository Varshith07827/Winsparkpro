"""Syncing the directory, and resolving an identifier to a chat.

The numbers in these tests are the ones measured on a live account: an address
book that pages at 1000 rows and returns every contact twice, and 4 shared
names in 494.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig, Contact
from wadam.engine.directory import Directory, normalize_phone
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.fakes import FakeMongo

LID = "132671606911101@lid"
PHONE = "919100251854"


class FakeClient:
    """Stands in for OpenWA. Records what was asked of it."""

    def __init__(self, chats=None, contacts=None, phones=None, check=None) -> None:
        self._chats = chats or []
        self._contacts = contacts or []
        self._phones = phones or {}
        self._check = check or {}
        self.phone_calls: list[str] = []
        self.check_calls: list[str] = []

    def list_chats(self):
        return list(self._chats)

    def list_contacts(self):
        return list(self._contacts)

    def resolve_phone(self, contact_id):
        self.phone_calls.append(contact_id)
        return self._phones.get(contact_id, "")

    def check_number(self, number):
        self.check_calls.append(number)
        return self._check.get(number, "")


@pytest.fixture()
def repo(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    yield repository
    repository.stop()


def a_directory(repo, **client_kwargs):
    client = FakeClient(**client_kwargs)
    return Directory(repo, client), client


# ── normalizing a number ──────────────────────────────────────────────


@pytest.mark.parametrize("given", [
    "919100251854", "+919100251854", "+91 91002 51854", "91-9100-251854",
    " 91 9100 251854 ",
])
def test_the_same_number_written_five_ways(given):
    assert normalize_phone(given) == "919100251854"


def test_a_name_is_not_a_number():
    assert normalize_phone("Prasanthi Gvpt") == ""
    assert normalize_phone("CSE - C 2023-27") == ""


# ── sync ──────────────────────────────────────────────────────────────


def test_chats_are_stored_with_automation_off(repo):
    """A sync that switched chats on would start answering every conversation
    in the account at once."""
    directory, _ = a_directory(repo, chats=[
        {"id": LID, "name": "Prasanthi Gvpt", "isGroup": False},
        {"id": "1203@g.us", "name": "Team", "isGroup": True},
    ])

    summary = directory.sync()

    assert summary["chats"] == 2
    assert summary["new_chats"] == 2
    assert all(c.automation_enabled is False for c in repo.list_chats())
    assert repo.get_chat("1203@g.us").is_group is True


def test_a_chats_phone_is_resolved_once_and_then_cached(repo):
    """The call is about a second, and a LID's phone number cannot change."""
    directory, client = a_directory(
        repo, chats=[{"id": LID, "name": "Prasanthi Gvpt"}], phones={LID: PHONE})

    directory.sync()
    directory.sync()
    directory.sync()

    assert repo.get_chat(LID).phone_number == PHONE
    assert client.phone_calls == [LID]


def test_a_chat_is_named_from_the_address_book(repo):
    """The chat list's own name may be stylised unicode; the address-book name
    is what a person would actually type."""
    directory, _ = a_directory(
        repo,
        chats=[{"id": LID, "name": "\U0001d54a\U0001d552\U0001d55a"}],
        contacts=[{"id": f"{PHONE}@c.us", "name": "Prasanthi Gvpt", "number": PHONE}],
        phones={LID: PHONE},
    )

    directory.sync()

    chat = repo.get_chat(LID)
    assert chat.contact_name == "Prasanthi Gvpt"
    assert chat.chat_name == "\U0001d54a\U0001d552\U0001d55a"


def test_a_resync_keeps_automation_and_updates_the_name(repo):
    directory, _ = a_directory(repo, chats=[{"id": LID, "name": "Old"}])
    directory.sync()
    chat = repo.get_chat(LID)
    chat.automation_enabled = True
    repo.save_chat(chat)

    directory._client._chats = [{"id": LID, "name": "New"}]  # noqa: SLF001
    directory.sync()

    assert repo.get_chat(LID).automation_enabled is True
    assert repo.get_chat(LID).chat_name == "New"


def test_contacts_replace_rather_than_merge(repo):
    """A contact deleted on the phone must stop resolving — a name that still
    works after you removed it is how a message reaches the wrong person."""
    directory, client = a_directory(repo, contacts=[
        {"id": "1@c.us", "name": "Gone", "number": "911111111111"},
        {"id": "2@c.us", "name": "Stays", "number": "912222222222"},
    ])
    directory.sync()
    assert repo.contact_count() == 2

    client._contacts = [{"id": "2@c.us", "name": "Stays", "number": "912222222222"}]  # noqa: SLF001
    directory.sync()

    assert repo.contact_count() == 1
    assert repo.contacts_named("Gone") == []


def test_a_sync_failure_is_survived(repo):
    """It runs on a timer. An unreachable OpenWA is logged and retried, not
    raised into a failed launch."""
    class Broken(FakeClient):
        def list_contacts(self):
            raise RuntimeError("connection reset")

    directory = Directory(repo, Broken())
    summary = directory.sync()

    assert "error" in summary


# ── resolve ───────────────────────────────────────────────────────────


@pytest.fixture()
def resolved(repo):
    directory, client = a_directory(
        repo,
        chats=[{"id": LID, "name": "Prasanthi Gvpt"}],
        contacts=[
            {"id": f"{PHONE}@c.us", "name": "Prasanthi Gvpt", "number": PHONE},
            {"id": "919999999999@c.us", "name": "No Chat Yet", "number": "919999999999"},
        ],
        phones={LID: PHONE},
        check={"919999999999": "888888888888@lid"},
    )
    directory.sync()
    return directory, client


def test_an_exact_chat_id_resolves(resolved):
    directory, _ = resolved
    assert directory.resolve(LID).chat_id == LID


def test_a_phone_number_resolves_to_its_chat(resolved):
    directory, client = resolved

    answer = directory.resolve("+91 91002 51854")

    assert answer.chat_id == LID
    assert client.check_calls == []      # an existing chat is preferred


def test_a_contact_name_resolves(resolved):
    directory, _ = resolved
    assert directory.resolve("Prasanthi Gvpt").chat_id == LID


def test_a_name_matches_regardless_of_case(resolved):
    directory, _ = resolved
    assert directory.resolve("prasanthi gvpt").chat_id == LID


def test_a_contact_with_no_chat_is_reached_through_openwa(resolved):
    """The point of #5: message someone who has never messaged you."""
    directory, client = resolved

    answer = directory.resolve("No Chat Yet")

    assert answer.chat_id == "888888888888@lid"
    assert client.check_calls == ["919999999999"]


def test_a_bare_number_with_no_chat_is_checked_live(resolved):
    directory, _ = resolved
    assert directory.resolve("919999999999").chat_id == "888888888888@lid"


def test_a_number_not_on_whatsapp_is_refused(resolved):
    directory, _ = resolved

    answer = directory.resolve("919000000000")

    assert not answer.ok
    assert "not on WhatsApp" in answer.reason


# ── refusing rather than guessing ─────────────────────────────────────


def test_two_contacts_sharing_a_name_are_refused(repo):
    """Measured on a live address book: 4 shared names in 494. Rare is not
    never, and the failure it prevents is messaging the wrong person."""
    directory, _ = a_directory(repo, contacts=[
        {"id": "911111111111@c.us", "name": "Chakri", "number": "911111111111"},
        {"id": "912222222222@c.us", "name": "Chakri", "number": "912222222222"},
    ])
    directory.sync()

    answer = directory.resolve("Chakri")

    assert not answer.ok
    assert answer.ambiguous
    assert len(answer.candidates) == 2


def test_two_chats_sharing_a_name_are_refused(repo):
    directory, _ = a_directory(repo, chats=[
        {"id": "a@lid", "name": "Support"},
        {"id": "b@lid", "name": "Support"},
    ])
    directory.sync()

    answer = directory.resolve("Support")

    assert not answer.ok
    assert answer.ambiguous


def test_a_ten_digit_number_is_refused_with_an_explanation(resolved):
    """The account messages Indian and US numbers. Guessing between +91 and +1
    would send to a real person who is not the intended one."""
    directory, _ = resolved

    answer = directory.resolve("9100251854")

    assert not answer.ok
    assert "country code" in answer.reason


def test_an_unknown_name_is_refused(resolved):
    directory, _ = resolved

    answer = directory.resolve("Nobody At All")

    assert not answer.ok
    assert "nothing matches" in answer.reason


def test_an_empty_identifier_is_refused(resolved):
    directory, _ = resolved
    assert not directory.resolve("   ").ok
