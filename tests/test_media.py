"""Media — where the bytes land, and what is allowed to leave.

The rule worth being rigid about here is the outbound one. A webhook endpoint
is somebody else's code on somebody else's machine, and the reply contract lets
it name a file for this process to read and post into a WhatsApp conversation.
Unconfined, that is an exfiltration primitive handed to whoever runs the
endpoint — or to anyone who has compromised it. Most of this file is that
boundary, including the symlink case, which is the one a string-prefix check
waves straight through.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from wadam.config import Settings
from wadam.domain.models import ChatConfig
from wadam.engine.pipeline import MediaError, MessagePipeline
from wadam.engine.guards import Cooldown
from wadam.engine.webhook import (
    MediaReply, build_payload, media_from_object, parse_media_reply,
)
from wadam.openwa import SendError
from wadam.openwa.inbound import parse_delivery
from wadam.storage.media import MediaStore, extension_for, safe_mimetype
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository
from tests.fakes import FakeMongo

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture()
def store(tmp_path: Path) -> MediaStore:
    media = MediaStore(tmp_path / "media", max_bytes=1024)
    media.prepare()
    return media


# ── keeping what arrives ──────────────────────────────────────────────


def test_saving_returns_a_relative_path(store: MediaStore):
    saved = store.save("111111111111111@lid", "ABC123", PNG, mimetype="image/png")

    assert saved is not None
    # Relative, because this string goes into MongoDB and the JSON mirror and
    # has to survive the application being moved or containerised.
    assert not Path(saved.path).is_absolute()
    assert saved.size == len(PNG)
    assert saved.mimetype == "image/png"
    assert (store.root / saved.path).read_bytes() == PNG


def test_the_chat_id_cannot_escape_the_media_directory(store: MediaStore):
    """`@`, `/` and `..` in an id are separators to a filesystem, not text."""
    saved = store.save("../../../etc", "../../passwd", PNG, mimetype="image/png")

    assert saved is not None
    written = (store.root / saved.path).resolve()
    assert store.root.resolve() in written.parents


def test_media_over_the_cap_is_not_written(store: MediaStore):
    assert store.save("chat", "big", b"x" * 2048, mimetype="image/png") is None


def test_nothing_is_left_behind_when_a_save_is_refused(store: MediaStore):
    store.save("chat", "big", b"x" * 2048, mimetype="image/png")
    assert list(store.root.rglob("*.part")) == []


def test_the_extension_comes_from_the_mimetype_not_the_host_registry():
    # guess_extension answers ".jpe" for image/jpeg on some Windows installs,
    # which is an install-dependent filename and therefore a bug that only
    # reproduces on one machine.
    assert extension_for("image/jpeg") == ".jpg"
    assert extension_for("audio/ogg") == ".ogg"
    assert extension_for("application/octet-stream", "report.docx") == ".docx"
    assert extension_for("", "") == ".bin"


def test_a_junk_mimetype_is_dropped_rather_than_echoed():
    assert safe_mimetype("image/png; charset=utf-8") == "image/png"
    assert safe_mimetype("not a mimetype at all") == ""
    assert safe_mimetype("../../etc/passwd") == ""


# ── the confinement rule ──────────────────────────────────────────────


def test_a_file_in_the_outbox_resolves(store: MediaStore):
    target = store.outbox / "report.pdf"
    target.write_bytes(b"%PDF-1.4")

    assert store.resolve_outgoing("outbox/report.pdf") == target.resolve()


def test_a_relative_path_climbing_out_is_refused(store: MediaStore):
    with pytest.raises(ValueError, match="outside the media directory"):
        store.resolve_outgoing("../../../../etc/passwd")


def test_an_absolute_path_outside_is_refused(store: MediaStore, tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours")

    with pytest.raises(ValueError, match="outside the media directory"):
        store.resolve_outgoing(str(secret))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_a_symlink_pointing_out_is_refused(store: MediaStore, tmp_path: Path):
    """The case a string-prefix check waves through.

    The link's own path is inside the media directory; only following it says
    otherwise, which is why resolution happens before the comparison.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours")
    link = store.outbox / "innocent.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not create symlinks unprivileged")

    with pytest.raises(ValueError, match="outside the media directory"):
        store.resolve_outgoing("outbox/innocent.txt")


def test_a_missing_file_is_refused_without_leaking_whether_it_exists(store: MediaStore):
    with pytest.raises(ValueError, match="no readable file"):
        store.resolve_outgoing("outbox/nothing-here.pdf")


