"""Configuration validation.

Startup is load → validate → launch, and validation reports *every* problem at
once rather than the first. Fixing a misconfiguration one restart at a time is
a miserable way to spend an evening.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wadam.config import ConfigError, load_settings

GOOD = """
MONGODB_URI=mongodb://localhost:27017
OPENWA_URL=http://localhost:2785
OPENWA_API_KEY=owa_k1_abcdef
OPENWA_SESSION_ID=f51856f9-b22f-4bf6-b99a-f45ab86f1ac8
WEBHOOK_SECRET=a-secret-that-is-long-enough
"""


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Real environment variables win over the file, so a developer's own
    settings would otherwise leak into these assertions."""
    for key in list(os.environ):
        if key.startswith(("MONGODB_", "OPENWA_", "WEBHOOK_", "API_", "LOG_",
                           "DATABASE_", "COOLDOWN_", "ANSWER_", "JSON_")):
            monkeypatch.delenv(key, raising=False)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def problems_for(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        load_settings(write(tmp_path, text))
    return caught.value.problems


# ── the happy path ────────────────────────────────────────────────────


def test_a_complete_env_loads(tmp_path: Path):
    settings = load_settings(write(tmp_path, GOOD))

    assert settings.openwa_url == "http://localhost:2785"
    assert settings.openwa_session_id == "f51856f9-b22f-4bf6-b99a-f45ab86f1ac8"
    assert settings.webhook_port == 8765
    assert settings.cooldown_seconds == 60.0
    assert settings.answer_groups is False


def test_a_trailing_slash_is_stripped_from_the_openwa_url(tmp_path: Path):
    settings = load_settings(write(tmp_path, GOOD.replace(
        "OPENWA_URL=http://localhost:2785", "OPENWA_URL=http://localhost:2785/")))
    assert settings.openwa_url == "http://localhost:2785"


# ── every problem at once ─────────────────────────────────────────────


def test_all_missing_requirements_are_reported_together(tmp_path: Path):
    problems = problems_for(tmp_path, "MONGODB_URI=mongodb://localhost:27017\n")
    joined = " ".join(problems)

    assert "OPENWA_API_KEY" in joined
    assert "OPENWA_SESSION_ID" in joined


def test_a_missing_mongodb_uri_is_refused(tmp_path: Path):
    text = GOOD.replace("MONGODB_URI=mongodb://localhost:27017", "")
    assert any("MONGODB_URI" in p for p in problems_for(tmp_path, text))


def test_a_non_mongodb_uri_is_refused(tmp_path: Path):
    text = GOOD.replace("mongodb://localhost:27017", "http://localhost:27017")
    assert any("mongodb://" in p for p in problems_for(tmp_path, text))


def test_a_non_http_openwa_url_is_refused(tmp_path: Path):
    text = GOOD.replace("http://localhost:2785", "localhost:2785")
    assert any("OPENWA_URL" in p for p in problems_for(tmp_path, text))


def test_mongodbs_own_databases_are_refused(tmp_path: Path):
    """Collections landing in `admin` are a mess to find and a worse one to
    clean up, so this is refused outright rather than warned about."""
    text = GOOD + "DATABASE_NAME=admin\n"
    assert any("DATABASE_NAME" in p for p in problems_for(tmp_path, text))


# ── the webhook secret ────────────────────────────────────────────────


def test_a_non_loopback_bind_requires_a_secret(tmp_path: Path):
    """The line is drawn at reachability. 0.0.0.0 is the default, because a
    Dockerised OpenWA cannot reach a loopback listener."""
    text = GOOD.replace("WEBHOOK_SECRET=a-secret-that-is-long-enough", "")
    problems = problems_for(tmp_path, text)

    assert any("WEBHOOK_SECRET is required" in p for p in problems)


def test_loopback_may_go_without_a_secret(tmp_path: Path):
    text = (GOOD.replace("WEBHOOK_SECRET=a-secret-that-is-long-enough", "")
            + "WEBHOOK_HOST=127.0.0.1\n")
    settings = load_settings(write(tmp_path, text))

    assert settings.webhook_secret == ""
    assert settings.webhook_host == "127.0.0.1"


def test_a_short_secret_is_refused(tmp_path: Path):
    text = GOOD.replace("a-secret-that-is-long-enough", "short")
    assert any("at least 16" in p for p in problems_for(tmp_path, text))


# ── the send API ──────────────────────────────────────────────────────


def test_the_send_api_is_off_without_a_port(tmp_path: Path):
    assert load_settings(write(tmp_path, GOOD)).api_port == 0


def test_a_public_send_api_requires_a_token(tmp_path: Path):
    text = GOOD + "API_PORT=8766\nAPI_HOST=0.0.0.0\n"
    assert any("API_TOKEN is required" in p for p in problems_for(tmp_path, text))


def test_a_loopback_send_api_does_not_require_a_token(tmp_path: Path):
    settings = load_settings(write(tmp_path, GOOD + "API_PORT=8766\n"))

    assert settings.api_port == 8766
    assert settings.api_host == "127.0.0.1"
    assert settings.api_token == ""


# ── redaction ─────────────────────────────────────────────────────────


def test_credentials_are_redacted_in_the_mirrored_settings(tmp_path: Path):
    """settings.json is meant to be opened and read by a human."""
    redacted = load_settings(write(tmp_path, GOOD)).redacted()

    assert redacted["openwa_api_key"] == "***"
    assert redacted["webhook_secret"] == "***"
    assert "abcdef" not in str(redacted)
