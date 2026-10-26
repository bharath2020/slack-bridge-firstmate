"""Small durable JSON state shared by the daemon and the CLI.

Holds the receipts cursor, the note id -> Slack thread mapping, topic
threads (topic key -> Slack thread), project channels (project key ->
private channel), the restart
history used for rate limiting, the queue of fresh-session requests,
Slack messages not yet handed to Firstmate, the load guard's state, the
pending `shed` lists and the processes `shed pause` stopped.
Writes are atomic (temp file + rename) and serialized with an flock so the
daemon and a concurrent ``slack-bridge say`` never clobber each other.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

THREAD_TTL_SECONDS = 30 * 24 * 3600
SEEN_LIMIT = 500


def _empty() -> dict[str, Any]:
    return {
        "version": 1,
        "receipts_cursor": None,  # None = not initialized yet
        "threads": {},  # note_id -> {"channel", "ts", "at"}
        "topics": {},  # topic key -> {"channel", "ts", "at", "note"?}
        "projects": {},  # project key -> {"channel", "name", "at"}
        "dm_channel": None,
        "restarts": [],  # epoch seconds of keeper restarts
        "last_restart_alert": 0,
        "fresh_requests": [],  # [{"id", "at", "source", "channel"?, "thread_ts"?}]
        "seen_commands": [],  # Slack message keys already routed as commands
        # request id -> {"channel", "ts", "thread_ts", "text"?, "note_id"?, "at",
        #                "attempts", "next_at", "delayed"} until Firstmate is woken
        "handoffs": {},
        # {"active", "since", "alerted", "alerted_at", "demoted"}
        "guard": {},
        # live `shed` lists, newest few: "<channel> <thread_ts>" ->
        #   {"at", "action", "procs": [{"pid", "command", "label"}]}
        "shed_offers": {},
        "paused": [],  # [{"pid", "command", "label"}] stopped by `shed pause`
        "bridge_restart": None,  # {"channel", "thread_ts"} to answer once back up
    }


class State:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / "state.json"
        self.lock_path = self.dir / "state.lock"

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.dir, 0o700)

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return _empty()
        data.pop("shed_offer", None)  # the old single-list shape
        base = _empty()
        base.update(data)
        return base

    @contextlib.contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        """Lock, yield the current state for mutation, then write it back."""
        self._ensure_dir()
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            yield data
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # Convenience helpers -------------------------------------------------

    def remember_thread(self, note_id: str, channel: str, ts: str) -> None:
        now = int(time.time())
        with self.update() as st:
            st["threads"][note_id] = {"channel": channel, "ts": ts, "at": now}
            cutoff = now - THREAD_TTL_SECONDS
            st["threads"] = {
                k: v for k, v in st["threads"].items() if v.get("at", now) >= cutoff
            }

    def thread_for(self, note_id: str) -> dict[str, Any] | None:
        return self.read()["threads"].get(note_id)

    def project_for_channel(self, channel: str) -> str | None:
        """The project key mapped to ``channel``, if any."""
        for key, proj in self.read()["projects"].items():
            if proj["channel"] == channel:
                return key
        return None

    def mark_command_seen(self, key: str) -> bool:
        """Record a routed command; return False if it was already seen."""
        with self.update() as st:
            seen = st["seen_commands"]
            if key in seen:
                return False
            seen.append(key)
            del seen[:-SEEN_LIMIT]
            return True
