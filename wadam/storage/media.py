"""Media on disk — where the bytes go, and what is allowed to leave.

    inbound   OpenWA ──GET media──▶ MediaStore.save()  ──▶ data/media/<chat>/<id>.<ext>
                                                            │
                                                    path recorded on the message

    outbound  reply/API ──path──▶ MediaStore.resolve_outgoing() ──▶ bytes ──▶ OpenWA

Two rules, and the second is the important one.

**Nothing is written in place.** A save goes to a `.part` sibling, is fsynced,
then `os.replace`d over the real name — the same discipline as the JSON mirror,
for the same reason: a reader either sees a whole file or no file, never a
half-written one, and a crash mid-download cannot leave a truncated image that
looks perfectly valid to everything downstream.

**A path from outside is confined to the media root.** `resolve_outgoing`
exists because the outbound side takes a file path from a webhook reply, and a
webhook endpoint is somebody else's code on somebody else's machine. Without
confinement, `{"media": {"path": "/etc/shadow"}}` is a one-line request that
makes this application read a file it has no business reading and post it into
a WhatsApp conversation — an exfiltration primitive handed to whoever runs the
endpoint, or to anyone who has compromised it.

So a local path must resolve inside the media root after symlinks are followed.
Put files you intend to send in `data/media/outbox/`. A URL is not confined
here because it is not read by this process at all — it is handed to OpenWA,
which fetches it behind its own SSRF guard.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: WhatsApp itself refuses well before this; the cap exists so a hostile or
#: broken gateway cannot fill the disk one message at a time.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

#: Where a caller is meant to put files it wants sent.
OUTBOX = "outbox"

# Mimetypes are echoed into a Content-Type and used to pick an extension, so a
# junk value from upstream should not become a junk filename on disk.
_MIMETYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,80}/[A-Za-z0-9!#$&^_.+-]{1,80}$")

# Everything outside this becomes "_". Deliberately narrow rather than a
# blocklist: chat ids and WhatsApp message ids carry "@", ":" and "/", all of
# which are either separators or illegal in a Windows filename, and a blocklist
# is how "..", a drive letter or an NTFS stream slips through.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

#: Extensions for the types WhatsApp actually sends, because
#: `mimetypes.guess_extension` is driven by the host's registry: it answers
#: `.jpe` for image/jpeg on some Windows installs and None for audio/ogg on
#: most, and an install-dependent file extension is a bug that only reproduces
#: on one machine.
_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4", "video/3gpp": ".3gp",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/amr": ".amr", "application/pdf": ".pdf",
}


@dataclass(frozen=True)
class StoredMedia:
    """One saved file. `path` is relative to the media root, never absolute.

    Relative because it is written to MongoDB and to the JSON mirror, and an
    absolute path stops being true the moment the application is moved,
    containerised, or restored onto a host whose home directory is elsewhere.
    """

    path: str
    mimetype: str
    size: int
    filename: str


def extension_for(mimetype: str, filename: str = "") -> str:
    """The extension to save under. Falls back to `.bin` rather than none.

    The declared filename is preferred when it has a suffix, because a
    document's own name is more specific than its mimetype: everything from a
    .docx to a .xlsx arrives as application/octet-stream or a generic Office
    type, and saving all of them as `.bin` loses the only hint there was.
    """
    suffix = Path(filename or "").suffix
    if 1 < len(suffix) <= 12 and not _UNSAFE.search(suffix[1:]):
        return suffix.lower()
    clean = (mimetype or "").split(";", 1)[0].strip().lower()
    if clean in _EXTENSIONS:
        return _EXTENSIONS[clean]
    return mimetypes.guess_extension(clean) or ".bin" if clean else ".bin"


def safe_mimetype(value: str) -> str:
    """`value` if it is a plausible mimetype, else "". Parameters are dropped."""
    clean = (value or "").split(";", 1)[0].strip()
    return clean if _MIMETYPE.match(clean) else ""


def _safe_segment(value: str, fallback: str) -> str:
    """One path segment that cannot escape its directory or upset Windows."""
    cleaned = _UNSAFE.sub("_", (value or "").strip())[:120].strip("._") or fallback
    # "." and ".." survive the substitution above intact — both are legal
    # under the character class and both mean something to the filesystem.
    return fallback if cleaned in (".", "..") else cleaned


class MediaStore:
    """Owns the media directory. Every read and write of a media file goes
    through here so the confinement rule has exactly one implementation."""

    def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._root = Path(root)
        self._max_bytes = max(0, int(max_bytes))

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def outbox(self) -> Path:
        return self._root / OUTBOX

    def prepare(self) -> None:
        """Create the directories. Called at startup so the outbox exists to be
        put things in before anything has ever been received."""
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logger.error("could not create the media directory %s: %s", self._root, error)

    # -- inbound -----------------------------------------------------------

    def save(self, chat_id: str, message_id: str, data: bytes,
             mimetype: str = "", filename: str = "") -> Optional[StoredMedia]:
        """Write `data` and return where it went, or None if it was not written.

        None rather than an exception: a media file that could not be saved
        must not cost the message it came with. The caller records the reason
        on the message and carries on.
        """
        if not data:
            return None
        if self._max_bytes and len(data) > self._max_bytes:
            logger.warning("media for %s is %d bytes, over the %d cap — not saved",
                           message_id, len(data), self._max_bytes)
            return None

        clean_type = safe_mimetype(mimetype)
        folder = self._root / _safe_segment(chat_id, "unknown")
        name = _safe_segment(message_id, "message") + extension_for(clean_type, filename)
        target = folder / name

        try:
            folder.mkdir(parents=True, exist_ok=True)
            # Same atomic dance as the JSON mirror: a crash between the write
            # and the replace leaves a .part nobody reads, not a truncated
            # image that every consumer downstream believes is whole.
            partial = target.with_name(target.name + ".part")
            with open(partial, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, target)
        except OSError as error:
            logger.error("could not save media for %s: %s", message_id, error)
            return None

        return StoredMedia(
            path=target.relative_to(self._root).as_posix(),
            mimetype=clean_type,
            size=len(data),
            filename=filename or name,
        )

    def absolute(self, relative_path: str) -> Path:
        """The full path of something `save` returned."""
        return self._root / relative_path

    # -- outbound ----------------------------------------------------------

    def resolve_outgoing(self, path: str) -> Path:
        """The file `path` refers to, or raise ValueError.

        This is the confinement boundary described at the top of the module.
        Resolved with `strict=True` so symlinks are followed *before* the
        comparison — a link inside the root pointing at `/etc/shadow` is
        exactly the case a string-prefix check waves through.
        """
        if not (path or "").strip():
            raise ValueError("no path given")

        root = self._root.resolve()
        candidate = Path(path.strip())
        if not candidate.is_absolute():
            candidate = root / candidate

        try:
            # Not strict: ".." still normalises and symlinks that exist are
            # still followed, but a path that does not exist resolves rather
            # than raising -- so confinement can be judged before existence.
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as error:
            # RuntimeError covers a symlink loop, which resolve() raises rather
            # than returning.
            raise ValueError(f"{path!r} cannot be resolved: {error}") from error

        # Confinement is checked FIRST, and deliberately: asked about a path
        # outside the root, this must answer the same way whether or not the
        # file happens to be there. Ordering it after the existence check made
        # "../../etc/shadow" and "../../etc/nonsense" give different errors,
        # which turns a refusal into a means of testing what exists on the host.
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"{path!r} is outside the media directory. Files to send must be "
                f"under {self.outbox} — a webhook endpoint is not trusted to name "
                f"an arbitrary path on this machine."
            )
        if not resolved.is_file():
            raise ValueError(f"no readable file at {path!r}")

        size = resolved.stat().st_size
        if self._max_bytes and size > self._max_bytes:
            raise ValueError(f"{path!r} is {size} bytes, over the {self._max_bytes} cap")
        return resolved

    def read_outgoing(self, path: str) -> tuple[bytes, str, str]:
        """The bytes, mimetype and filename for a file being sent."""
        resolved = self.resolve_outgoing(path)
        guessed = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return resolved.read_bytes(), guessed, resolved.name
