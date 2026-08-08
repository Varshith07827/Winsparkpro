"""The inbound send API.

Two things get the most attention here, because they are the ways this feature
could do real harm:

* **Resolution must never guess.** An identifier that matches two chats is
  refused. Four digits is 10,000 values, so with a few hundred chats a
  collision is likely rather than exotic — and a wrong guess sends someone's
  message to the wrong person, silently, with a 200 in reply.
* **Authentication must not be optional.** This endpoint sends messages from a
  personal WhatsApp account.

The HTTP layer is exercised over a real socket rather than by calling handlers
directly, because the parts most likely to be wrong about an HTTP server are the
parts that are actually HTTP.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wadam.api.host import SendApiHost
from wadam.api.resolver import resolve_chat
from wadam.api.server import SendApiServer, SendResponse
from wadam.config import ConfigError, Settings, load_settings
from wadam.domain.models import ChatConfig, MessageStatus, chat_id_for, contact_id_for
from wadam.engine.engine import SendOutcome
from wadam.storage.json_backup import JsonBackupStore
from wadam.storage.repository import Repository

from tests.test_storage import FakeMongo


def chat(name: str, external_id: str = "") -> ChatConfig:
    return ChatConfig(chat_id=chat_id_for(name), chat_name=name, external_id=external_id)


# ---------------------------------------------------------------------------
# Deriving a contact ID
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("+1 (555) 010-9423", "9423"),
    ("15550109423", "9423"),
    ("+1 555 010 9423", "9423"),
    ("919876543210", "3210"),
])
def test_a_number_yields_its_last_four_digits(name, expected):
    assert contact_id_for(name) == expected


@pytest.mark.parametrize("name", [
    "Aarav Sharma",
    "CSE - C 2023-27",       # digits, but a name
    "Papa",
    "Hostel Block B",
    "",
    "123",                    # too few digits to be a number
])
def test_a_name_yields_nothing_to_derive(name):
    # A saved contact shows a name and never its number, so there is nothing to
    # derive — those chats get their contact ID typed in.
    assert contact_id_for(name) == ""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_an_explicit_contact_id_resolves():
    chats = [chat("Aarav Sharma", "9423"), chat("Priya Nair", "1122")]
    resolution = resolve_chat(chats, "9423")
    assert resolution.ok
    assert resolution.chat.chat_name == "Aarav Sharma"
    assert resolution.matched_by == "external_id"


def test_a_numeric_chat_name_resolves_by_its_last_four():
    chats = [chat("+1 (555) 010-9423"), chat("Priya Nair")]
    resolution = resolve_chat(chats, "9423")
    assert resolution.ok
    assert resolution.matched_by == "contact_last4"


def test_an_explicit_id_outranks_a_derived_one():
    """Someone typed "9423" against Priya. That is a deliberate assignment and
    beats a number that merely happens to end the same way."""
    chats = [chat("+1 (555) 010-9423"), chat("Priya Nair", "9423")]
    resolution = resolve_chat(chats, "9423")
    assert resolution.ok
    assert resolution.chat.chat_name == "Priya Nair"
    assert resolution.matched_by == "external_id"


def test_chat_id_and_name_also_resolve():
    alice = chat("Alice")
    assert resolve_chat([alice], alice.chat_id).matched_by == "chat_id"
    assert resolve_chat([alice], "alice").matched_by == "chat_name"


def test_two_chats_sharing_an_id_are_refused_not_guessed():
    """The failure this whole module exists to prevent."""
    chats = [chat("Aarav", "9423"), chat("Priya", "9423")]
    resolution = resolve_chat(chats, "9423")

    assert not resolution.ok
    assert resolution.ambiguous
    assert resolution.candidates == ("Aarav", "Priya")


def test_two_numbers_ending_the_same_way_are_refused():
    chats = [chat("+1 (555) 010-9423"), chat("+1 (555) 018-9423")]
    resolution = resolve_chat(chats, "9423")
    assert not resolution.ok
    assert resolution.ambiguous


def test_an_ambiguous_tier_does_not_fall_through_to_a_cleaner_one():
    """Two chats answer to this by contact ID. Continuing to the name tier
    would find a single tidy match and send there — which is precisely the
    wrong answer."""
    chats = [chat("Aarav", "9423"), chat("Priya", "9423"), chat("9423")]
    resolution = resolve_chat(chats, "9423")
    assert not resolution.ok
    assert resolution.ambiguous


def test_an_unknown_id_resolves_to_nothing():
    resolution = resolve_chat([chat("Alice")], "0000")
    assert not resolution.ok
    assert not resolution.ambiguous


def test_an_empty_id_resolves_to_nothing():
    assert not resolve_chat([chat("Alice", "9423")], "   ").ok


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def write_env(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "MONGODB_URI=mongodb://localhost:27017\nDATABASE_NAME=wadam\n"
        f"JSON_BACKUP_FOLDER={tmp_path / 'backup'}\n{extra}\n",
        encoding="utf-8",
    )
    return path


def test_the_api_is_off_unless_a_port_is_set(tmp_path: Path):
    settings = load_settings(write_env(tmp_path, ""))
    assert settings.api_port == 0
    assert settings.api_host == "127.0.0.1"


def test_a_tokenless_listener_is_allowed_on_loopback(tmp_path: Path):
    """Nothing off this machine can reach 127.0.0.1, and anything already
    running as you here could drive WhatsApp directly anyway."""
    settings = load_settings(write_env(tmp_path, "API_PORT=8765"))
    assert settings.api_port == 8765
    assert settings.api_token == ""


def test_a_tokenless_listener_is_refused_off_loopback(tmp_path: Path):
    """Bound anywhere reachable, the token is the only thing between the
    network and someone's WhatsApp account."""
    with pytest.raises(ConfigError) as raised:
        load_settings(write_env(tmp_path, "API_PORT=8765\nAPI_HOST=0.0.0.0"))
    assert any("API_TOKEN is required" in p for p in raised.value.problems)


