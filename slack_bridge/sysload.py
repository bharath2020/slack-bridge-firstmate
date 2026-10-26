"""What is loading the Mac, read with ``os.getloadavg``, ``sysctl`` and ``ps``.

Nothing here starts anything heavier than one ``ps`` and one ``sysctl``, so
``load`` still answers when the machine is badly overloaded. Offenders are
simply the heaviest processes by CPU or memory, whatever they are. They are
shown by executable name, never full command lines, so no argument reaches
Slack.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

PS_TIMEOUT = 15.0

AGENT_NAMES = {"claude", "codex", "pi", "opencode", "gemini", "acli", "cursor-agent"}
REMOTE_CONTROL = re.compile(r"remote[-_ ]?control", re.IGNORECASE)
SYSTEM_PREFIXES = ("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/")
SYSTEM_NAMES = {"launchd", "WindowServer", "kernel_task"}
PRESSURE = {1: "normal", 2: "warning", 4: "critical"}

# A process is heavy from this much CPU (percent of one core) or memory.
HEAVY_CPU = 10.0
HEAVY_RSS_KB = 2**20
HEAVY_SWAP_MB = 8 * 1024
PAUSE, QUIT = "pause", "quit"


@dataclass
class Proc:
    pid: int
    ppid: int
    cpu: float  # percent of one core
    rss_kb: int
    command: str

    @property
    def name(self) -> str:
        return os.path.basename(self.command.split(" ", 1)[0]) or "?"

    @property
    def is_agent(self) -> bool:
        return self.name in AGENT_NAMES


@dataclass
class Snapshot:
    procs: list[Proc] = field(default_factory=list)
    protected: set[int] = field(default_factory=set)


def parse_ps(out: str) -> list[Proc]:
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            procs.append(Proc(int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3]),
                              parts[4]))
        except ValueError:
            continue
    return procs


def read_procs(ps_bin: str = "ps") -> list[Proc]:
    proc = subprocess.run([ps_bin, "-axww", "-o", "pid=,ppid=,%cpu=,rss=,command="],
                          capture_output=True, text=True, timeout=PS_TIMEOUT, check=False)
    if proc.returncode != 0:
        raise OSError(f"ps exited {proc.returncode}")
    return parse_ps(proc.stdout)


def descendants(procs: list[Proc], roots: set[int]) -> set[int]:
    """``roots`` plus every process below them."""
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)
    seen, todo = set(), [r for r in roots if r]
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        todo.extend(children.get(pid, []))
    return seen


def ancestors(procs: list[Proc], pid: int) -> set[int]:
    parent = {p.pid: p.ppid for p in procs}
    seen = set()
    while pid and pid not in seen and pid != 1:
        seen.add(pid)
        pid = parent.get(pid, 0)
    return seen


def is_system(proc: Proc) -> bool:
    return proc.command.startswith(SYSTEM_PREFIXES) or proc.name in SYSTEM_NAMES


def protected_pids(procs: list[Proc], roots: set[int]) -> set[int]:
    """Never touched: each root (the bridge, the Firstmate pane's shell) and
    any Remote Control process, with their whole trees and their ancestors,
    plus every macOS system process."""
    rc = {p.pid for p in procs if REMOTE_CONTROL.search(p.command)}
    keep = descendants(procs, set(roots) | rc)
    for pid in set(roots) | rc:
        keep |= ancestors(procs, pid)
    return keep | {p.pid for p in procs if is_system(p)}


def heavy(proc: Proc) -> bool:
    return proc.cpu >= HEAVY_CPU or proc.rss_kb >= HEAVY_RSS_KB


def offenders(snap: Snapshot, action: str, n: int = 5) -> list[Proc]:
    """The heaviest unprotected processes: by memory for ``quit``, by CPU for
    ``pause``."""
    if action == QUIT:
        key, floor = (lambda p: p.rss_kb), HEAVY_RSS_KB
    else:
        key, floor = (lambda p: p.cpu), HEAVY_CPU
    free = [p for p in snap.procs if p.pid not in snap.protected and key(p) >= floor]
    return sorted(free, key=key, reverse=True)[:n]


@dataclass
class Pressure:
    level: str
    swap_used_mb: float | None
    mem_total_gb: float | None


def read_pressure(sysctl_bin: str = "sysctl") -> Pressure:
    try:
        proc = subprocess.run(
            [sysctl_bin, "-n", "kern.memorystatus_vm_pressure_level", "vm.swapusage",
             "hw.memsize"], capture_output=True, text=True, timeout=PS_TIMEOUT, check=False)
        lines = proc.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        lines = []
    level = PRESSURE.get(_int(lines[0]) if lines else None, "unknown")
    swap = re.search(r"used\s*=\s*([\d.]+)M", lines[1]) if len(lines) > 1 else None
    mem = _int(lines[2]) if len(lines) > 2 else None
    return Pressure(level, float(swap.group(1)) if swap else None,
                    mem / 2**30 if mem else None)


def suggest(pressure: Pressure) -> tuple[str, str]:
    """(action, why): ``quit`` when memory is the problem, else ``pause``."""
    if pressure.level in ("warning", "critical"):
        return QUIT, f"memory pressure is {pressure.level}"
    if pressure.swap_used_mb is not None and pressure.swap_used_mb >= HEAVY_SWAP_MB:
        return QUIT, f"{pressure.swap_used_mb / 1024:.1f} GB of swap is in use"
    return PAUSE, "the CPU is the bottleneck"


def rule(snap: Snapshot, pressure: Pressure) -> tuple[str, str, list[Proc]]:
    """(action, why, offenders) by ``suggest``, switching to the other action
    when no unprotected process qualifies for the suggested one."""
    action, why = suggest(pressure)
    procs = offenders(snap, action)
    if not procs:
        other = PAUSE if action == QUIT else QUIT
        procs = offenders(snap, other)
        if procs:
            why = (f"{why}, but nothing unprotected holds {gb(HEAVY_RSS_KB)} or more"
                   if action == QUIT else f"{why}, but nothing unprotected is CPU-heavy")
            action = other
    return action, why, procs


def _int(s: str) -> int | None:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None


def gb(kb: int) -> str:
    return f"{kb / 2**20:.1f} GB"


def headline(load: tuple[float, float, float], cores: int) -> str:
    return (f"load {load[0]:.1f} / {load[1]:.1f} / {load[2]:.1f} (1/5/15 min) "
            f"on {cores} cores")


def top(snap: Snapshot, key, n: int = 5) -> list[Proc]:
    return sorted(snap.procs, key=key, reverse=True)[:n]


def top_lines(snap: Snapshot, n: int = 5) -> list[str]:
    return ["*Top CPU:* " + ", ".join(
                f"{p.name} {p.cpu:.0f}%" for p in top(snap, lambda p: p.cpu, n)),
            "*Top memory:* " + ", ".join(
                f"{p.name} {gb(p.rss_kb)}" for p in top(snap, lambda p: p.rss_kb, n))]


def format_report(snap: Snapshot, load: tuple[float, float, float], cores: int,
                  pressure: Pressure, threshold: float) -> str:
    lines = [f"*Mac load:* {headline(load, cores)}; alarm above {threshold:.0f}.",
             f"*Memory:* pressure {pressure.level}"
             + (f", swap used {pressure.swap_used_mb / 1024:.1f} GB"
                if pressure.swap_used_mb is not None else "")
             + (f", {pressure.mem_total_gb:.0f} GB RAM" if pressure.mem_total_gb else "")]
    return "\n".join(lines + top_lines(snap))
