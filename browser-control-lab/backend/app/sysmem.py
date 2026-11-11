"""System memory, memory pressure and swap, measured the way each OS reports them.

Why this exists: ``psutil.virtual_memory().percent`` is ``(total - available) / total``.
On macOS psutil's "available" is only free + inactive pages, so file cache, purgeable and
speculative pages count as "used" and the figure runs well above Activity Monitor's
"Memory Used" (App + Wired + Compressed). The admission guard and the dashboard therefore
use an Activity-Monitor-style figure on macOS (parsed from ``vm_stat``) and show the raw
psutil value separately.

Memory pressure:
  * macOS: ``sysctl kern.memorystatus_vm_pressure_level`` (1 normal, 2 warn, 4 critical) —
    the same signal behind Activity Monitor's pressure graph.
  * Linux: PSI ``/proc/pressure/memory`` (share of time tasks stalled on memory).
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

IS_MAC = sys.platform == "darwin"
GB = 1024**3
MB = 1024**2

PRESSURE_ORDER = {"unknown": -1, "normal": 0, "warn": 1, "critical": 2}


# --------------------------------------------------------------------------- parsers
def parse_vm_stat(text: str) -> dict[str, int]:
    """Parse ``vm_stat`` output into bytes per field (keys are lower_snake_case)."""
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    out: dict[str, int] = {"page_size": page}
    for line in text.splitlines()[1:]:
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().rstrip(".")
        if not val.isdigit():
            continue
        key = re.sub(r"[^a-z0-9]+", "_", key.strip().strip('"').lower()).strip("_")
        out[key] = int(val) * page
    return out


def mac_breakdown(vm: dict[str, int], total: int) -> dict[str, int]:
    """Activity-Monitor-style breakdown from parsed vm_stat (bytes).

    App = anonymous - purgeable, Wired = wired down, Compressed = occupied by compressor,
    Cached files = file-backed + purgeable, Used = App + Wired + Compressed.
    """
    anonymous = vm.get("anonymous_pages", 0)
    purgeable = vm.get("pages_purgeable", 0)
    app = max(0, anonymous - purgeable)
    wired = vm.get("pages_wired_down", 0)
    compressed = vm.get("pages_occupied_by_compressor", 0)
    cached = vm.get("file_backed_pages", 0) + purgeable
    used = min(total, app + wired + compressed)
    return {"app": app, "wired": wired, "compressed": compressed, "cached": cached,
            "used": used, "free": vm.get("pages_free", 0)}


def mac_pressure_level(value: int | None) -> str:
    return {1: "normal", 2: "warn", 4: "critical"}.get(value or 0, "unknown")


def parse_psi(text: str) -> dict[str, float]:
    """Parse /proc/pressure/memory -> {'some_avg10': .., 'full_avg10': ..}."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        for p in parts[1:]:
            k, _, v = p.partition("=")
            if k == "avg10":
                out[f"{kind}_avg10"] = float(v)
    return out


def psi_level(psi: dict[str, float]) -> str:
    if not psi:
        return "unknown"
    # full = every task stalled on memory; some = at least one stalled.
    if psi.get("full_avg10", 0) >= 5 or psi.get("some_avg10", 0) >= 40:
        return "critical"
    if psi.get("some_avg10", 0) >= 10:
        return "warn"
    return "normal"


# --------------------------------------------------------------------------- readers
def _run(cmd: list[str]) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def read_pressure() -> dict[str, Any]:
    if IS_MAC:
        out = _run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
        value = int(out.strip()) if out and out.strip().isdigit() else None
        return {"level": mac_pressure_level(value), "source": "kern.memorystatus_vm_pressure_level",
                "value": value}
    try:
        psi = parse_psi(Path("/proc/pressure/memory").read_text())
    except OSError:
        psi = {}
    return {"level": psi_level(psi), "source": "/proc/pressure/memory" if psi else "unavailable",
            "value": psi.get("some_avg10")}