def test_a_short_token_is_rejected_off_loopback(tmp_path: Path):
    with pytest.raises(ConfigError) as raised:
        load_settings(write_env(
            tmp_path, "API_PORT=8765\nAPI_HOST=0.0.0.0\nAPI_TOKEN=hunter2"))
    assert any("at least 16" in p for p in raised.value.problems)


def test_binding_publicly_with_a_token_warns_but_starts(tmp_path: Path):
    settings = load_settings(write_env(
        tmp_path, "API_PORT=8765\nAPI_HOST=0.0.0.0\nAPI_TOKEN=" + "x" * 32))
    assert settings.api_port == 8765
    assert any("other machines" in w for w in settings.warnings)


def test_the_token_is_redacted_in_the_settings_mirror(tmp_path: Path):
    settings = load_settings(write_env(
        tmp_path, "API_PORT=8765\nAPI_TOKEN=" + "s3cret" * 6))
    assert settings.redacted()["api_token"] == "***"


# ---------------------------------------------------------------------------
# The HTTP surface, over a real socket
# ---------------------------------------------------------------------------


TOKEN = "t" * 32


class _Recorder:
    def __init__(self, response: SendResponse | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.response = response or SendResponse(200, {"ok": True, "chat": "Alice"})

    def __call__(self, identifier: str, text: str) -> SendResponse:
        self.calls.append((identifier, text))
        return self.response


@pytest.fixture()
def api():
    recorder = _Recorder()
    server = SendApiServer("127.0.0.1", 0, TOKEN, recorder)
    server.start()
    # Port 0 means "any free port"; ask the socket which one it got.
    port = server._httpd.server_address[1]
    yield f"http://127.0.0.1:{port}", recorder
    server.stop()


def post(url: str, body, token: str | None = TOKEN, path: str = "/"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as ex:
        return ex.code, json.loads(ex.read())


def test_a_valid_request_reaches_the_sender(api):
    url, recorder = api
    status, payload = post(url, {"id": "9423", "message": "Hello Varshith"})
    assert status == 200
    assert payload["ok"] is True
    assert recorder.calls == [("9423", "Hello Varshith")]


def test_the_documented_curl_shape_works(api):
    """Exactly the request format this was built for."""
    url, recorder = api
    status, _ = post(url, b'{"id":"9423","message":"Hello Varshith"}', path="/wam/")
    assert status == 200
    assert recorder.calls == [("9423", "Hello Varshith")]


def test_every_send_path_is_accepted(api):
    url, recorder = api
    for path in ("/", "/send", "/wam", "/wam/"):
        assert post(url, {"id": "1", "message": "x"}, path=path)[0] == 200
    assert len(recorder.calls) == 4


def test_an_unknown_path_is_a_404(api):
    url, recorder = api
    status, payload = post(url, {"id": "1", "message": "x"}, path="/nope")
    assert status == 404
    assert payload["code"] == "not_found"
    assert recorder.calls == []


def test_an_open_listener_accepts_a_request_with_no_token():
    """With no token configured, authentication is off — that is the point of
    leaving it empty, and it must actually work."""
    recorder = _Recorder()
    server = SendApiServer("127.0.0.1", 0, "", recorder)
    server.start()
    try:
        url = f"http://127.0.0.1:{server._httpd.server_address[1]}"
        status, payload = post(url, {"id": "9423", "message": "Hello Varshith"}, token=None)
    finally:
        server.stop()
    assert status == 200 and payload["ok"] is True
    assert recorder.calls == [("9423", "Hello Varshith")]
    assert server.authentication_required is False


def test_a_configured_token_is_still_enforced(api):
    url, recorder = api
    assert post(url, {"id": "1", "message": "x"}, token=None)[0] == 401
    assert post(url, {"id": "1", "message": "x"}, token="w" * 32)[0] == 401
    assert recorder.calls == []


def test_a_request_without_a_token_is_refused(api):
    url, recorder = api
    status, payload = post(url, {"id": "9423", "message": "Hello"}, token=None)
    assert status == 401
    assert payload["code"] == "unauthorized"
    assert recorder.calls == [], "nothing may be sent without authentication"


def test_a_wrong_token_is_refused(api):
    url, recorder = api
    status, _ = post(url, {"id": "9423", "message": "Hello"}, token="w" * 32)
    assert status == 401
    assert recorder.calls == []


def test_an_x_api_token_header_also_works(api):
    url, recorder = api
    request = urllib.request.Request(url, data=b'{"id":"1","message":"x"}', method="POST")
    request.add_header("X-API-Token", TOKEN)
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
    assert recorder.calls == [("1", "x")]


@pytest.mark.parametrize("body,code", [
    (b"not json", "bad_json"),
    (b"[1,2,3]", "bad_json"),
    ({"message": "no id"}, "missing_id"),
    ({"id": "9423"}, "missing_message"),
    ({"id": "9423", "message": "   "}, "missing_message"),
])
def test_malformed_requests_are_rejected_with_a_reason(api, body, code):
    url, recorder = api
    status, payload = post(url, body)
    assert status == 400
    assert payload["code"] == code
    assert recorder.calls == []


def test_an_unquoted_numeric_id_is_accepted(api):
    # {"id": 9423} rather than {"id": "9423"} is the commonest integration slip,
    # and refusing it would teach nobody anything.
    url, recorder = api
    assert post(url, {"id": 9423, "message": "x"})[0] == 200
    assert recorder.calls == [("9423", "x")]


def test_message_aliases_are_understood(api):
    url, recorder = api
    for key in ("message", "text", "reply", "body"):
        post(url, {"id": "1", key: "hello"})
    assert [text for _id, text in recorder.calls] == ["hello"] * 4


def test_health_answers_without_a_token_and_says_nothing_private(api):
    url, _recorder = api
    with urllib.request.urlopen(url + "/health", timeout=5) as response:
        status = response.status
        payload = json.loads(response.read())
    assert status == 200 and payload["ok"] is True
    # It answers "is this listening?" and nothing else — no chat names, no
    # counts describing someone's conversations.
    assert set(payload) == {"ok", "app", "version", "requests"}


def test_a_get_on_the_send_path_is_405(api):
    url, _recorder = api
    try:
        urllib.request.urlopen(url, timeout=5)
        pytest.fail("expected 405")
    except urllib.error.HTTPError as ex:
        assert ex.code == 405


def test_an_exception_in_the_sender_becomes_a_500_not_a_traceback():
    """A bug in the send path must reach the caller as JSON they can read, not
    as a dropped connection."""
    def explode(_identifier, _text):
        raise RuntimeError("something broke")

    server = SendApiServer("127.0.0.1", 0, TOKEN, explode)
    server.start()
    try:
        url = f"http://127.0.0.1:{server._httpd.server_address[1]}"
        status, payload = post(url, {"id": "1", "message": "x"})
    finally:
        server.stop()

    assert status == 500
    assert payload["code"] == "internal"
    assert "something broke" in payload["error"]


def test_a_malformed_response_from_the_send_path_still_answers():
    """Regression: a callback returning the wrong type used to raise past the
    handler's guard, closing the socket with no response at all."""
    server = SendApiServer("127.0.0.1", 0, TOKEN, lambda _i, _t: None)
    server.start()
    try:
        url = f"http://127.0.0.1:{server._httpd.server_address[1]}"
        status, payload = post(url, {"id": "1", "message": "x"})
    finally:
        server.stop()

    assert status == 500
    assert payload["code"] == "internal"


# ---------------------------------------------------------------------------
# The host: resolution + engine, without HTTP
# ---------------------------------------------------------------------------


def engine_chat_id(repo, name: str) -> str:
    for c in repo.list_chats():
        if c.chat_name == name:
            return c.chat_id
    raise AssertionError(f"no chat named {name}")


class FakeEngine:
    def __init__(self, outcome: SendOutcome | None = None) -> None:
        self.outcome = outcome or SendOutcome(True, outgoing_id="q1", queued=True)
        self.sent: list[tuple[str, str]] = []
        self.raise_on_submit: Exception | None = None
        self.queued: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.status_result = None

    def submit(self, factory):
        if self.raise_on_submit:
            raise self.raise_on_submit
        import concurrent.futures

        future: concurrent.futures.Future = concurrent.futures.Future()
        coroutine = factory()
        # Which engine method the API reached for. The coroutine is never
        # awaited (the outcome is canned), so this is the only record of it.
        self.calls.append(coroutine.cr_code.co_name)
        coroutine.close()
        future.set_result(self.outcome)
        return future

    async def send_message(self, chat_id: str, text: str, origin: str = "api"):
        self.sent.append((chat_id, text))
        return self.outcome

    async def queue_message(self, chat_id: str, text: str, origin: str = "api"):
        self.queued.append((chat_id, text))
        return self.outcome

    def outgoing_status(self, outgoing_id: str):
        return self.status_result


@pytest.fixture()
def host(tmp_path: Path):
    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0,
                        api_port=8765, api_token=TOKEN)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    engine = FakeEngine()
    yield SendApiHost(settings, repository, engine), repository, engine
    repository.stop()


def test_a_resolved_send_reports_which_chat_it_reached(host):
    api_host, repo, engine = host
    repo.save_chat(chat("Aarav Sharma", "9423"))

    response = api_host._send("9423", "Hello Varshith")

    # 202, not 200: the message is queued, not yet delivered.
    assert response.status == 202
    assert response.payload["chat"] == "Aarav Sharma"
    assert response.payload["matched_by"] == "external_id"
    assert response.payload["status"] == "queued"
    assert response.payload["outgoing_id"] == "q1"


def test_a_send_is_queued_rather_than_performed_inline(host):
    """The request must not wait on WhatsApp.

    A physical send costs seconds; a caller posting twenty of them cannot hold
    twenty connections open for minutes. Blocking here is what produced
    `timeout` responses for messages that had actually been delivered."""
    api_host, repo, engine = host
    repo.save_chat(chat("Aarav Sharma", "9423"))

    api_host._send("9423", "Hello")

    assert engine.calls == ["queue_message"], (
        "the API must enqueue, not call the blocking send path"
    )


def test_a_status_lookup_reports_what_happened(host):
    api_host, repo, engine = host
    from wadam.domain.models import OutgoingMessage, OutgoingStatus

    message = OutgoingMessage(chat_id="c1", chat_name="Aarav", text="Hello")
    message.status = OutgoingStatus.DELIVERED
    engine.status_result = message

    response = api_host.status(message.outgoing_id)

    assert response.status == 200
    assert response.payload["status"] == "delivered"
    assert response.payload["chat"] == "Aarav"


def test_an_unknown_status_id_is_404(host):
    api_host, _repo, engine = host
    engine.status_result = None

    response = api_host.status("nope")

    assert response.status == 404
    assert response.payload["code"] == "unknown_id"


def test_an_ambiguous_id_returns_409_and_names_the_conflict(host):
    api_host, repo, _engine = host
    repo.save_chat(chat("Aarav", "9423"))
    repo.save_chat(chat("Priya", "9423"))

    response = api_host._send("9423", "Hello")

    assert response.status == 409
    assert response.payload["code"] == "ambiguous_id"
    assert response.payload["candidates"] == ["Aarav", "Priya"]
    # And it is on the record, because a silently refused send is its own kind
    # of surprise.
    assert any(e.event == "api.ambiguous_id" for e in repo.recent_logs())


def test_an_unknown_id_returns_404_with_the_fix(host):
    api_host, repo, _engine = host
    repo.save_chat(chat("Aarav", "9423"))

    response = api_host._send("0000", "Hello")

    assert response.status == 404
    assert "last four digits" in response.payload["error"]


def test_a_failed_send_is_502_not_200(host):
    api_host, repo, engine = host
    repo.save_chat(chat("Aarav", "9423"))
    engine.outcome = SendOutcome(False, error="the compose box still had text")

    response = api_host._send("9423", "Hello")

    assert response.status == 502
    assert response.payload["ok"] is False
    assert "compose box" in response.payload["error"]


def test_an_unavailable_engine_is_503(host):
    api_host, repo, engine = host
    repo.save_chat(chat("Aarav", "9423"))
    engine.raise_on_submit = RuntimeError("The engine is not running.")

    response = api_host._send("9423", "Hello")
    assert response.status == 503


# ---------------------------------------------------------------------------
# Persistence of an API send
# ---------------------------------------------------------------------------


def test_an_api_send_is_stored_like_any_other_message(tmp_path: Path):
    """An API-originated message goes through the same persistence as a reply —
    it is a message in the conversation, and the record has to say so."""
    import asyncio

    from wadam.engine.engine import AutomationEngine

    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()

    engine = AutomationEngine(settings, repository, lambda _s: None)

    class Sender:
        async def send_async(self, name, text):
            from wadam.whatsapp.sender import SendResult

            return SendResult.succeeded("uia-value-pattern + send-button-invoke")

    engine._sender = Sender()
    target = chat("Aarav Sharma", "9423")
    repository.save_chat(target)

    outcome = asyncio.run(engine.send_message(target.chat_id, "Hello Varshith"))

    assert outcome.ok
    stored = repository.messages_for(target.chat_id)
    assert [(m.direction, m.text, m.origin, m.status) for m in stored] == [
        ("out", "Hello Varshith", "api", MessageStatus.SENT)
    ]
    assert repository.get_chat(target.chat_id).last_outgoing_text == "Hello Varshith"
    assert any(e.event == "api.send" for e in repository.recent_logs())

    engine._sta.dispose()
    repository.stop()


def test_a_failed_api_send_records_nothing_as_sent(tmp_path: Path):
    import asyncio

    from wadam.engine.engine import AutomationEngine

    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    engine = AutomationEngine(settings, repository, lambda _s: None)

    class Sender:
        async def send_async(self, name, text):
            from wadam.whatsapp.sender import SendResult

            return SendResult.failed("the compose box still had text")

    engine._sender = Sender()
    target = chat("Aarav Sharma", "9423")
    repository.save_chat(target)

    outcome = asyncio.run(engine.send_message(target.chat_id, "Hello"))

    assert not outcome.ok
    # We do not claim to have sent something we could not verify.
    assert repository.messages_for(target.chat_id) == []
    assert repository.get_chat(target.chat_id).last_error

    engine._sta.dispose()
    repository.stop()


# ---------------------------------------------------------------------------
# Discovery fills the contact ID in
# ---------------------------------------------------------------------------


def test_a_numeric_chat_gets_a_contact_id_on_discovery(tmp_path: Path):
    from wadam.engine.discovery import ChatDiscovery
    from wadam.whatsapp.reader import ChatRow
    from wadam.whatsapp.row_parser import parse_chat_row

    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    discovery = ChatDiscovery(repository, settings)

    rows = [
        ChatRow(**parse_chat_row("+1 (555) 010-9423 12:00 pm hi")),
        ChatRow(**parse_chat_row("Aarav Sharma 12:00 pm hi")),
    ]
    result = discovery.sync(rows)
    by_name = {c.chat_name: c for c in result.seen}

    assert by_name["+1 (555) 010-9423"].external_id == "9423"
    # A saved contact has no number to derive from — left empty for someone to
    # fill in, not guessed at.
    assert by_name["Aarav Sharma"].external_id == ""
    repository.stop()


def test_a_hand_set_contact_id_is_never_overwritten(tmp_path: Path):
    from wadam.engine.discovery import ChatDiscovery
    from wadam.whatsapp.reader import ChatRow
    from wadam.whatsapp.row_parser import parse_chat_row

    settings = Settings(mongodb_uri="mongodb://localhost:27017", database_name="test",
                        json_backup_folder=tmp_path, json_autosave_interval=0)
    backup = JsonBackupStore(tmp_path, autosave_interval=0)
    backup.ensure_folder()
    repository = Repository(settings, FakeMongo(), backup)
    repository.start()
    discovery = ChatDiscovery(repository, settings)

    discovery.sync([ChatRow(**parse_chat_row("Aarav Sharma 12:00 pm hi"))])
    stored = repository.get_chat(chat_id_for("Aarav Sharma"))
    stored.external_id = "7777"
    repository.save_chat(stored)

    discovery.sync([ChatRow(**parse_chat_row("Aarav Sharma 12:05 pm something new"))])

    assert repository.get_chat(chat_id_for("Aarav Sharma")).external_id == "7777"
    repository.stop()


# ---------------------------------------------------------------------------
# Transport contract + capability probing
# ---------------------------------------------------------------------------


def test_the_sender_satisfies_the_transport_protocol():
    """The pipeline, relay and send API all talk to a Transport, not to
    WhatsApp. If that ever stops being true, swapping in a Business Platform
    transport becomes a rewrite instead of a class."""
    from wadam.whatsapp.transport import Transport

    assert hasattr(Transport, "send")
    # A structural check rather than an isinstance one: WhatsAppSender needs a
    # live STA thread to construct, and the point is the shape, not the object.
    from wadam.whatsapp.sender import WhatsAppSender

    assert callable(getattr(WhatsAppSender, "send_async"))
    assert callable(getattr(WhatsAppSender, "capabilities"))


def test_transport_capabilities_describe_the_cost_to_the_user():
    from wadam.whatsapp.transport import TransportCapabilities

    uia = TransportCapabilities(
        name="Windows UI Automation", requires_foreground=True, moves_cursor=True,
        uses_clipboard=True, requires_interactive_desktop=True,
        requires_whatsapp_running=True)
    text = uia.describe()
    assert "foreground" in text and "cursor" in text

    api = TransportCapabilities(
        name="Business Platform", requires_foreground=False, moves_cursor=False,
        uses_clipboard=False, requires_interactive_desktop=False,
        requires_whatsapp_running=False)
    assert "no user-visible effect" in api.describe()


def test_capabilities_cache_is_keyed_on_the_whatsapp_version(tmp_path: Path):
    """A WhatsApp update must invalidate the probe. Otherwise the day the
    provider starts implementing ValuePattern, this application keeps typing
    character by character forever."""
    from wadam.whatsapp.capabilities import Capabilities, CapabilityStore

    store = CapabilityStore(tmp_path / "caps.json")
    store.save(Capabilities(whatsapp_version="2.2630.102.0", value_pattern_write=False))

    reloaded = CapabilityStore(tmp_path / "caps.json").load()
    assert reloaded is not None
    assert reloaded.whatsapp_version == "2.2630.102.0"
    assert reloaded.headless_send_possible is False

    # A working ValuePattern is what "headless" means, by either route.
    assert Capabilities(value_pattern_write=True).headless_send_possible is True
    assert Capabilities(legacy_set_value=True).headless_send_possible is True


def test_the_capability_summary_states_the_verdict_plainly():
    from wadam.whatsapp.capabilities import Capabilities

    blocked = Capabilities(whatsapp_version="2.2630.102.0")
    assert "unavailable" in blocked.summary() and "discards" in blocked.summary()
    working = Capabilities(whatsapp_version="9.9", value_pattern_write=True)
    assert "AVAILABLE" in working.summary()
