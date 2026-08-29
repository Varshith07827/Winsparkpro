"""First-run setup: when it appears, and what it is allowed to write.

Both tests here exist because the real thing went wrong. A configuration that
worked was treated as unconfigured, the dialog reappeared, and pressing Start
overwrote the file — including the MongoDB URI, which then pointed somewhere
else entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wadam.ui.first_run import env_text, needs_setup, write_env


def test_the_two_required_keys_are_enough(tmp_path: Path):
    """This also demanded OPENWA_SESSION_ID until that became discoverable at
    startup. A .env without it then looked unconfigured, and setup reappeared
    for an installation that was working perfectly."""
    env = tmp_path / ".env"
    env.write_text("MONGODB_URI=mongodb://localhost:27017\nOPENWA_API_KEY=k\n",
                   encoding="utf-8")

    assert needs_setup(env) is False


@pytest.mark.parametrize("body", [
    "",
    "MONGODB_URI=mongodb://localhost:27017\n",
    "OPENWA_API_KEY=k\n",
    "# MONGODB_URI=mongodb://localhost:27017\n# OPENWA_API_KEY=k\n",
])
def test_setup_is_needed_when_either_is_missing(tmp_path: Path, body: str):
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")

    assert needs_setup(env) is True


def test_a_missing_file_needs_setup(tmp_path: Path):
    assert needs_setup(tmp_path / "nothing-here") is True


def test_an_existing_env_is_never_overwritten(tmp_path: Path):
    """The guard that would have saved the file. Starting over is deleting the
    file yourself, deliberately."""
    env = tmp_path / ".env"
    env.write_text("MONGODB_URI=mongodb://elsewhere:27017\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_env(env, "mongodb://localhost:27017", "http://localhost:2785", "k")

    assert "elsewhere" in env.read_text(encoding="utf-8")


def test_the_written_env_carries_only_what_cannot_be_guessed(tmp_path: Path):
    env = tmp_path / ".env"
    write_env(env, "mongodb://localhost:27017", "http://localhost:2785", "owa_k1_x")
    written = env.read_text(encoding="utf-8")

    live = {line.split("=", 1)[0] for line in written.splitlines()
            if "=" in line and not line.startswith("#")}

    assert "MONGODB_URI" in live
    assert "OPENWA_API_KEY" in live
    # Discovered or generated at startup, so writing them would only be a way
    # for the file and reality to disagree later.
    assert "OPENWA_SESSION_ID" not in live
    assert "WEBHOOK_SECRET" not in live


def test_env_text_mentions_the_ssrf_setting_openwa_needs(tmp_path: Path):
    """The one thing that must be changed on OpenWA's side, and the failure it
    causes is silent: registration fails and no message ever arrives."""
    assert "SSRF_ALLOWED_HOSTS" in env_text("mongodb://x", "http://y", "k")
