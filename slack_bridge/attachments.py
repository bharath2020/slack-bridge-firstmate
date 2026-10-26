"""Pictures the captain attaches to a Slack message (subtype ``file_share``).

Each picture (PNG, JPEG, GIF, WebP or HEIC, by Slack's mimetype) is
downloaded from its ``url_private`` with the bot token in an Authorization
header and saved under the bridge's private state directory, one folder per
message (0700, files 0600). The note then names each saved picture by its
absolute path, so Firstmate can open it. Other files are never downloaded;
the note names them as skipped. Each message's folder is deleted once it is
older than ``KEEP_SECONDS`` (``expire``, run by the daemon).

File URLs, the token and picture bytes are never logged or posted; logs
carry only counts and short reason codes.
"""

from __future__ import annotations

import contextlib
import http.client
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("slack_bridge.attachments")

MAX_FILE_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 60.0
KEEP_SECONDS = 7 * 24 * 60 * 60  # saved pictures are deleted after 7 days

IMAGE_MIMETYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}

# Reason code -> plain words for the note and the captain's thread.
REASONS = {
    "no-access": "the Slack app is not allowed to read files yet "
                 "(it needs the files:read permission)",
    "no-link": "Slack sent no download link for it "
               "(the Slack app may need the files:read permission)",
    "too-large": f"it is larger than {MAX_FILE_BYTES // 1024 // 1024} MB",
    "network": "the download did not complete",
    "save-failed": "it could not be saved on the Mac",
}


class FetchError(RuntimeError):
    """A failed download; carries only a reason code, never the URL."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def reason_text(reason: str) -> str:
    if reason.startswith("http-"):
        return f"Slack answered with an error (HTTP {reason[5:]})"
    return REASONS.get(reason, reason)


def file_refs(event: dict) -> list[dict]:
    """The fields the bridge needs from each file of a Slack message."""
    refs = []
    for f in event.get("files") or []:
        if not isinstance(f, dict):
            continue
        refs.append({"name": str(f.get("name") or f.get("title") or "file"),
                     "mimetype": str(f.get("mimetype") or ""),
                     "file_access": str(f.get("file_access") or ""),
                     "size": f.get("size") if isinstance(f.get("size"), int) else None,
                     "url": f.get("url_private") or f.get("url_private_download") or ""})
    return refs


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would resend the Authorization header to wherever Slack points;
    # an unauthorized file request is redirected to the sign-in page anyway.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SlackDownloader:
    """GET a ``url_private`` with the bot token; returns the bytes."""

    def __init__(self, bot_token: str, timeout: float = FETCH_TIMEOUT_SECONDS):
        self._token = bot_token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:  # never let the token reach a log line
        return "SlackDownloader(token=<redacted>)"

    def __call__(self, url: str, max_bytes: int) -> bytes:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "files.slack.com":
            raise FetchError("no-link")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                # Without files:read Slack serves its HTML sign-in page.
                if resp.headers.get_content_type() == "text/html":
                    raise FetchError("no-access")
                data = resp.read(max_bytes + 1)
                length = resp.headers.get("Content-Length", "")
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400 or exc.code in (401, 403):
                raise FetchError("no-access") from None
            raise FetchError(f"http-{exc.code}") from None
        except (OSError, ValueError, http.client.HTTPException):
            # HTTPException: e.g. IncompleteRead when a chunked body is cut short.
            raise FetchError("network") from None
        if len(data) > max_bytes:
            raise FetchError("too-large")
        if length.isdigit() and len(data) < int(length):
            raise FetchError("network")  # the connection closed before the whole file
        return data


@dataclass
class Saved:
    lines: list[str]  # appended to the note body
    failed: list[tuple[str, str]]  # (picture name, reason code)


def _safe_name(name: str, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "picture"
    if not stem.lower().endswith(f".{ext}"):
        stem = f"{stem}.{ext}"
    return stem[-120:]


class Attachments:
    def __init__(self, directory: Path, fetch: Callable[[str, int], bytes],
                 max_bytes: int = MAX_FILE_BYTES, clock: Callable[[], float] = time.time):
        self.dir = Path(directory)
        self.fetch = fetch
        self.max_bytes = max_bytes
        self.clock = clock

    def expire(self, keep_seconds: float = KEEP_SECONDS) -> int:
        """Delete message folders older than ``keep_seconds``; returns how many.

        Only real folders directly under the directory count, aged by their
        own modification time; symlinks are never followed or removed. A
        folder that cannot be deleted is counted and skipped.
        """
        try:
            if not stat.S_ISDIR(os.lstat(self.dir).st_mode):
                return 0
            entries = list(os.scandir(self.dir))
        except FileNotFoundError:
            return 0
        cutoff = self.clock() - keep_seconds
        removed, failed, reasons = 0, 0, set()
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                shutil.rmtree(entry.path)
            except OSError as exc:
                failed += 1
                reasons.add(type(exc).__name__)
                continue
            removed += 1
        if removed or failed:
            log.info("files expired: removed=%d failed=%d reasons=%s", removed, failed,
                     ",".join(sorted(reasons)) or "-")
        return removed

    def save(self, rid: str, refs: list[dict]) -> Saved:
        """Download and save every picture of one message; never raises."""
        lines: list[str] = []
        failed: list[tuple[str, str]] = []
        skipped = 0
        folder = self.dir / re.sub(r"[^A-Za-z0-9._-]+", "_", rid)
        for i, ref in enumerate(refs, 1):
            name, mimetype = ref["name"], ref["mimetype"].lower()
            # Slack sent the file without its details: it may well be a picture.
            partial = not mimetype or ref.get("file_access") == "check_file_info"
            ext = IMAGE_MIMETYPES.get(mimetype)
            if ext is None and not partial:
                skipped += 1
                lines.append(f"[file skipped, not a picture: {name}]")
                continue
            try:
                if partial:
                    raise FetchError("no-link")
                path = self._save_one(folder, f"{i}-{_safe_name(name, ext)}", ref)
            except FetchError as exc:
                failed.append((name, exc.reason))
                lines.append(f"[picture not fetched: {name} — {reason_text(exc.reason)}]")
                continue
            lines.append(f"[picture: {path}]")
        log.info("files: request_id=%s saved=%d failed=%d skipped=%d reasons=%s", rid,
                 len(refs) - len(failed) - skipped, len(failed), skipped,
                 ",".join(sorted({r for _, r in failed})) or "-")
        return Saved(lines, failed)

    def _save_one(self, folder: Path, filename: str, ref: dict) -> Path:
        if not ref["url"]:
            raise FetchError("no-link")
        if ref["size"] is not None and ref["size"] > self.max_bytes:
            raise FetchError("too-large")
        data = self.fetch(ref["url"], self.max_bytes)
        try:
            for d in (self.dir, folder):
                d.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(d, 0o700)
            path = folder / filename
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".part-")  # created 0600
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError:
            raise FetchError("save-failed") from None
        return path.resolve()
