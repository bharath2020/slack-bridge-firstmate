"""Thin wrapper over Firstmate's ``fm-inbox.sh``.

The subcommands used are ``note`` (idempotent capture), ``receipts`` (the
reply feed and pending notes, read-only) and ``status`` (read-only).
``announce`` repairs a note that was saved but whose wake failed (exit 3).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass


class InboxError(RuntimeError):
    pass


@dataclass
class NoteResult:
    note_id: str
    outcome: str  # created | replay
    announced: bool | None
    acknowledged: bool


class FmInbox:
    # A heavily loaded Mac has taken over 60 s to save and announce one note.
    def __init__(self, script: str, fm_home: str, timeout: float = 180.0):
        self.script = script
        self.fm_home = fm_home
        self.timeout = timeout

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, FM_HOME=self.fm_home)
        try:
            return subprocess.run(
                [self.script, *args],
                input=stdin,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InboxError(f"fm-inbox {args[0]} failed to run: {exc.__class__.__name__}")

    def note(self, request_id: str, body: str) -> NoteResult:
        proc = self._run(["note", "--request-id", request_id, "--json", "-"], stdin=body)
        # exit 3 = saved but not announced: still a saved note we can map.
        if proc.returncode not in (0, 3):
            raise InboxError(f"fm-inbox note exited {proc.returncode}")
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise InboxError("fm-inbox note returned unreadable JSON")
        result = NoteResult(
            note_id=data["id"],
            outcome=data.get("outcome", "created"),
            announced=data.get("announced"),
            acknowledged=bool(data.get("acknowledged")),
        )
        if proc.returncode == 3:
            result.announced = self.announce(result.note_id)
        return result

    def announce(self, note_id: str) -> bool:
        return self._run(["announce", note_id]).returncode == 0

    def receipts(self, after: str | None, all_pending: bool = False) -> dict:
        args = ["receipts", "--all-replies"]
        if all_pending:
            args.append("--all-pending")
        if after:
            args += ["--after", after]
        proc = self._run(args)
        if proc.returncode != 0:
            raise InboxError(f"fm-inbox receipts exited {proc.returncode}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise InboxError("fm-inbox receipts returned unreadable JSON")
        if data.get("schema") != "fm-inbox-receipts.v1":
            raise InboxError(f"unexpected receipts schema {data.get('schema')!r}")
        return data

    def status(self) -> str:
        proc = self._run(["status"])
        if proc.returncode != 0:
            raise InboxError(f"fm-inbox status exited {proc.returncode}")
        return proc.stdout
