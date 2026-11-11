"""Find and terminate Chromium processes launched by this lab (identified by a cmdline marker)."""
from __future__ import annotations

import psutil

from .config import PROCESS_MARKER


def _marker_value(proc: psutil.Process) -> str | None:
    try:
        for arg in proc.cmdline():
            if arg.startswith(PROCESS_MARKER + "="):
                return arg.split("=", 1)[1]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return None


def find_lab_processes(marker: str | None = None) -> list[psutil.Process]:
    """All processes carrying the lab marker (optionally a specific value), plus their descendants."""
    roots: list[psutil.Process] = []
    for proc in psutil.process_iter():
        value = _marker_value(proc)
        if value is None:
            continue
        if marker is None or value == marker or value.startswith(marker + ":"):
            roots.append(proc)
    seen: dict[int, psutil.Process] = {}
    for root in roots:
        seen[root.pid] = root
        try:
            for child in root.children(recursive=True):
                seen[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    alive = []
    for p in seen.values():
        try:
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                alive.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return alive


def kill_lab_processes(marker: str | None = None, timeout: float = 3.0) -> int:
    """Terminate (then kill) matching processes. Returns number of processes signalled."""
    procs = find_lab_processes(marker)
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=timeout)
    return len(procs)


def _owner_alive(value: str) -> bool:
    owner = value.split(":", 1)[0]
    try:
        pid = int(owner)
    except ValueError:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_orphaned_lab_processes() -> int:
    """Kill lab Chromium processes whose owning server process (encoded in the marker) is gone.

    Markers have the form ``<server_pid>:<session_id>``; processes owned by a server that is
    still alive (e.g. a second lab instance) are left untouched.
    """
    owners: set[str] = set()
    for proc in psutil.process_iter():
        value = _marker_value(proc)
        if value is not None and not _owner_alive(value):
            owners.add(value.split(":", 1)[0])
    return sum(kill_lab_processes(owner) for owner in owners)


# --------------------------------------------------------------------------- PID tracking
# Chromium's helper processes (renderer, GPU, network) do not carry the command-line
# marker; they are found as *descendants* of the marked browser process. If the browser
# process dies first, helpers are re-parented (to launchd on macOS, init on Linux) and the
# descendant search can no longer see them. So every PID seen while a session runs is
# recorded together with its start time, and cleanup also kills any recorded PID that is
# still alive *and* still has the same start time (guards against PID reuse).

TrackedPid = tuple[int, float]  # (pid, create_time)


def snapshot_pids(procs: list[psutil.Process]) -> set[TrackedPid]:
    out: set[TrackedPid] = set()
    for p in procs:
        try:
            out.add((p.pid, p.create_time()))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return out


def alive_tracked(tracked: set[TrackedPid] | list[TrackedPid]) -> list[psutil.Process]:
    alive = []
    for pid, ctime in list(tracked):
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - ctime) < 0.05 and p.status() != psutil.STATUS_ZOMBIE:
                alive.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return alive


def kill_tracked(tracked: set[TrackedPid] | list[TrackedPid], timeout: float = 3.0) -> int:
    procs = alive_tracked(tracked)
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=timeout)
    return len(procs)


def describe(procs: list[psutil.Process]) -> list[dict]:
    out = []
    for p in procs:
        try:
            out.append({"pid": p.pid, "name": p.name(), "ppid": p.ppid(),
                        "rss_mb": round(p.memory_info().rss / 1024**2, 1),
                        "marker": _marker_value(p)})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return out
