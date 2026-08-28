"""Working out the settings that should not have to be typed.

Three of the five values `.env` used to demand can be discovered or generated,
and each was a step where getting it wrong produced a failure that pointed
somewhere else:

**The session id.** A UUID copied from an API response, and the mistake people
make is pasting the session's *name* instead. Nearly every instance has exactly
one session, so when there is exactly one it is used; when there are several
the choice is genuinely the operator's and startup says so, listing them.

**The webhook secret.** It had to match a value pasted into OpenWA's webhook
registration by hand. When it did not, every delivery was refused with a 401
that looked like a bug in this application. It is now generated once, stored,
and given to OpenWA directly — the two ends cannot disagree because only one of
them chooses.

**The webhook itself.** Registering it was a curl invocation with four fields
that all had to agree with `.env`. It is now created on startup and kept
pointing at wherever this process is listening.

All three can still be set explicitly, and an explicit value always wins. The
discovery is for the common case, not a replacement for control.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import replace
from typing import Tuple

from wadam.config import ConfigError, Settings
from wadam.openwa import OpenWAClient
from wadam.storage.repository import Repository

logger = logging.getLogger(__name__)


def resolve_session_id(settings: Settings, client: OpenWAClient) -> str:
    """The session to use. Raises ConfigError when it cannot be decided.

    An explicit `OPENWA_SESSION_ID` is verified rather than trusted — a stale
    one produces 404s on every send, and a startup screen naming the sessions
    that do exist is a great deal more useful than that.
    """
    sessions = client.list_sessions()
    if not sessions:
        raise ConfigError([
            "OpenWA has no sessions. Create one and link a phone to it first."
        ])

    known = {str(s.get("id")): s for s in sessions if s.get("id")}

    if settings.openwa_session_id:
        if settings.openwa_session_id in known:
            return settings.openwa_session_id
        raise ConfigError([
            f"OPENWA_SESSION_ID={settings.openwa_session_id} is not a session on "
            f"{settings.openwa_url}. It must be the session's id, not its name. "
            f"Available: " + ", ".join(
                f"{s.get('name')} ({s.get('id')})" for s in sessions[:5])
        ])

    if len(known) == 1:
        session_id, session = next(iter(known.items()))
        logger.info("using the only session on this instance: %s (%s)",
                    session.get("name"), session_id)
        return session_id

    raise ConfigError([
        f"{len(known)} sessions exist, so OPENWA_SESSION_ID has to say which: " +
        ", ".join(f"{s.get('name')} ({s.get('id')})" for s in sessions[:5])
    ])


def resolve_webhook_secret(settings: Settings, repository: Repository) -> str:
    """The shared secret, generated once and remembered.

    Kept in `application_state` rather than written back into `.env`: a program
    that edits its own configuration file is a program that will one day
    clobber a comment or a value somebody was mid-way through changing.
    """
    if settings.webhook_secret:
        return settings.webhook_secret

    state = repository.app_state
    existing = getattr(state, "webhook_secret", "")
    if existing:
        return existing

    generated = secrets.token_urlsafe(32)
    state.webhook_secret = generated
    repository.save_app_state(state)
    logger.info("generated a webhook secret and stored it")
    return generated


def prepare(settings: Settings, repository: Repository,
            client_for: callable) -> Tuple[Settings, OpenWAClient]:
    """Fill in what was left out, and make OpenWA agree with it.

    Returns the effective settings and a client bound to the resolved session.
    Raises ConfigError with something a person can act on.
    """
    probe = client_for(settings.openwa_session_id or "unknown")
    session_id = resolve_session_id(settings, probe)
    secret = resolve_webhook_secret(settings, repository)

    effective = replace(settings, openwa_session_id=session_id, webhook_secret=secret)
    client = client_for(session_id)

    if settings.register_webhook:
        url = settings.webhook_public_url or (
            f"http://host.docker.internal:{settings.webhook_port}/hook")
        try:
            webhook_id = client.ensure_webhook(session_id, url, secret)
            logger.info("OpenWA will deliver message.received to %s (webhook %s)",
                        url, webhook_id)
        except Exception as error:  # noqa: BLE001 - never block a launch on this
            # Worth saying loudly rather than failing: the application is
            # perfectly usable for sending with no inbound webhook at all, and
            # the usual cause is OpenWA's SSRF guard, which needs a change on
            # OpenWA's side that this process cannot make.
            logger.error(
                "could not register the webhook with OpenWA (%s). Inbound messages "
                "will not arrive until this is fixed — if OpenWA refused a private "
                "address, add SSRF_ALLOWED_HOSTS=host.docker.internal to its .env.",
                error)

    return effective, client