def test_an_oversized_outgoing_file_is_refused(store: MediaStore):
    fat = store.outbox / "fat.bin"
    fat.write_bytes(b"x" * 2048)

    with pytest.raises(ValueError, match="over the"):
        store.resolve_outgoing("outbox/fat.bin")


# ── understanding an endpoint's answer ────────────────────────────────


@pytest.mark.parametrize("body,expected_url,expected_path,kind", [
    ('{"media": {"url": "https://x/a.jpg"}}', "https://x/a.jpg", "", ""),
    ('{"media": {"path": "outbox/a.pdf"}}', "", "outbox/a.pdf", ""),
    ('{"media": "https://x/a.jpg"}', "https://x/a.jpg", "", ""),
    ('{"media": "outbox/a.pdf"}', "", "outbox/a.pdf", ""),
    ('{"image": "https://x/a.jpg"}', "https://x/a.jpg", "", "image"),
    ('{"document": "outbox/a.pdf"}', "", "outbox/a.pdf", "document"),
    ('{"file": "outbox/a.pdf"}', "", "outbox/a.pdf", "document"),
    ('{"data": {"media": {"url": "https://x/a.jpg"}}}', "https://x/a.jpg", "", ""),
])
def test_the_media_shapes_an_endpoint_may_answer_with(body, expected_url, expected_path, kind):
    media = parse_media_reply(body)

    assert media is not None
    assert media.url == expected_url
    assert media.path == expected_path
    assert media.kind == kind


def test_a_bare_relative_string_is_a_path_not_a_url():
    """Guessing the other way would have OpenWA fetch `outbox/a.pdf`."""
    media = parse_media_reply('{"media": "outbox/a.pdf"}')

    assert media is not None and media.path and not media.url


def test_a_plain_text_reply_carries_no_media():
    assert parse_media_reply('{"reply": "just words"}') is None
    assert parse_media_reply("Confirmed") is None
    assert parse_media_reply("") is None


def test_naming_both_a_url_and_a_path_is_refused():
    """They point at different bytes; picking one silently sends the wrong file."""
    assert parse_media_reply('{"media": {"url": "https://x/a.jpg", "path": "outbox/a.pdf"}}') is None


def test_text_and_media_can_arrive_together():
    body = '{"reply": "Here you go", "media": {"url": "https://x/a.jpg"}}'

    from wadam.engine.webhook import parse_reply
    assert parse_reply(body) == "Here you go"
    assert parse_media_reply(body).url == "https://x/a.jpg"


def test_the_send_api_reads_the_same_shapes():
    """One implementation, so the request body and the reply cannot drift."""
    assert media_from_object({"image": "https://x/a.jpg"}).kind == "image"
    assert media_from_object({"message": "text only"}) is None


# ── what the delivery carries ─────────────────────────────────────────


def _delivery(media: dict | None) -> dict:
    # `type` drives media_kind, so a message with no media at all must not
    # claim one -- "chat" is what OpenWA sends for plain text.
    message = {"chatId": "111111111111111@lid", "waMessageId": "ABC",
               "body": "look", "type": "image" if media is not None else "chat"}
    if media is not None:
        message["media"] = media
    return {"data": {"message": message}}


def test_the_bytes_arrive_inside_the_delivery():
    payload = _delivery({"mimetype": "image/png",
                         "data": base64.b64encode(PNG).decode(), "filename": "a.png"})

    parsed = parse_delivery(payload)

    assert parsed is not None and parsed.has_media
    assert parsed.media_mimetype == "image/png"
    assert base64.b64decode(parsed.media_base64) == PNG
    assert not parsed.media_omitted


def test_an_omitted_payload_is_recorded_as_itself():
    """OpenWA's own cap is configuration on its side, not a failure on ours."""
    parsed = parse_delivery(_delivery({"mimetype": "video/mp4", "omitted": True,
                                       "sizeBytes": 90_000_000}))

    assert parsed is not None
    assert parsed.media_omitted and parsed.media_size == 90_000_000
    assert not parsed.media_base64


def test_a_message_with_no_media_says_so():
    parsed = parse_delivery(_delivery(None))
    assert parsed is not None and not parsed.has_media


def test_a_size_arriving_as_a_string_is_still_a_size():
    parsed = parse_delivery(_delivery({"mimetype": "image/png", "sizeBytes": "2048"}))
    assert parsed is not None and parsed.media_size == 2048


