"""Keeper: keeps the main Firstmate pane alive and starts fresh sessions.

It talks to Herdr only through pane-level commands: ``pane list``,
``pane process-info``, ``pane run`` and ``pane send-keys``. It never runs
Herdr session or server lifecycle commands and never creates or closes panes.

Safety gates:
- A fresh session (``/clear``) is typed only while the pane's agent reports
  ``idle``/``done``; it waits for that and gives up after a timeout rather
  than interrupting a working turn or a permission prompt. Only a forced
  request (``restart force``) interrupts: it sends one Escape key through
  ``pane send-keys``, never kills a process, and still waits for ``idle``.
- A restart happens only when the pane reports no agent on two consecutive
  checks AND its foreground process is the pane's own login shell, and at
  most ``restart_limit`` times per window; past that it only alerts.
- ``target_pane`` pins the pane id, so tests and drills never discover the
  live Firstmate pane.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .state import State

log = logging.getLogger("slack_bridge.keeper")

IDLE_STATUSES = {"idle", "done"}
SHELLS = {"zsh", "bash", "sh", "fish", "-zsh", "-bash", "-sh"}
ALERT_INTERVAL_SECONDS = 3600

Notify = Callable[..., None]  # notify(text, channel=None, thread_ts=None)


class HerdrError(RuntimeError):
    pass


class Herdr:
    def __init__(self, binary: str, session: str, timeout: float = 20.0):
        self.binary = binary
        self.session = session
        self.timeout = timeout

    def _call(self, *args: str) -> dict:
        cmd = [self.binary, "--session", self.session, "pane", *args]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HerdrError(f"herdr pane {args[0]}: {exc.__class__.__name__}")
        if proc.returncode != 0:
            raise HerdrError(f"herdr pane {args[0]} exited {proc.returncode}")
        try:
            return json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            raise HerdrError(f"herdr pane {args[0]} returned unreadable JSON")

    def list_panes(self) -> list[dict]:
        return self._call("list").get("result", {}).get("panes", [])

    def process_info(self, pane_id: str) -> dict:
        return self._call("process-info", "--pane", pane_id).get("result", {}).get(
            "process_info", {}
        )

    def run(self, pane_id: str, command: str) -> None:
        """Type ``command`` and Enter into the pane."""
        self._call("run", pane_id, command)

    def send_keys(self, pane_id: str, key: str) -> None:
        self._call("send-keys", pane_id, key)


@dataclass
class PaneView:
    pane_id: str
    cwd: str
    label: str
    agent: str | None
    agent_status: str

    @property
    def has_agent(self) -> bool:
        return bool(self.agent)

    @property
    def idle(self) -> bool:
        return self.has_agent and self.agent_status in IDLE_STATUSES


def _same_path(a: str, b: str) -> bool:
    return os.path.realpath(a or "") == os.path.realpath(b or "")


class Keeper:
    def __init__(
        self,
        cfg: Config,
        state: State,
        herdr: Herdr,
        notify: Notify,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.state = state
        self.herdr = herdr
        self.notify = notify
        self.clock = clock
        self.sleep = sleep
        self._missing_agent_checks = 0
        self._last_alert: dict[str, float] = {}
        self._busy: tuple[str, float] | None = None  # (status, first seen)

    # Discovery -----------------------------------------------------------

    def find_pane(self) -> tuple[PaneView | None, str]:
        """Return (pane, reason). Reason explains a None."""
        panes = self.herdr.list_panes()
        if self.cfg.target_pane:
            matches = [p for p in panes if p.get("pane_id") == self.cfg.target_pane]
            what = f"pane {self.cfg.target_pane}"
        else:
            matches = [
                p
                for p in panes
                if _same_path(p.get("cwd", ""), self.cfg.fm_home)
                and "firstmate" in (p.get("label") or "").lower()
            ]
            what = "a Firstmate pane"
        if not matches:
            return None, f"no {what} found in Herdr session {self.cfg.herdr_session!r}"
        if len(matches) > 1:
            ids = ", ".join(p.get("pane_id", "?") for p in matches)
            return None, f"more than one Firstmate pane ({ids}); pin one with --pane"
        p = matches[0]
        view = PaneView(
            pane_id=p["pane_id"],
            cwd=p.get("cwd", ""),
            label=p.get("label") or "",
            agent=p.get("agent"),
            agent_status=p.get("agent_status") or "unknown",
        )
        if view.idle or not view.has_agent:
            self._busy = None
        elif self._busy is None or self._busy[0] != view.agent_status:
            self._busy = (view.agent_status, self.clock())
        return view, ""

    def busy(self) -> tuple[str, float] | None:
        """(status, seconds) for how long the last seen pane has shown the same
        busy status; None when it was idle."""
        if self._busy is None:
            return None
        status, since = self._busy
        return status, self.clock() - since

    # Fresh session -------------------------------------------------------

    def request_fresh_session(
        self, source: str, channel: str | None = None, thread_ts: str | None = None,
        force: bool = False,
    ) -> str:
        """Queue a fresh-session request for the daemon's keeper loop."""
        req = {
            "id": uuid.uuid4().hex[:12],
            "at": int(self.clock()),
            "source": source,
            "channel": channel,
            "thread_ts": thread_ts,
            "force": force,
        }
        with self.state.update() as st:
            st["fresh_requests"].append(req)
        return req["id"]

    def fresh_session(self, wait_seconds: float | None = None, force: bool = False,
                      escalate: Callable[[], bool] | None = None) -> tuple[bool, str]:
        """Wait for the Firstmate pane to be idle, then type /clear.

        Returns (ok, human-readable result). Never types into a pane whose
        agent is working or blocked; ``force``, or ``escalate()`` turning
        true while waiting, first sends it one Escape to interrupt the turn.
        """
        wait = self.cfg.fresh_wait_seconds if wait_seconds is None else wait_seconds
        deadline = self.clock() + wait
        interval = 5.0
        last_status = "unknown"
        interrupted = False
        while True:
            if not force and escalate is not None and escalate():
                force = True
            try:
                pane, reason = self.find_pane()
            except HerdrError as exc:
                return False, f"Fresh session not started: {exc}."
            if pane is None:
                return False, f"Fresh session not started: {reason}."
            if not pane.has_agent:
                return False, (
                    f"Fresh session not started: no agent is running in {pane.pane_id}; "
                    "the keeper will restart it."
                )
            if pane.idle:
                try:
                    self.herdr.run(pane.pane_id, "/clear")
                except HerdrError as exc:
                    return False, f"Fresh session failed while typing /clear: {exc}."
                log.info("fresh session: typed /clear into %s", pane.pane_id)
                return True, (
                    f"Fresh Firstmate session started in {pane.pane_id} (/clear"
                    + (", after interrupting its turn" if interrupted else "")
                    + "); it rebuilds from its records."
                )
            if force and not interrupted:
                try:
                    self.herdr.send_keys(pane.pane_id, "esc")
                except HerdrError as exc:
                    return False, f"Fresh session not started: interrupt failed: {exc}."
                interrupted = True
                deadline = self.clock() + wait
                log.info("fresh session: sent Escape to %s (%s)", pane.pane_id,
                         pane.agent_status)
            last_status = pane.agent_status
            if self.clock() >= deadline:
                if interrupted:
                    return False, (
                        f"Fresh session not started: Firstmate was still {last_status} "
                        f"{int(wait)}s after the interrupt. Send `restart force` again, or "
                        "check the Mac."
                    )
                return False, (
                    f"Fresh session not started: Firstmate stayed {last_status} for "
                    f"{int(wait)}s and a working turn is not interrupted. Send "
                    "\"new session\" again when it is idle, or `restart force` to "
                    "interrupt it."
                )
            self.sleep(max(0.5, min(interval, deadline - self.clock())))

    def process_fresh_requests(self) -> int:
        """Handle queued requests; returns how many were handled."""
        with self.state.update() as st:
            queue, st["fresh_requests"] = st["fresh_requests"], []
        if not queue:
            return 0

        def escalate() -> bool:
            """Take every request queued meanwhile once a forced one arrives."""
            with self.state.update() as st:
                if not any(r.get("force") for r in st["fresh_requests"]):
                    return False
                queue.extend(st["fresh_requests"])
                st["fresh_requests"] = []
            return True

        # Several queued requests collapse into one /clear, forced if any one
        # asked for it; answer each.
        ok, msg = self.fresh_session(force=any(r.get("force") for r in queue),
                                     escalate=escalate)
        text = ("✅ " if ok else "⚠️ ") + msg
        for req in queue:
            self.notify(text, channel=req.get("channel"), thread_ts=req.get("thread_ts"))
        log.info("fresh session requests=%d ok=%s", len(queue), ok)
        return len(queue)

    # Restart -------------------------------------------------------------

    def _restarts_in_window(self, st: dict) -> list[float]:
        cutoff = self.clock() - self.cfg.restart_window_seconds
        return [t for t in st["restarts"] if t >= cutoff]

    def _alert(self, key: str, text: str) -> None:
        now = self.clock()
        if now - self._last_alert.get(key, 0) < ALERT_INTERVAL_SECONDS:
            return
        self._last_alert[key] = now
        log.warning("keeper alert: %s", key)
        self.notify(text)

    def check(self) -> str:
        """One keeper tick. Returns a short outcome tag for logs/tests."""
        try:
            pane, reason = self.find_pane()
        except HerdrError as exc:
            self._alert("herdr", f":warning: Keeper cannot read Herdr: {exc}.")
            return "herdr-error"
        if pane is None:
            self._missing_agent_checks = 0
            self._alert("no-pane", f":warning: Keeper: {reason}. Not creating one.")
            return "no-pane"
        if pane.has_agent:
            self._missing_agent_checks = 0
            self._last_alert.pop("no-pane", None)
            return "ok"
        self._missing_agent_checks += 1
        if self._missing_agent_checks < 2:
            return "agent-missing-once"  # debounce a transient report
        return self._restart(pane)

    def _restart(self, pane: PaneView) -> str:
        try:
            info = self.herdr.process_info(pane.pane_id)
        except HerdrError as exc:
            self._alert("herdr", f":warning: Keeper cannot inspect {pane.pane_id}: {exc}.")
            return "herdr-error"
        fg = info.get("foreground_processes") or []
        fg_names = {p.get("name") or p.get("argv0") or "" for p in fg}
        at_shell = (
            info.get("foreground_process_group_id") == info.get("shell_pid")
            and bool(fg_names)
            and fg_names <= SHELLS
        )
        if not at_shell:
            self._alert(
                "busy-fg",
                f":warning: Firstmate is not running in {pane.pane_id} and the pane is "
                f"busy with {', '.join(sorted(fg_names)) or 'an unknown process'}; "
                "not restarting.",
            )
            return "fg-busy"
        with self.state.update() as st:
            recent = self._restarts_in_window(st)
            if len(recent) >= self.cfg.restart_limit:
                st["restarts"] = recent
                allowed = False
            else:
                recent.append(self.clock())
                st["restarts"] = recent
                allowed = True
        if not allowed:
            self._alert(
                "rate-limit",
                f":rotating_light: Firstmate keeps stopping in {pane.pane_id}: "
                f"{self.cfg.restart_limit} restarts in the last hour. Not restarting "
                "again; please check the Mac.",
            )
            return "rate-limited"
        command = f"cd {shlex.quote(pane.cwd or self.cfg.fm_home)} && {self.cfg.agent_cmd}"
        try:
            self.herdr.run(pane.pane_id, command)
        except HerdrError as exc:
            self._alert("restart-failed", f":warning: Keeper restart failed: {exc}.")
            return "restart-failed"
        self._missing_agent_checks = 0
        log.info("keeper: restarted agent in %s", pane.pane_id)
        self.notify(
            f":arrows_counterclockwise: Firstmate had stopped in {pane.pane_id}; "
            f"started `{self.cfg.agent_cmd}` again."
        )
        return "restarted"
