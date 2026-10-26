"""Ask the captain's own model CLI for the ``shed`` and ``restart`` suggestion.

One ``claude -p`` call (``codex exec`` when claude is not installed) at
background priority (``taskpolicy -b``) with a hard timeout. The prompt holds
only executable names, CPU, memory, memory pressure, swap, load and
Firstmate's status and busy time, never a command line. The answer only shapes a suggestion: it may
pick among the listed pids and the allowed choices, anything else is ignored,
and the captain still has to reply to act. On any failure the caller's
rule-based suggestion is used and the reply says so.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field

from . import text as T
from .config import Config

log = logging.getLogger("slack_bridge.suggest")

CLAUDE_ARGS = ["-p", "--safe-mode", "--restricted", "--strict-mcp-config",
               "--no-session-persistence"]
CODEX_ARGS = ["exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "-"]
WHY_LIMIT = 200

SHED_PROMPT = """\
A Mac is overloaded. Below are the heaviest processes its owner may act on.
Choose one action for some of them: "pause" (SIGSTOP, reversible; best when the
CPU is the problem) or "quit" (SIGTERM; frees memory; best under memory
pressure or heavy swap). Pick only pids from the list.
Reply with one JSON object and nothing else:
{"action": "pause" or "quit", "pids": [pid, ...], "why": "one short sentence"}
"""

RESTART_PROMPT = """\
Firstmate, an AI agent session the owner relies on, is busy. Choose "wait" (let
its turn finish, then start a fresh session) or "force" (interrupt the turn
with Escape, then start a fresh session).
Reply with one JSON object and nothing else:
{"choice": "wait" or "force", "why": "one short sentence"}
"""


class SuggestError(RuntimeError):
    pass


@dataclass
class Advice:
    choice: str
    why: str
    pids: list[int] = field(default_factory=list)
    by: str | None = None  # the CLI that gave it; None for the rules
    fallback: str | None = None  # why the rules were used after asking a model

    @property
    def label(self) -> str:
        return f"Suggested by {self.by}" if self.by else "Suggested"

    @property
    def note(self) -> str:
        return f" Rule-based: {self.fallback}." if self.fallback else ""


class Advisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _command(self) -> tuple[list[str], str]:
        for binary, args in ((self.cfg.suggest_cli, CLAUDE_ARGS),
                             (self.cfg.suggest_fallback_cli, CODEX_ARGS)):
            path = shutil.which(binary) if binary else None
            if path:
                return [self.cfg.taskpolicy_bin, "-b", path, *args], os.path.basename(binary)
        raise SuggestError("no claude or codex CLI found")

    def ask(self, prompt: str) -> tuple[dict, str]:
        """(answer, CLI name); raises SuggestError."""
        cmd, name = self._command()
        timeout = self.cfg.suggest_timeout_seconds
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True,
                                    cwd=tempfile.gettempdir(), start_new_session=True)
        except OSError as exc:
            raise SuggestError(f"{name} could not start ({exc.__class__.__name__})")
        try:
            out, _ = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise SuggestError(f"{name} gave no answer within {timeout:.0f}s")
        if proc.returncode != 0:
            raise SuggestError(f"{name} exited {proc.returncode}")
        found = re.search(r"\{.*\}", out, re.DOTALL)
        try:
            data = json.loads(found.group(0)) if found else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise SuggestError(f"{name} gave no JSON answer")
        return data, name

    def _consult(self, prompt: str, rule: Advice, read) -> Advice:
        try:
            data, name = self.ask(prompt)
        except SuggestError as exc:
            log.info("suggestion fell back to the rules: %s", exc)
            return dataclasses.replace(rule, fallback=str(exc))
        advice = read(data)
        if advice is None:
            log.info("suggestion fell back to the rules: unusable %s answer", name)
            return dataclasses.replace(rule, fallback=f"{name} gave no usable answer")
        why = " ".join(str(data.get("why") or "").split())[:WHY_LIMIT]
        return dataclasses.replace(advice, why=T.escape(why) or rule.why, by=name)

    def shed(self, procs, load, cores: int, pressure, rule: Advice) -> Advice:
        """``procs`` are the unprotected candidates; the answer may only
        narrow them down and choose pause or quit."""
        listed = [p.pid for p in procs]
        facts = {
            "load_1_5_15_min": [round(x, 1) for x in load], "cores": cores,
            "memory_pressure": pressure.level,
            "swap_used_gb": None if pressure.swap_used_mb is None
            else round(pressure.swap_used_mb / 1024, 1),
            "ram_gb": None if pressure.mem_total_gb is None else round(pressure.mem_total_gb),
            "processes": [{"pid": p.pid, "name": p.name, "cpu_percent": round(p.cpu),
                           "memory_gb": round(p.rss_kb / 2**20, 1)} for p in procs],
        }

        def read(data: dict) -> Advice | None:
            pids = data.get("pids")
            picked = [pid for pid in listed
                      if isinstance(pids, list) and pid in pids]
            if data.get("action") not in ("pause", "quit") or not picked:
                return None
            return Advice(data["action"], "", picked)

        return self._consult(SHED_PROMPT + json.dumps(facts), rule, read)

    def restart(self, status: str, busy_seconds: float, rule: Advice) -> Advice:
        facts = {"firstmate_status": status, "busy_minutes": round(busy_seconds / 60),
                 "load_1_5_15_min": [round(x, 1) for x in os.getloadavg()],
                 "cores": os.cpu_count()}

        def read(data: dict) -> Advice | None:
            choice = data.get("choice")
            return Advice(choice, "") if choice in ("wait", "force") else None

        return self._consult(RESTART_PROMPT + json.dumps(facts), rule, read)