@dataclass
class MemorySnapshot:
    total: int = 0
    used: int = 0                     # OS-appropriate "used" (Activity Monitor style on macOS)
    available: int = 0
    psutil_percent: float = 0.0       # raw psutil figure, for comparison
    breakdown: dict[str, int] = field(default_factory=dict)
    method: str = "psutil"
    pressure: dict[str, Any] = field(default_factory=lambda: {"level": "unknown"})
    swap_total: int = 0
    swap_used: int = 0
    swap_in_rate: float = 0.0         # bytes/s
    swap_out_rate: float = 0.0        # bytes/s

    @property
    def used_percent(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_gb": round(self.total / GB, 2),
            "used_gb": round(self.used / GB, 2),
            "available_gb": round(self.available / GB, 2),
            "used_percent": round(self.used_percent, 1),
            "psutil_percent": round(self.psutil_percent, 1),
            "method": self.method,
            "breakdown_gb": {k: round(v / GB, 2) for k, v in self.breakdown.items()},
            "pressure": self.pressure,
            "swap": {
                "total_gb": round(self.swap_total / GB, 2),
                "used_gb": round(self.swap_used / GB, 2),
                "percent": round(100.0 * self.swap_used / self.swap_total, 1) if self.swap_total else 0.0,
                "in_mb_s": round(self.swap_in_rate / MB, 2),
                "out_mb_s": round(self.swap_out_rate / MB, 2),
            },
        }


class MemoryReader:
    """Stateful reader (keeps previous swap counters to compute swap-in/out rates)."""

    def __init__(self) -> None:
        self._prev_swap: tuple[float, int, int] | None = None

    def read(self) -> MemorySnapshot:
        vm = psutil.virtual_memory()
        snap = MemorySnapshot(total=vm.total, psutil_percent=vm.percent)
        parsed = _run(["/usr/bin/vm_stat"]) if IS_MAC else None
        if parsed:
            b = mac_breakdown(parse_vm_stat(parsed), vm.total)
            snap.used = b.pop("used")
            snap.breakdown = b
            snap.available = vm.total - snap.used
            snap.method = "vm_stat (App+Wired+Compressed)"
        else:
            snap.used = vm.total - vm.available
            snap.available = vm.available
            snap.method = "psutil (total-available)"
            cached = getattr(vm, "cached", 0) + getattr(vm, "buffers", 0)
            snap.breakdown = {"used": snap.used, "cached": cached, "free": vm.free}
        snap.pressure = read_pressure()

        sw = psutil.swap_memory()
        snap.swap_total, snap.swap_used = sw.total, sw.used
        now = time.monotonic()
        if self._prev_swap is not None:
            t, sin, sout = self._prev_swap
            dt = max(1e-6, now - t)
            snap.swap_in_rate = max(0.0, (sw.sin - sin) / dt)
            snap.swap_out_rate = max(0.0, (sw.sout - sout) / dt)
        self._prev_swap = (now, sw.sin, sw.sout)
        return snap


# --------------------------------------------------------------------------- per-process
def process_memory(procs: list[psutil.Process]) -> dict[str, Any]:
    """Memory of a group of processes.

    * ``uss``: unique set size summed — memory returned to the OS if these processes exit.
      Closest to Activity Monitor's per-process "Memory" for Chromium (excludes shared
      libraries/frameworks).
    * ``rss``: resident set size summed — counts shared pages once *per process*, so it
      overstates the group's real cost (often 2-3x for Chromium's multi-process model).
    ``uss_unavailable`` counts processes whose USS could not be read (falls back to RSS).
    """
    uss = rss = 0
    missing = 0
    for p in procs:
        try:
            r = p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        rss += r
        try:
            uss += p.memory_full_info().uss
        except (psutil.AccessDenied, AttributeError, psutil.ZombieProcess):
            uss += r
            missing += 1
        except psutil.NoSuchProcess:
            rss -= r
    method = "uss" if missing == 0 else ("rss" if missing == len(procs) else "uss+rss")
    return {"uss_mb": round(uss / MB, 1), "rss_mb": round(rss / MB, 1),
            "method": method, "uss_unavailable": missing}