# ── the pipeline ──────────────────────────────────────────────────────


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.media_sent: list[dict] = []

    def send_text(self, chat_id: str, text: str) -> dict:
        self.sent.append((chat_id, text))
        return {"ok": True}

    def send_media(self, chat_id: str, kind: str, **kwargs) -> dict:
        self.media_sent.append({"chatId": chat_id, "kind": kind, **kwargs})
        return {"ok": True}

    def download_media(self, chat_id: str, message_id: str):
        raise SendError("OpenWA returned 404: no stored media", status=404)

    @staticmethod
    def kind_for(mimetype: str, filename: str = "") -> str:
        from wadam.openwa import OpenWAClient
        return OpenWAClient.kind_for(mimetype, filename)


@pytest.fixture()
def pipeline(tmp_path: Path, store: MediaStore):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    client = FakeClient()
    pipe = MessagePipeline(repository=repository, client=client, webhook=None,
                           cooldown=Cooldown(0), media=store)
    yield pipe, repository, client, store
    repository.stop()


def test_an_arriving_photo_is_kept_and_its_path_recorded(pipeline):
    pipe, repo, _client, store = pipeline
    msg = parse_delivery(_delivery({"mimetype": "image/png",
                                    "data": base64.b64encode(PNG).decode()}))

    pipe.process(msg)

    stored = repo.messages_for("111111111111111@lid", 10)[0]
    assert stored.media_path and not stored.media_error
    assert (store.root / stored.media_path).read_bytes() == PNG


def test_an_omitted_payload_leaves_a_reason_not_a_silence(pipeline):
    pipe, repo, _client, _store = pipeline
    msg = parse_delivery(_delivery({"mimetype": "video/mp4", "omitted": True,
                                    "sizeBytes": 90_000_000}))

    pipe.process(msg)

    stored = repo.messages_for("111111111111111@lid", 10)[0]
    assert not stored.media_path
    assert "omitted" in stored.media_error and "90000000" in stored.media_error


def test_media_that_cannot_be_fetched_does_not_cost_the_message(pipeline):
    """The text, the sender and the reply all matter more than the photo."""
    pipe, repo, _client, _store = pipeline
    msg = parse_delivery(_delivery({"mimetype": "image/png"}))  # metadata, no bytes

    outcome = pipe.process(msg)

    stored = repo.messages_for("111111111111111@lid", 10)[0]
    assert outcome.ok
    assert stored.text == "look"
    assert stored.media_error and not stored.media_path


def test_sending_a_file_from_the_outbox(pipeline):
    pipe, _repo, client, store = pipeline
    (store.outbox / "report.pdf").write_bytes(b"%PDF-1.4")

    pipe.send_media("111111111111111@lid", MediaReply(path="outbox/report.pdf"))

    assert client.media_sent[0]["kind"] == "document"
    assert base64.b64decode(client.media_sent[0]["base64_data"]) == b"%PDF-1.4"


def test_sending_a_url_hands_it_to_openwa_rather_than_fetching_it(pipeline):
    """So a reply cannot use this process as a proxy onto its own network."""
    pipe, _repo, client, _store = pipeline

    pipe.send_media("111111111111111@lid",
                    MediaReply(url="https://x/a.jpg", kind="image", caption="hi"))

    assert client.media_sent[0]["url"] == "https://x/a.jpg"
    assert "base64_data" not in client.media_sent[0]
    assert client.media_sent[0]["caption"] == "hi"


def test_a_path_outside_the_outbox_is_refused_by_the_pipeline(pipeline, tmp_path: Path):
    pipe, _repo, client, _store = pipeline
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours")

    with pytest.raises(MediaError, match="outside the media directory"):
        pipe.send_media("111111111111111@lid", MediaReply(path=str(secret)))

    assert client.media_sent == []


# ── the payload an endpoint sees ──────────────────────────────────────


def test_the_payload_gains_a_media_object_only_when_there_is_media():
    from wadam.domain.models import StoredMessage
    chat = ChatConfig(chat_id="111111111111111@lid", chat_name="Alice")

    plain = build_payload(chat, StoredMessage(text="hi"))
    assert "media" not in plain

    withmedia = build_payload(chat, StoredMessage(
        text="look", media_kind="image", media_path="c/a.png",
        media_mimetype="image/png", media_size=72))
    assert withmedia["media"]["stored"] is True
    assert withmedia["media"]["path"] == "c/a.png"


def test_the_payload_carries_a_path_never_the_bytes():
    """Base64 in every delivery would put someone's photos in the endpoint's
    request log, and inflate the POST by a third of the file."""
    from wadam.domain.models import StoredMessage
    payload = build_payload(
        ChatConfig(chat_id="c", chat_name="A"),
        StoredMessage(media_kind="image", media_path="c/a.png", media_size=72))

    assert "data" not in payload["media"] and "base64" not in payload["media"]
