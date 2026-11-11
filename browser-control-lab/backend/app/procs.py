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


def process_tree_memory_mb(procs: list[psutil.Process]) -> float:
    """Unique set size (memory freed if the processes exited); falls back to RSS if unavailable."""
    total = 0
    for p in procs:
        try:
            total += p.memory_full_info().uss
        except (psutil.AccessDenied, AttributeError):
            try:
                total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
    return total / (1024 * 1024)


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
