"""Emergency actions and the fleet load guard, run inside the bridge.

None of this depends on Firstmate, so it works while Firstmate is stuck. The
optional model suggestion (``suggest.Advisor``) is bounded by a timeout and
falls back to the rules, so it never blocks an action either.

- ``Rescue.load_report``: what is loading the Mac (``ps``/``sysctl`` only).
- ``Rescue.offer``: lists the heaviest unprotected processes with a suggested
  action, from the captain's model CLI when it answers (``suggest.Advisor``),
  else from the rules (``quit`` under memory pressure, ``pause`` when it is
  CPU, switching to the other when nothing qualifies), and keeps that list,
  with each pid's command line, in the state file for 10 minutes under the
  thread it was posted in, so several lists can be live at once.
- ``Rescue.shed``: pauses (SIGSTOP) or quits (SIGTERM) exactly the processes
  of that list that are still the same process; a one-word accept counts only
  as a reply in the list's thread. ``Rescue.resume`` sends SIGCONT to every
  process the bridge paused.
- ``Rescue.restart_bridge``: ``launchctl kickstart -k`` of the bridge's own
  LaunchAgent, started detached so it outlives this process.
- ``LoadGuard``: as soon as the 1-minute load average crosses the threshold it
  moves heavy processes and their children to background priority
  (``taskpolicy -b``); when the 5-minute average stays above it for the
  sustain window it posts an alert with the ``shed`` list; when the load
  clears it restores them (``taskpolicy -B``).

Protected and never paused, quit or demoted: the bridge itself, the main
Firstmate pane's process tree and any Claude Remote Control process, each
with its ancestors and descendants, and macOS system processes
(``sysload.protected_pids``). When the Firstmate pane cannot be identified,
every agent session's tree is protected too.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Callable

from . import sysload as S
from .config import Config
from .state import State
from .suggest import Advice

log = logging.getLogger("slack_bridge.guard")

SHED_WAIT_SECONDS = 10.0
SHED_OFFER_SECONDS = 600.0
SHED_OFFER_LIMIT = 5
CLEAR_RATIO = 0.8  # the alarm clears below 80% of the threshold
ROOTS_TTL_SECONDS = 300.0
PROTECTED_NOTE = ("Protected and never touched: the bridge, the main Firstmate session, "
                  "Remote Control and macOS system processes.")

# roots() -> (protected root pids, whether the Firstmate pane was found)
Roots = Callable[[], "tuple[set[int], bool]"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _entry(p: S.Proc) -> dict:
    return {"pid": p.pid, "command": p.command, "label": p.name}


def _key(channel: str, thread_ts: str) -> str:
    return f"{channel} {thread_ts}"


def _name(item: dict) -> str:
    return f"{item['label']} (pid {item['pid']})"


class Rescue:
    def __init__(
        self,
        cfg: Config,
        roots: Roots,
        state: State,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
        cores: int | None = None,
        kill: Callable[[int, int], None] = os.kill,
        alive: Callable[[int], bool] = _alive,
        advisor=None,
    ):
        self.cfg = cfg
        self.advisor = advisor
        self.roots = roots
        self.state = state
        self.clock = clock
        self.sleep = sleep
        self.loadavg = loadavg
        self.cores = cores or os.cpu_count() or 1
        self.kill = kill
        self.alive = alive

    @property
    def threshold(self) -> float:
        return self.cfg.load_threshold or self.cfg.load_factor * self.cores

    def snapshot(self) -> tuple[S.Snapshot, bool]:
        """(snapshot with the protected set, firstmate_known)."""
        procs = S.read_procs(self.cfg.ps_bin)
        roots, known = self.roots()
        if not known:
            roots = roots | {p.pid for p in procs if p.is_agent}
        return S.Snapshot(procs, S.protected_pids(procs, roots | {os.getpid()})), known

    # load ----------------------------------------------------------------------

    def load_report(self) -> str:
        """Makes no Herdr call, so it answers even when Herdr is slow."""
        try:
            snap = S.Snapshot(S.read_procs(self.cfg.ps_bin))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return (f"{S.headline(self.loadavg(), self.cores)}. Process list unavailable "
                    f"({exc.__class__.__name__}).")
        return (S.format_report(snap, self.loadavg(), self.cores,
                                S.read_pressure(self.cfg.sysctl_bin), self.threshold)
                + "\nReply `shed` to list what to pause or quit.")

    # shed ----------------------------------------------------------------------

    def offer(self, channel: str, thread_ts: str, snap: S.Snapshot | None = None) -> str:
        """List what ``shed`` would act on and keep it as the offer of
        ``thread_ts``, answerable there with ``yes``."""
        text, offer = self.draft_offer(snap)
        if offer:
            self.keep_offer(channel, thread_ts, offer)
        return text

    def draft_offer(self, snap: S.Snapshot | None = None) -> tuple[str, dict | None]:
        """(text, offer) without keeping it, for a list posted later (the alarm's)."""
        if snap is None:
            try:
                snap, _ = self.snapshot()
            except (OSError, subprocess.TimeoutExpired) as exc:
                return (f"Shed unavailable: cannot list processes "
                        f"({exc.__class__.__name__})."), None
        pressure = S.read_pressure(self.cfg.sysctl_bin)
        action, why, ruled = S.rule(snap, pressure)
        candidates = {p.pid: p for p in S.offenders(snap, S.QUIT) + S.offenders(snap, S.PAUSE)}
        advice = Advice(action, why, [p.pid for p in ruled])
        if self.advisor is not None and candidates:
            advice = self.advisor.shed(list(candidates.values()), self.loadavg(), self.cores,
                                       pressure, advice)
        procs = [candidates[pid] for pid in advice.pids]
        action = advice.choice
        if not procs:
            return "Nothing to shed: no heavy process is running outside the protected set.", None
        other = S.PAUSE if action == S.QUIT else S.QUIT
        lines = ["*Heaviest processes `shed` can act on:*"]
        lines.extend(f"• {p.name} (pid {p.pid}): {p.cpu:.0f}% CPU, {S.gb(p.rss_kb)}"
                     for p in procs)
        lines.append(f"{advice.label}: `shed {action}` ({advice.why}).{advice.note} Reply "
                     f"`yes` in this thread within {round(SHED_OFFER_SECONDS / 60)} min to "
                     f"{action} these, or `shed {other}` to {other} them instead. Paused "
                     "processes continue with `resume`.")
        lines.append(PROTECTED_NOTE)
        return "\n".join(lines), {"action": action, "procs": [_entry(p) for p in procs]}

    def keep_offer(self, channel: str, thread_ts: str, offer: dict) -> None:
        with self.state.update() as st:
            live = self._unexpired(st["shed_offers"])
            live[_key(channel, thread_ts)] = {**offer, "at": int(self.clock())}
            st["shed_offers"] = dict(sorted(live.items(), key=lambda kv: kv[1]["at"])
                                     [-SHED_OFFER_LIMIT:])

    def _unexpired(self, offers: dict[str, dict]) -> dict[str, dict]:
        now = self.clock()
        return {k: v for k, v in offers.items() if now - v["at"] <= SHED_OFFER_SECONDS}

    def _live_offers(self) -> dict[str, dict]:
        return self._unexpired(self.state.read()["shed_offers"])

    def current_offer(self, channel: str, thread_ts: str) -> dict | None:
        """The unexpired offer posted in that thread."""
        return self._live_offers().get(_key(channel, thread_ts))

    def shed(self, action: str | None, channel: str, thread_ts: str,
             top_level: bool = False) -> str:
        """Act on this thread's pending offer: ``action`` None takes its
        suggestion. Only an explicit action sent top-level falls back to the
        newest offer."""
        offers = self._live_offers()
        key = _key(channel, thread_ts)
        if key not in offers and action and top_level and offers:
            key = max(offers, key=lambda k: offers[k]["at"])
        offer = offers.get(key)
        if offer is None:
            return ("No current `shed` list here, so nothing was touched.\n"
                    + self.offer(channel, thread_ts))
        action = action or offer["action"]
        try:
            snap, _ = self.snapshot()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Shed failed: cannot list processes ({exc.__class__.__name__})."
        with self.state.update() as st:
            st["shed_offers"].pop(key, None)
        current = {p.pid: p for p in snap.procs}
        lines, targets = [], []
        for item in offer["procs"]:
            cur = current.get(item["pid"])
            if cur is None:
                lines.append(f"• {_name(item)} had already exited")
            elif cur.command != item["command"]:
                lines.append(f"• left {_name(item)} alone: that pid is now another process")
            elif cur.pid in snap.protected:
                lines.append(f"• left {_name(item)} alone: it is protected")
            else:
                targets.append(item)
        lines.extend(self._pause(targets) if action == S.PAUSE else self._quit(targets))
        log.info("shed %s: targets=%d", action, len(targets))
        tail = "Reply `resume` to continue them. " if action == S.PAUSE and targets else ""
        return (f"*Shed ({action}):*\n" + "\n".join(lines)
                + f"\n{tail}Nothing outside this list was touched. {PROTECTED_NOTE}")

    def _signal(self, pid: int, sig: int) -> str | None:
        """None on success, else why it failed."""
        try:
            self.kill(pid, sig)
        except ProcessLookupError:
            return "it had already exited"
        except OSError as exc:
            return exc.__class__.__name__
        return None

    def _pause(self, targets: list[dict]) -> list[str]:
        lines, paused = [], []
        for item in targets:
            err = self._signal(item["pid"], signal.SIGSTOP)
            if err:
                lines.append(f"• could not pause {_name(item)}: {err}")
            else:
                lines.append(f"• paused {_name(item)}")
                paused.append(item)
        if paused:
            with self.state.update() as st:
                pids = {i["pid"] for i in paused}
                st["paused"] = [i for i in st["paused"] if i["pid"] not in pids] + paused
        return lines

    def _quit(self, targets: list[dict]) -> list[str]:
        lines, sent = [], []
        was_paused = {i["pid"] for i in self.state.read()["paused"]}
        for item in targets:
            err = self._signal(item["pid"], signal.SIGTERM)
            if err:
                lines.append(f"• could not quit {_name(item)}: {err}")
                continue
            if item["pid"] in was_paused:
                self._signal(item["pid"], signal.SIGCONT)  # a stopped process cannot exit
            sent.append(item)
        deadline = self.clock() + SHED_WAIT_SECONDS
        while any(self.alive(i["pid"]) for i in sent) and self.clock() < deadline:
            self.sleep(1.0)
        for item in sent:
            if self.alive(item["pid"]):
                lines.append(f"• {_name(item)} is still running "
                             f"{SHED_WAIT_SECONDS:.0f}s after SIGTERM")
            else:
                lines.append(f"• quit {_name(item)}")
        if sent:
            with self.state.update() as st:
                pids = {i["pid"] for i in sent}
                st["paused"] = [i for i in st["paused"] if i["pid"] not in pids]
        return lines

    def resume(self) -> str:
        with self.state.update() as st:
            paused, st["paused"] = st["paused"], []
        if not paused:
            return "Nothing to resume: the bridge has not paused anything."
        try:
            current = {p.pid: p for p in S.read_procs(self.cfg.ps_bin)}
        except (OSError, subprocess.TimeoutExpired):
            current = None  # SIGCONT is harmless; send it without the identity check
        lines = []
        for item in paused:
            cur = current.get(item["pid"]) if current is not None else None
            if current is not None and cur is None:
                lines.append(f"• {_name(item)} had exited")
                continue
            if cur is not None and cur.command != item["command"]:
                lines.append(f"• left {_name(item)} alone: that pid is now another process")
                continue
            err = self._signal(item["pid"], signal.SIGCONT)
            lines.append(f"• could not resume {_name(item)}: {err}" if err
                         else f"• resumed {_name(item)}")
        log.info("resume: %d", len(paused))
        return "*Resume:*\n" + "\n".join(lines)

    # restart bridge ------------------------------------------------------------

    def restart_bridge(self) -> None:
        """Ask launchd to restart this LaunchAgent; detached so it survives us."""
        if not self.cfg.launchd_label:
            raise OSError("this bridge is not running as the LaunchAgent")
        target = f"gui/{os.getuid()}/{self.cfg.launchd_label}"
        self._kickstart = subprocess.Popen(
            [self.cfg.launchctl_bin, "kickstart", "-k", target], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        log.info("restart bridge: kickstart -k %s", target)


class FirstmateRoots:
    """Protected roots: the Firstmate pane's shell, found through the keeper's
    Herdr pane lookup and cached; the bridge's own pid is always added."""

    def __init__(self, keeper, clock: Callable[[], float] = time.time):
        self.keeper = keeper
        self.clock = clock
        self._pid: int | None = None
        self._at = 0.0

    def __call__(self) -> tuple[set[int], bool]:
        if self.keeper is None:
            return set(), False
        if self._pid and self.clock() - self._at < ROOTS_TTL_SECONDS:
            return {self._pid}, True
        try:
            pane, _ = self.keeper.find_pane()
            info = self.keeper.herdr.process_info(pane.pane_id) if pane else {}
        except Exception as exc:  # HerdrError, or Herdr too slow under load
            log.info("firstmate pane lookup failed: %s", exc.__class__.__name__)
            return set(), False
        pid = info.get("shell_pid")
        if not isinstance(pid, int) or pid <= 1:
            return set(), False
        self._pid, self._at = pid, self.clock()
        return {pid}, True


class LoadGuard:
    def __init__(self, cfg: Config, rescue: Rescue, state: State, notify,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.rescue = rescue
        self.state = state
        self.notify = notify
        self.clock = clock
        self._high_since: float | None = None

    def _guard(self) -> dict:
        return self.state.read()["guard"]

    def _save(self, **changes) -> None:
        with self.state.update() as st:
            st["guard"].update(changes)

    def tick(self) -> str:
        now = self.clock()
        load = self.rescue.loadavg()
        threshold = self.rescue.threshold
        high = load[1] > threshold
        calm = load[1] < threshold * CLEAR_RATIO
        self._high_since = (self._high_since or now) if high else None
        snap = None
        try:
            snap, _ = self.rescue.snapshot()
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("load guard: process list unavailable: %s", exc.__class__.__name__)
        g = self._guard()
        clearing = g.get("active") and calm
        if snap is not None and not clearing and (load[0] > threshold or g.get("active")):
            self._demote(snap)
        if g.get("active"):
            if calm:
                return self._clear(g, load)
            alerted = not g.get("alerted") and self._alert(load, snap, now)
            return "alerted" if alerted else "active"
        if high and now - self._high_since >= self.cfg.load_sustain_seconds:
            self._save(active=True, since=int(now), alerted=False)
            log.warning("load alarm: load5=%.1f threshold=%.1f", load[1], threshold)
            return "raised" if self._alert(load, snap, now) else "raised-quiet"
        if g.get("demoted") and calm and load[0] < threshold * CLEAR_RATIO:
            restored = self._restore(g["demoted"])
            self._save(demoted=[])
            log.info("load spike passed: restored=%d", restored)
        return "high" if high else "ok"

    def _alert(self, load, snap: S.Snapshot | None, now: float) -> bool:
        """Post the overload alert unless one went out within the re-alert window."""
        g = self._guard()
        if now - g.get("alerted_at", 0) < self.cfg.load_realert_seconds:
            return False
        self._save(alerted=True, alerted_at=int(now))
        mins = round(max(self.cfg.load_sustain_seconds, now - g.get("since", now)) / 60)
        lines = [f":rotating_light: *The Mac is overloaded* ({mins} min above "
                 f"{self.rescue.threshold:.0f}): {S.headline(load, self.rescue.cores)}."]
        if snap is not None:
            lines.extend(S.top_lines(snap, 3))
        demoted = len(g.get("demoted", []))
        if demoted:
            lines.append(f"Moved {demoted} heavy process{'es' if demoted != 1 else ''} and "
                         "their children to background priority until it clears.")
        text, offer = (self.rescue.draft_offer(snap) if snap is not None
                       else ("Reply `shed` to list what to pause or quit.", None))
        lines.append(text)
        lines.append("Send `load` for details.")
        posted = self.notify("\n".join(lines))
        if offer and posted and posted[1]:
            self.rescue.keep_offer(*posted, offer)
        return True

    def _clear(self, g: dict, load) -> str:
        restored = self._restore(g.get("demoted", []))
        self._save(active=False, demoted=[])
        self._high_since = None
        log.info("load alarm cleared: load5=%.1f restored=%d", load[1], restored)
        if g.get("alerted"):
            mins = max(1, round((self.clock() - g.get("since", self.clock())) / 60))
            self.notify(f":white_check_mark: Mac load is back to normal after ~{mins} min: "
                        f"{S.headline(load, self.rescue.cores)}."
                        + (f" Restored {restored} process{'es' if restored != 1 else ''} "
                           "to normal priority." if restored else ""))
        return "cleared"

    # priority ------------------------------------------------------------------

    def _demote(self, snap: S.Snapshot) -> list[int]:
        """Heavy unprotected processes and their children, each demoted once."""
        if not self.cfg.guard_enabled:
            return []
        heavy = {p.pid for p in snap.procs if p.pid not in snap.protected and S.heavy(p)}
        targets = S.descendants(snap.procs, heavy) - snap.protected
        done = set(self._guard().get("demoted", []))
        ok = [pid for pid in sorted(targets - done) if self._taskpolicy("-b", pid)]
        if ok:
            self._save(demoted=sorted(done | set(ok)))
        return ok

    def _restore(self, pids: list[int]) -> int:
        return sum(1 for pid in pids if self._taskpolicy("-B", pid))

    def _taskpolicy(self, flag: str, pid: int) -> bool:
        try:
            rc = subprocess.run([self.cfg.taskpolicy_bin, flag, "-p", str(pid)],
                                capture_output=True, timeout=10, check=False).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = -1
        if rc != 0:
            log.info("taskpolicy %s -p %d failed (rc=%s)", flag, pid, rc)
        return rc == 0
