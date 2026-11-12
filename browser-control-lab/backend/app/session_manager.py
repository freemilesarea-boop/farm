"""Playwright session manager.

Each test session owns a *separate* Chromium process (own profile, own cookie jar and
storage), tagged with a command-line marker ``--blc-lab-marker=<server_pid>:<session_id>``
so its whole process tree can be found and terminated, even after a server crash.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import psutil
from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright

from .config import PROCESS_MARKER, settings
from .db import Database
from .procs import (alive_tracked, describe, find_lab_processes, kill_lab_processes, kill_tracked,
                    snapshot_pids)
from .sysmem import PRESSURE_ORDER, MemoryReader, MemorySnapshot, process_memory
from .custom_target import CustomURLError, ValidatedTarget
from .security import assert_local_url, is_local_url, redact
from .testpages import SCENARIOS

ACTIVE_STATES = {"starting", "running", "stopping"}

# Browser-level network lockdown. The per-page route guard only sees page traffic;
# these flags also stop Chromium's own background services (updates, sync, GCM, ...)
# from reaching any non-local host: no proxy, and every hostname except localhost
# resolves to NOTFOUND.
LOCAL_ONLY_BROWSER_ARGS = [
    "--no-proxy-server",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-extensions",
    "--no-first-run",
    "--no-pings",
]
MAX_CONSECUTIVE_FAILURES = 5
VIDEO_POLL_S = 1.0
VIDEO_FIND_TIMEOUT_MS = 10_000

# --- injected into custom Video Playback pages (only plain HTML5 <video> elements) ---
VIDEO_INSTALL_JS = """(opts) => {
  const v = document.querySelector('video');
  if (!v) return {present: false};
  if (!window.__blc) {
    const b = window.__blc = {repeats: 0, errors: [], loop: !!opts.loop};
    v.addEventListener('ended', () => {
      b.repeats++;
      if (b.loop) { v.currentTime = 0; v.play().catch(e => b.errors.push('replay failed: ' + e.name + ': ' + e.message)); }
    });
    v.addEventListener('error', () => b.errors.push(
      'media error ' + (v.error ? v.error.code + ' (' + ['', 'ABORTED', 'NETWORK', 'DECODE', 'SRC_NOT_SUPPORTED'][v.error.code] + ') ' + (v.error.message || '') : '')));
  }
  window.__blc.loop = !!opts.loop;
  v.loop = false;  // repeats are driven by the 'ended' handler so they can be counted
  v.muted = !!opts.muted;
  return v.play().then(() => ({present: true, started: true}))
                 .catch(e => ({present: true, started: false,
                               error: e.name + ': ' + e.message + (v.error ? ' (media error ' + v.error.code + ')' : '')}));
}"""
VIDEO_STATUS_JS = """() => {
  const v = document.querySelector('video');
  if (!v) return {present: false};
  const b = window.__blc;
  const state = v.error ? 'error' : v.ended ? 'ended' : (!v.paused ? 'playing' : 'paused');
  return {present: true, installed: !!b, state, paused: v.paused, ended: v.ended,
          current_time: v.currentTime, duration: isFinite(v.duration) ? v.duration : null,
          ready_state: v.readyState, network_state: v.networkState,
          error: v.error ? {code: v.error.code, message: v.error.message || ''} : null,
          current_src: v.currentSrc || v.src || null, video_count: document.querySelectorAll('video').length,
          repeats: b ? b.repeats : 0, errors: b ? b.errors.splice(0) : [], muted: v.muted};
}"""
READY_STATES = ["HAVE_NOTHING", "HAVE_METADATA", "HAVE_CURRENT_DATA", "HAVE_FUTURE_DATA", "HAVE_ENOUGH_DATA"]
NETWORK_STATES = ["NETWORK_EMPTY", "NETWORK_IDLE", "NETWORK_LOADING", "NETWORK_NO_SOURCE"]
MEDIA_ERRORS = {1: "MEDIA_ERR_ABORTED", 2: "MEDIA_ERR_NETWORK", 3: "MEDIA_ERR_DECODE", 4: "MEDIA_ERR_SRC_NOT_SUPPORTED"}
CODEC_HINT = (" — 이 브라우저가 해당 코덱/컨테이너를 재생하지 못합니다. WebM(VP9/AV1) 또는 MP4(H.264/VP9/AV1)로 "
              "인코딩된 파일인지 확인하세요.")


def video_view(st: dict, loop: bool) -> dict:
    """Shape the in-page status for the API/dashboard (named readyState/networkState/error)."""
    if not st.get("present"):
        return {"present": False, "state": "none"}
    err = st.get("error")
    rs, ns = st.get("ready_state"), st.get("network_state")
    return {
        "present": True, "state": st["state"], "paused": st.get("paused"), "ended": st.get("ended"),
        "current_time": round(st["current_time"], 2) if st.get("current_time") is not None else None,
        "duration": round(st["duration"], 2) if st.get("duration") else None,
        "ready_state": rs, "ready_state_name": READY_STATES[rs] if isinstance(rs, int) and 0 <= rs < 5 else None,
        "network_state": ns,
        "network_state_name": NETWORK_STATES[ns] if isinstance(ns, int) and 0 <= ns < 4 else None,
        "error": ({"code": err["code"], "name": MEDIA_ERRORS.get(err["code"]), "message": err.get("message")}
                  if err else None),
        "current_src": st.get("current_src"), "video_count": st.get("video_count"),
        "repeats": st.get("repeats", 0), "muted": st.get("muted"), "loop": loop,
    }


class LimitError(RuntimeError):
    """Raised when a request would exceed session / resource limits."""


@dataclass
class TestSession:
    id: str
    name: str
    scenario: str
    created_at: float
    status: str = "idle"
    started_at: float | None = None
    stopped_at: float | None = None
    iterations: int = 0
    errors: int = 0
    consecutive_failures: int = 0
    last_load_ms: float | None = None
    last_ttfb_ms: float | None = None
    total_load_ms: float = 0.0
    ok_iterations: int = 0
    isolation_ok: bool | None = None
    blocked_requests: int = 0
    last_error: str | None = None
    memory_mb: float | None = None        # USS sum of the session's process tree
    memory_rss_mb: float | None = None    # RSS sum (overstates: shared pages counted per process)
    memory_method: str | None = None
    process_count: int = 0
    tracked: set = field(default_factory=set, repr=False)  # {(pid, create_time)} ever seen
    run_id: int | None = None
    crashed: bool = False
    # ---- custom URL scenario
    custom: dict | None = None            # {url, mode, reload_interval_s, muted, loop}
    target: ValidatedTarget | None = field(default=None, repr=False)
    current_url: str | None = None
    page_state: str = "idle"              # idle | loading | loaded | error
    page_load_count: int = 0
    video: dict = field(default_factory=dict)
    last_blocked_url: str | None = None
    # runtime handles (not serialised)
    task: asyncio.Task | None = field(default=None, repr=False)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    browser: Browser | None = field(default=None, repr=False)

    @property
    def marker(self) -> str:
        return f"{os.getpid()}:{self.id}"

    @property
    def target_url(self) -> str:
        """URL the browser navigates to. Direct video files are wrapped in the lab's player page."""
        if self.custom:
            nav = self.custom.get("nav_url") or self.custom["url"]
            if self.custom.get("kind") == "video_file" and self.custom["mode"] == "video":
                from urllib.parse import quote
                return f"http://127.0.0.1:{settings.port}/lab/player?src={quote(nav, safe='')}"
            return nav
        return f"http://127.0.0.1:{settings.port}{SCENARIOS[self.scenario]['path']}"

    @property
    def avg_load_ms(self) -> float | None:
        return round(self.total_load_ms / self.ok_iterations, 1) if self.ok_iterations else None

    def uptime_s(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.stopped_at if self.status not in ACTIVE_STATES else time.time()
        return max(0.0, (end or time.time()) - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "scenario": self.scenario,
            "target_url": self.target_url,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "uptime_s": round(self.uptime_s(), 1),
            "iterations": self.iterations,
            "errors": self.errors,
            "last_load_ms": self.last_load_ms,
            "last_ttfb_ms": self.last_ttfb_ms,
            "avg_load_ms": self.avg_load_ms,
            "isolation_ok": self.isolation_ok,
            "blocked_requests": self.blocked_requests,
            "last_error": self.last_error,
            "custom": self.custom,
            "current_url": self.current_url,
            "page_state": self.page_state,
            "page_load_count": self.page_load_count,
            "video": self.video or None,
            "memory_mb": self.memory_mb,
            "memory_rss_mb": self.memory_rss_mb,
            "memory_method": self.memory_method,
            "process_count": self.process_count,
        }


class SystemMonitor:
    """Samples CPU / memory / pressure / swap in the background so guards never block."""

    def __init__(self) -> None:
        self.cpu_percent = 0.0
        self.cpu_count = psutil.cpu_count(logical=True) or 1
        # CPU is a moving average of the last 3 one-second samples, so a single
        # spike (e.g. one Chromium launching) does not flip the admission guard.
        self._cpu_samples: deque[float] = deque(maxlen=3)
        psutil.cpu_percent(interval=None)  # prime; first real sample comes 1 s later
        self._mem_reader = MemoryReader()
        self.memory: MemorySnapshot = self._mem_reader.read()

    @property
    def ram_percent(self) -> float:
        """RAM figure used by the guard (Activity-Monitor style on macOS)."""
        return self.memory.used_percent

    @property
    def pressure_level(self) -> str:
        return self.memory.pressure.get("level", "unknown")

    def refresh_memory(self) -> None:
        self.memory = self._mem_reader.read()

    def sample(self) -> None:
        self._cpu_samples.append(psutil.cpu_percent(interval=None))
        self.cpu_percent = sum(self._cpu_samples) / len(self._cpu_samples)
        self.refresh_memory()

    def to_dict(self) -> dict[str, Any]:
        m = self.memory
        return {
            "cpu_percent": round(self.cpu_percent, 1),
            "cpu_count": self.cpu_count,
            # kept for compatibility: the guard's RAM figure
            "ram_percent": round(m.used_percent, 1),
            "ram_used_gb": round(m.used / 1024**3, 2),
            "ram_total_gb": round(m.total / 1024**3, 2),
            "memory": m.to_dict(),
        }


class SessionManager:
    def __init__(self, db: Database):
        self.db = db
        self.sessions: dict[str, TestSession] = {}
        self.monitor = SystemMonitor()
        self._pw_cm = None
        self._pw: Playwright | None = None
        self._pw_lock = asyncio.Lock()
        self._launch_sem = asyncio.Semaphore(3)  # avoid launching many Chromiums at once
        self._sampler: asyncio.Task | None = None
        self._lock = asyncio.Lock()  # guards admission decisions
        self._verified_origins: dict[str, float] = {}
        self._probe_sem = asyncio.Semaphore(1)

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        recovered = self.db.mark_unclean_sessions()
        from .procs import kill_orphaned_lab_processes

        killed = kill_orphaned_lab_processes()
        killed += self._reap_tracked_from_dead_servers()
        if recovered or killed:
            self.db.log("warning", "Recovered from unclean shutdown",
                        data={"sessions_reset": recovered, "orphan_processes_killed": killed})
        for row in self.db.list_sessions():
            if row["scenario"] not in SCENARIOS and not (row["scenario"] == "custom" and row["config"]):
                continue
            self.sessions[row["id"]] = TestSession(
                id=row["id"], name=row["name"], scenario=row["scenario"], created_at=row["created_at"],
                status="idle" if row["last_status"] in ACTIVE_STATES else row["last_status"],
                custom=row["config"] if row["scenario"] == "custom" else None,
            )
        pol = settings.custom_policy
        for problem in pol.config_problems:
            self.db.log("warning", f"Custom URL allowlist entry ignored: {problem}")
        self._sampler = asyncio.create_task(self._sample_loop())
        self.db.log("info", "Server started",
                    data={"sessions_loaded": len(self.sessions), "max_concurrent": settings.max_concurrent})

    async def shutdown(self) -> None:
        await self.stop_all(reason="server_shutdown")
        if self._sampler:
            self._sampler.cancel()
        if self._pw_cm is not None:
            try:
                await self._pw_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._pw_cm = self._pw = None
        leftover = kill_lab_processes(str(os.getpid()))
        self.db.log("info", "Server stopped", data={"leftover_processes_killed": leftover})

    def _reap_tracked_from_dead_servers(self) -> int:
        killed = 0
        for server_pid, pids in self.db.tracked_by_server().items():
            if server_pid == os.getpid():
                continue
            try:
                alive = psutil.pid_exists(server_pid) and psutil.Process(server_pid).status() != psutil.STATUS_ZOMBIE
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                alive = False
            if alive:
                continue  # another running lab instance owns these
            killed += kill_tracked(pids)
            self.db.untrack_server(server_pid)
        return killed

    async def _playwright(self) -> Playwright:
        async with self._pw_lock:
            if self._pw is None:
                self._pw_cm = async_playwright()
                self._pw = await self._pw_cm.__aenter__()
            return self._pw

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.monitor.sample()
                for s in list(self.sessions.values()):
                    if s.status in ACTIVE_STATES:
                        await asyncio.to_thread(self._sample_session, s)
                    else:
                        s.process_count = 0
                        s.memory_mb = s.memory_rss_mb = s.memory_method = None
            except Exception as exc:  # never let the sampler die
                self.db.log("error", f"Monitor sampling failed: {exc}")

    def _sample_session(self, s: TestSession) -> None:
        procs = find_lab_processes(s.marker)
        new = snapshot_pids(procs) - s.tracked
        if new and s.status in ACTIVE_STATES:
            s.tracked |= new
            self.db.track_processes(s.id, os.getpid(), new)
        # include tracked processes that were re-parented away from the marked browser
        seen = {p.pid for p in procs}
        procs += [p for p in alive_tracked(s.tracked) if p.pid not in seen]
        mem = process_memory(procs)
        s.process_count = len(procs)
        s.memory_mb, s.memory_rss_mb, s.memory_method = mem["uss_mb"], mem["rss_mb"], mem["method"]

    # ------------------------------------------------------------------ registry
    def create_sessions(self, count: int, scenario: str, name_prefix: str | None = None,
                        custom: dict | None = None) -> list[TestSession]:
        """``custom`` must already be validated (see main.create_sessions); it is re-validated at start."""
        if scenario == "custom":
            if not custom or not custom.get("url"):
                raise ValueError("custom scenario requires a URL")
        elif scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario {scenario!r}")
        else:
            custom = None
        if count < 1:
            raise ValueError("count must be >= 1")
        if len(self.sessions) + count > settings.max_sessions:
            raise LimitError(
                f"Session limit reached: {len(self.sessions)} registered, max {settings.max_sessions}"
            )
        created = []
        for _ in range(count):
            sid = uuid.uuid4().hex[:10]
            n = len(self.sessions) + 1
            s = TestSession(id=sid, name=f"{name_prefix or 'session'}-{n:03d}", scenario=scenario,
                            created_at=time.time(), custom=dict(custom) if custom else None)
            self.sessions[sid] = s
            self.db.insert_session(s.id, s.name, s.scenario, s.created_at, s.custom)
            created.append(s)
        self.db.log("info", f"Registered {count} session(s)",
                    data={"scenario": scenario, **({"url": custom["url"], "mode": custom["mode"]} if custom else {})})
        return created

    def get(self, sid: str) -> TestSession:
        try:
            return self.sessions[sid]
        except KeyError:
            raise KeyError(f"Session {sid} not found") from None

    async def delete_session(self, sid: str) -> None:
        s = self.get(sid)
        if s.status in ACTIVE_STATES:
            await self.stop(sid, reason="deleted")
        del self.sessions[sid]
        self.db.delete_session(sid)
        self.db.log("info", f"Deleted session {s.name}", session_id=sid)

    # ------------------------------------------------------------------ admission control
    def active_count(self) -> int:
        return sum(1 for s in self.sessions.values() if s.status in ("starting", "running"))

    def admission_check(self) -> str | None:
        """Return a reason string if a new browser may NOT be started right now."""
        if self.active_count() >= settings.max_concurrent:
            return f"concurrency limit reached ({settings.max_concurrent})"
        # CPU comes from the 1 s background sampler; memory is read fresh.
        self.monitor.refresh_memory()
        return self._resource_block_reason()

    def _resource_block_reason(self) -> str | None:
        if self.monitor.cpu_percent >= settings.cpu_threshold:
            return f"CPU {self.monitor.cpu_percent:.0f}% >= threshold {settings.cpu_threshold:.0f}%"
        if self.monitor.ram_percent >= settings.ram_threshold:
            return f"RAM {self.monitor.ram_percent:.0f}% >= threshold {settings.ram_threshold:.0f}%"
        block = PRESSURE_ORDER.get(settings.pressure_block_level)
        level = self.monitor.pressure_level
        if block is not None and block >= 0 and PRESSURE_ORDER.get(level, -1) >= block:
            return f"memory pressure '{level}' (blocking at '{settings.pressure_block_level}')"
        return None

    # ------------------------------------------------------------------ control
    async def start(self, sid: str) -> TestSession:
        s = self.get(sid)
        async with self._lock:
            if s.status in ACTIVE_STATES:
                return s
            reason = self.admission_check()
            if reason:
                self.db.log("warning", f"Start blocked: {reason}", session_id=sid)
                raise LimitError(reason)
            if s.custom:
                # Re-validate at every start (allowlist may have changed; DNS re-checked).
                s.target = settings.custom_policy.validate(s.custom["url"])
            else:
                assert_local_url(s.target_url)
            self._reset_metrics(s)
            s.status = "starting"
            s.started_at = time.time()
            s.stopped_at = None
            s.stop_event = asyncio.Event()
            s.run_id = self.db.start_run(s.id, s.started_at)
            self.db.set_session_status(s.id, s.status)
            s.task = asyncio.create_task(self._run(s), name=f"session-{s.id}")
        self.db.log("info", f"Starting {s.name}", session_id=sid, data={"target": s.target_url})
        return s

    async def stop(self, sid: str, reason: str = "user_stop") -> TestSession:
        s = self.get(sid)
        if s.status not in ACTIVE_STATES or s.task is None:
            return s
        s.status = "stopping"
        s.stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(s.task), timeout=20)
        except asyncio.TimeoutError:
            s.task.cancel()
            self.db.log("warning", "Session did not stop in time; cancelled", session_id=sid)
        except Exception:
            pass
        if reason != "user_stop":
            self.db.log("info", f"Stopped {s.name} ({reason})", session_id=sid)
        return s

    async def restart(self, sid: str) -> TestSession:
        await self.stop(sid, reason="restart")
        return await self.start(sid)

    async def start_all(self, wave_size: int = 3) -> dict[str, Any]:
        """Start idle sessions in small waves. After each wave we wait until its browsers are up
        and take a fresh CPU/RAM reading, so the resource guard sees the real load before
        admitting more (otherwise a bulk start would admit everything before CPU rises)."""
        started, skipped = [], []
        pending = [s for s in self.sessions.values() if s.status not in ACTIVE_STATES]
        blocked_reason: str | None = None
        for i in range(0, len(pending), wave_size):
            wave = []
            for s in pending[i:i + wave_size]:
                if blocked_reason is None:
                    try:
                        await self.start(s.id)
                        started.append(s.id)
                        wave.append(s)
                        continue
                    except LimitError as exc:
                        blocked_reason = str(exc)
                skipped.append({"id": s.id, "reason": blocked_reason})
            if blocked_reason is not None:
                continue
            deadline = time.time() + 30
            while time.time() < deadline and any(s.status == "starting" for s in wave):
                await asyncio.sleep(0.2)
            if i + wave_size < len(pending):
                await asyncio.sleep(1.0)
                self.monitor.sample()  # fresh reading including the wave just launched
        if skipped:
            self.db.log("warning", f"Start-all: {len(started)} started, {len(skipped)} blocked",
                        data={"first_reason": skipped[0]["reason"]})
        else:
            self.db.log("info", f"Start-all: {len(started)} started")
        return {"started": started, "skipped": skipped}

    async def stop_all(self, reason: str = "stop_all") -> dict[str, Any]:
        targets = [s.id for s in self.sessions.values() if s.status in ACTIVE_STATES]
        await asyncio.gather(*(self.stop(sid, reason=reason) for sid in targets), return_exceptions=True)
        leftover = await asyncio.to_thread(kill_lab_processes, str(os.getpid()))
        leftover += await asyncio.to_thread(kill_tracked, self._all_tracked())
        # Verify: nothing with our marker and no tracked PID may still be alive.
        remaining = await asyncio.to_thread(self.lab_processes)
        verified = not remaining
        self.db.log("info" if verified else "error",
                    f"Stop-all ({reason}): {len(targets)} session(s) stopped, "
                    f"{'0 browser processes remain' if verified else f'{len(remaining)} processes REMAIN'}",
                    data={"leftover_processes_killed": leftover, "remaining": len(remaining)})
        return {"stopped": targets, "leftover_processes_killed": leftover,
                "remaining_processes": len(remaining), "verified_clean": verified}

    def _all_tracked(self) -> set:
        out: set = set()
        for s in self.sessions.values():
            out |= s.tracked
        return out

    def lab_processes(self) -> list[psutil.Process]:
        """Every live process belonging to this server: marker search + tracked PIDs."""
        procs = find_lab_processes(str(os.getpid()))
        seen = {p.pid for p in procs}
        procs += [p for p in alive_tracked(self._all_tracked()) if p.pid not in seen]
        return procs

    def process_report(self) -> dict[str, Any]:
        procs = self.lab_processes()
        return {"count": len(procs), "memory": process_memory(procs), "processes": describe(procs),
                "tracked_pids": len(self._all_tracked())}

    # ------------------------------------------------------------------ session worker
    @staticmethod
    def _reset_metrics(s: TestSession) -> None:
        s.iterations = s.errors = s.consecutive_failures = s.ok_iterations = s.blocked_requests = 0
        s.total_load_ms = 0.0
        s.last_load_ms = s.last_ttfb_ms = None
        s.isolation_ok = None
        s.last_error = None
        s.crashed = False
        s.current_url = None
        s.page_state = "idle"
        s.page_load_count = 0
        s.video = {}
        s.last_blocked_url = None

    def _record_error(self, s: TestSession, message: str) -> None:
        s.errors += 1
        s.consecutive_failures += 1
        s.last_error = redact(message)[:500]
        self.db.log("error", s.last_error, session_id=s.id, data={"iteration": s.iterations})

    async def _run(self, s: TestSession) -> None:
        end_reason = "stopped"
        context: BrowserContext | None = None
        try:
            pw = await self._playwright()
            async with self._launch_sem:
                if s.stop_event.is_set():
                    return
                s.browser = await pw.chromium.launch(
                    headless=settings.headless,
                    executable_path=settings.chromium_executable,
                    args=[f"{PROCESS_MARKER}={s.marker}", *self._browser_args(s)],
                )
            s.browser.on("disconnected", lambda _b: self._on_disconnect(s))
            context = await s.browser.new_context(viewport={"width": 1280, "height": 800})
            await context.route("**/*", lambda route: self._guard_route(s, route))
            if s.custom:
                context.on("requestfailed", lambda req: self._on_request_failed(s, req))
            page = await context.new_page()
            page.set_default_timeout(settings.page_timeout_ms)
            if not s.stop_event.is_set():
                s.status = "running"
                self.db.set_session_status(s.id, s.status)
            self.db.log("info", f"{s.name} running (browser {s.browser.version})", session_id=s.id)

            if s.custom and not await self._verify_ownership(s, page):
                end_reason = "error"
                s.stop_event.set()

            while not s.stop_event.is_set():
                if s.custom and s.custom["mode"] == "video":
                    await self._video_cycle(s, page)
                else:
                    await self._iteration(s, page)
                if s.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    end_reason = "error"
                    self.db.log("error", f"{s.name}: {MAX_CONSECUTIVE_FAILURES} consecutive failures, stopping",
                                session_id=s.id)
                    break
                if s.browser is None or not s.browser.is_connected():
                    end_reason = "error"
                    break
                interval = (float(s.custom["reload_interval_s"]) if s.custom else settings.iteration_interval)
                try:
                    await asyncio.wait_for(s.stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
            if s.crashed:
                end_reason = "error"
        except asyncio.CancelledError:
            end_reason = "cancelled"
            raise
        except Exception as exc:
            end_reason = "error"
            self._record_error(s, f"Session crashed: {type(exc).__name__}: {exc}")
        finally:
            await self._cleanup(s, context)
            s.stopped_at = time.time()
            s.status = "error" if end_reason == "error" else "stopped"
            self.db.set_session_status(s.id, s.status)
            if s.run_id is not None:
                self.db.end_run(s.run_id, end_reason, s.iterations, s.errors, s.avg_load_ms)
            s.task = None

    def _on_disconnect(self, s: TestSession) -> None:
        if s.status == "running" and not s.stop_event.is_set():
            s.crashed = True
            self._record_error(s, "Browser disconnected unexpectedly")
            s.stop_event.set()

    @staticmethod
    def _browser_args(s: TestSession) -> list[str]:
        return SessionManager._browser_args_for(s.custom)

    @staticmethod
    def _browser_args_for(custom: dict | None, pol=None) -> list[str]:
        if not custom:
            return list(LOCAL_ONLY_BROWSER_ARGS)
        # Same lockdown, but allow-listed hosts are pinned to their validated IPs.
        pol = pol or settings.custom_policy
        rules = pol.resolver_rules()
        args = [a if not a.startswith("--host-resolver-rules=") else f"--host-resolver-rules={rules}"
                for a in LOCAL_ONLY_BROWSER_ARGS if a != "--no-proxy-server"]
        args += pol.proxy_args()
        if custom["mode"] == "video":
            args.append("--autoplay-policy=no-user-gesture-required")
        return args

    _NET_BLOCK_ERRORS = ("ERR_NAME_NOT_RESOLVED", "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
                         "ERR_BLOCKED_BY_CLIENT")

    def _on_request_failed(self, s: TestSession, req) -> None:
        """Requests stopped by the network-level policy (redirect hops, IP literals) never reach the
        route guard, so log them here with their URL."""
        err = req.failure or ""
        if any(e in err for e in self._NET_BLOCK_ERRORS) and not settings.custom_policy.host_allowed(
                req.url, navigation=False):
            s.blocked_requests += 1
            s.last_blocked_url = req.url[:200]
            self.db.log("warning", f"Blocked by network policy ({err}): {req.url[:200]}", session_id=s.id)

    async def _guard_route(self, s: TestSession, route: Route) -> None:
        req = route.request
        url = req.url
        if s.custom:
            try:
                main_nav = req.is_navigation_request() and req.frame.parent_frame is None
            except Exception:
                main_nav = False
            ok = settings.custom_policy.host_allowed(url, navigation=main_nav)
        else:
            ok = is_local_url(url)
        if ok:
            await route.continue_()
        else:
            s.blocked_requests += 1
            kind = "redirect/navigation" if req.is_navigation_request() else "request"
            self.db.log("warning", f"Blocked {kind} to non-allowed host: {url[:200]}", session_id=s.id)
            await route.abort("blockedbyclient")

    async def _iteration(self, s: TestSession, page: Page) -> bool:
        """One page load. Returns True on success."""
        s.iterations += 1
        s.page_state = "loading"
        t0 = time.perf_counter()
        try:
            resp = await page.goto(s.target_url, wait_until="load")
            elapsed = (time.perf_counter() - t0) * 1000
            s.current_url = page.url
            status = resp.status if resp else 0
            if s.custom and not self._final_url_ok(s, page, resp):
                return False
            if status >= 400 or status == 0:
                s.page_state = "error"
                self._record_error(s, f"HTTP {status} from {s.target_url}")
                s.last_load_ms = round(elapsed, 1)
                return False
            timing = await page.evaluate(
                """() => { const n = performance.getEntriesByType('navigation')[0];
                           return n ? {ttfb: n.responseStart - n.requestStart, load: n.loadEventEnd - n.startTime} : null; }"""
            )
            s.last_load_ms = round(elapsed, 1)
            s.last_ttfb_ms = round(timing["ttfb"], 1) if timing else None
            s.total_load_ms += elapsed
            s.ok_iterations += 1
            s.page_load_count += 1
            s.page_state = "loaded"
            if s.custom:
                await self._check_storage_isolation(s, page)
            else:
                await self._check_isolation(s, page)
            if not (s.custom and s.custom["mode"] == "video"):
                s.consecutive_failures = 0  # video mode resets only once playback runs
            return True
        except Exception as exc:
            if s.stop_event.is_set():
                return False  # navigation interrupted by shutdown; not a test failure
            s.page_state = "error"
            msg = str(exc).splitlines()[0] if str(exc) else ""
            if s.custom and any(e in msg for e in self._NET_BLOCK_ERRORS):
                blocked = f" ({s.last_blocked_url})" if s.last_blocked_url else ""
                msg = (f"Blocked: the page or a redirect tried to reach a host that is not allowed{blocked}. "
                       + msg)
            self._record_error(s, f"{type(exc).__name__}: {msg}")
            return False

    def _final_url_ok(self, s: TestSession, page: Page, resp) -> bool:
        """Defence in depth: re-check every redirect hop and the final URL against the policy."""
        pol = settings.custom_policy
        hops = []
        req = resp.request if resp else None
        while req is not None:
            hops.append(req.url)
            req = req.redirected_from
        for u in [page.url, *hops]:
            if not pol.host_allowed(u, navigation=True):
                s.page_state = "error"
                self._record_error(s, f"Redirect left the allowlist: {u[:200]} (navigation refused)")
                return False
        return True

    async def _verify_ownership(self, s: TestSession, page: Page) -> bool:
        """Non-local custom targets must serve this lab's token at /.well-known/blc-lab-verify.txt."""
        tgt = s.target
        if tgt is None or tgt.local or not settings.custom_policy.require_verification:
            return True
        why = await self._ownership_problem(page, tgt)
        if why:
            s.page_state = "error"
            self._record_error(s, f"Ownership verification failed for {tgt.origin}: {why}. Serve the token shown "
                                  f"in the dashboard at {tgt.verify_url} (or test on localhost).")
            return False
        self.db.log("info", f"Ownership verified for {tgt.origin}", session_id=s.id)
        return True

    async def _ownership_problem(self, page: Page, tgt: ValidatedTarget, *, force: bool = False) -> str | None:
        """None if ``tgt`` is local, verification is off, cached, or the token file checks out.
        ``force`` (dashboard registration) always fetches the file, ignoring the setting and the cache."""
        if tgt.local or not (force or settings.custom_policy.require_verification):
            return None
        cached = self._verified_origins.get(tgt.origin)
        if cached and not force and time.time() - cached < 3600:
            return None
        token = settings.verification_token
        why = None
        try:
            resp = await page.goto(tgt.verify_url, wait_until="load")
            if resp is None or resp.status != 200:
                why = f"HTTP {resp.status if resp else 0}"
            elif resp.request.redirected_from is not None or page.url != tgt.verify_url:
                why = "the verification URL redirected (not allowed)"
            elif token not in (await resp.text())[:4096]:
                why = "the file does not contain this lab's token"
        except Exception as exc:
            why = (str(exc).splitlines() or [type(exc).__name__])[0]
        if why is None:
            self._verified_origins[tgt.origin] = time.time()
        return why

    # ------------------------------------------------------------------ URL probe ("URL 확인")
    async def register(self, url: str) -> dict[str, Any]:
        """"내 서버 등록": allow the host of ``url`` from the dashboard, but only after it serves this
        lab's ownership token. All other rules (platform block, SSRF/public-IP, redirects) still
        apply. On success the host is persisted and the probe result is returned."""
        from .custom_target import add_registered_host
        pol = settings.custom_policy
        host = pol.unlisted_host(url)
        if host is None:
            pol.validate(url)  # raises the precise reason (blocked platform, bad URL, ...)
            out = await self.probe(url)  # already allowed -> plain check
            out["registered"] = False
            out["already_allowed"] = True
            return out
        out = await self.probe(url, policy=pol.with_host(host), force_verify=True)
        out["registered"] = bool(out.get("verified")) and out.get("allowed", False)
        if out["registered"]:
            add_registered_host(settings.registered_hosts_path, host)
            self.db.log("info", f"Host registered from dashboard after ownership check: {host}")
        return out

    async def probe(self, url: str, *, policy=None, force_verify: bool = False) -> dict[str, Any]:
        """Open ``url`` once in a short-lived Chromium under the custom-URL network policy and
        report what is there. Policy errors raise CustomURLError; resource limits raise LimitError."""
        pol = policy or settings.custom_policy
        tgt = pol.validate(url)
        reason = self._resource_block_reason()
        if reason:
            raise LimitError(reason)
        out: dict[str, Any] = {
            "input_url": tgt.input_url, "normalized_url": tgt.url, "host": tgt.host, "local": tgt.local,
            "resolved_ips": tgt.ips, "media_kind": tgt.media_kind, "allowed": True, "blocked_reason": None,
            "verification_required": (not tgt.local) and (force_verify or pol.require_verification),
            "verified": None,
            "final_url": None, "redirects": [], "http_status": None, "content_type": None,
            "is_direct_video": False, "is_html": False, "video_found": None, "video": None,
        }
        probe_id = f"probe-{uuid.uuid4().hex[:8]}"
        marker = f"{os.getpid()}:{probe_id}"
        blocked: list[str] = []
        browser = None
        async with self._probe_sem:
            try:
                pw = await self._playwright()
                fake = {"mode": "video"}
                browser = await pw.chromium.launch(
                    headless=settings.headless, executable_path=settings.chromium_executable,
                    args=[f"{PROCESS_MARKER}={marker}", *self._browser_args_for(fake, pol)])
                context = await browser.new_context(viewport={"width": 1280, "height": 800})

                async def guard(route):
                    req = route.request
                    try:
                        nav = req.is_navigation_request() and req.frame.parent_frame is None
                    except Exception:
                        nav = False
                    if pol.host_allowed(req.url, navigation=nav):
                        await route.continue_()
                    else:
                        blocked.append(req.url[:300])
                        await route.abort("blockedbyclient")
                await context.route("**/*", guard)
                context.on("requestfailed", lambda req: blocked.append(req.url[:300])
                           if any(e in (req.failure or "") for e in self._NET_BLOCK_ERRORS)
                           and not pol.host_allowed(req.url, navigation=False) else None)
                page = await context.new_page()
                page.set_default_timeout(settings.page_timeout_ms)

                why = await self._ownership_problem(page, tgt, force=force_verify)
                if out["verification_required"]:
                    out["verified"] = why is None
                if why:
                    out.update(allowed=False, blocked_reason=f"소유권 확인 실패: {why} ({tgt.verify_url})")
                    return out
                try:
                    resp = await page.goto(tgt.url, wait_until="load")
                except Exception as exc:
                    msg = (str(exc).splitlines() or [type(exc).__name__])[0]
                    if blocked:
                        out.update(allowed=False, blocked_reason=f"허용되지 않은 호스트로의 요청/리다이렉트 차단: {blocked[0]}")
                    else:
                        out["blocked_reason"] = None
                        out["error"] = msg
                    return out
                hops = []
                req = resp.request if resp else None
                while req is not None:
                    hops.append(req.url)
                    req = req.redirected_from
                out["redirects"] = list(reversed(hops))[1:] if len(hops) > 1 else []
                out["final_url"] = page.url
                bad = [u for u in [page.url, *hops] if not pol.host_allowed(u, navigation=True)]
                if bad:
                    out.update(allowed=False, blocked_reason=f"리다이렉트 최종 목적지가 허용되지 않음: {bad[0]}")
                    return out
                out["http_status"] = resp.status if resp else None
                ct = (resp.headers.get("content-type") if resp else None) or ""
                out["content_type"] = ct or None
                from .custom_target import is_video_content_type
                out["is_direct_video"] = is_video_content_type(ct)
                out["is_html"] = "html" in ct.lower()
                if out["is_html"] or out["is_direct_video"]:
                    # Chromium shows a direct video file in a "media document" that contains a <video>.
                    try:
                        await page.wait_for_selector("video", state="attached", timeout=3000)
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)  # let metadata load
                    st = await page.evaluate(VIDEO_STATUS_JS)
                    out["video_found"] = bool(st.get("present"))
                    out["video"] = video_view(st, False) if st.get("present") else None
                    if out["video"] and out["video"]["error"] and out["video"]["error"]["code"] in (3, 4):
                        out["video"]["hint"] = CODEC_HINT.strip(" —")
                if blocked:
                    out["blocked_subrequests"] = blocked[:10]
                return out
            finally:
                if browser is not None:
                    try:
                        await asyncio.wait_for(browser.close(), timeout=5)
                    except Exception:
                        pass
                await asyncio.to_thread(kill_lab_processes, marker)
                left = await asyncio.to_thread(find_lab_processes, marker)
                if left:
                    self.db.log("error", f"URL probe left {len(left)} processes")
                self.db.log("info", f"URL probe: {tgt.input_url[:200]} -> allowed={out['allowed']} "
                                    f"status={out['http_status']} video={out['video_found']}")

    async def _video_cycle(self, s: TestSession, page: Page) -> None:
        """Load the page, start the first HTML5 <video>, then poll its state until stop or failure."""
        if not await self._iteration(s, page):
            return
        opts = {"muted": bool(s.custom.get("muted", True)), "loop": bool(s.custom.get("loop", True))}
        try:
            await page.wait_for_selector("video", state="attached", timeout=VIDEO_FIND_TIMEOUT_MS)
        except Exception:
            s.video = {"present": False, "state": "none"}
            self._record_error(s, "No HTML5 <video> element found on the page")
            return
        res = await page.evaluate(VIDEO_INSTALL_JS, opts)
        if not res.get("present"):
            s.video = {"present": False, "state": "none"}
            self._record_error(s, "No HTML5 <video> element found on the page")
            return
        if not res.get("started"):
            try:
                s.video = video_view(await page.evaluate(VIDEO_STATUS_JS), opts["loop"])
            except Exception:
                s.video = {"present": True, "state": "error", "repeats": 0}
            s.video["state"] = "error"
            err = res.get("error") or ""
            hint = CODEC_HINT if ("NotSupported" in err or "error 4" in err) else ""
            self._record_error(s, f"Video playback failed to start: {err}{hint}")
            return
        self.db.log("info", f"{s.name}: video playback started (muted={opts['muted']}, loop={opts['loop']})",
                    session_id=s.id)
        while not s.stop_event.is_set():
            try:
                st = await page.evaluate(VIDEO_STATUS_JS)
            except Exception as exc:
                if s.stop_event.is_set():
                    return
                self._record_error(s, f"Lost the page while playing: {(str(exc).splitlines() or [''])[0]}")
                return  # outer loop reloads after the interval
            if not st.get("present") or not st.get("installed"):
                self._record_error(s, "Video element disappeared or the page navigated away")
                return
            s.video = video_view(st, opts["loop"])
            for err in st.get("errors", []):
                self._record_error(s, f"Video: {err}{CODEC_HINT if 'SRC_NOT_SUPPORTED' in err or 'DECODE' in err else ''}")
            if st["state"] == "error":
                return
            if st["state"] == "playing":
                s.consecutive_failures = 0
            try:
                await asyncio.wait_for(s.stop_event.wait(), timeout=VIDEO_POLL_S)
            except asyncio.TimeoutError:
                pass

    async def _check_storage_isolation(self, s: TestSession, page: Page) -> None:
        """Custom targets may be any allowed origin, so only localStorage is checked (no lab endpoint)."""
        try:
            result = await page.evaluate(
                """(sid) => { try { const k = '__blc_lab_sid'; const before = localStorage.getItem(k);
                     if (!before) localStorage.setItem(k, sid);
                     return {before, now: localStorage.getItem(k)}; } catch (e) { return null; } }""", s.id)
        except Exception:
            result = None
        if result is None:
            return  # storage unavailable on this page (e.g. sandboxed); not a failure
        ok = result["now"] == s.id and result["before"] in (None, s.id)
        if not ok and s.isolation_ok is not False:
            self.db.log("error", "Isolation check failed", session_id=s.id, data=result)
        s.isolation_ok = ok

    async def _check_isolation(self, s: TestSession, page: Page) -> None:
        """Each session writes its own id to localStorage + a cookie and must only ever read back its own."""
        result = await page.evaluate(
            """async (sid) => {
                 const before = localStorage.getItem('lab_sid');
                 if (!before) { localStorage.setItem('lab_sid', sid); document.cookie = 'lab_sid=' + sid + '; path=/'; }
                 const r = await fetch('/lab/whoami', {credentials: 'same-origin'});
                 const who = await r.json();
                 return {before, storage: localStorage.getItem('lab_sid'), cookie: who.lab_sid};
               }""",
            s.id,
        )
        ok = result["storage"] == s.id and result["cookie"] == s.id and result["before"] in (None, s.id)
        if not ok and s.isolation_ok is not False:
            self.db.log("error", "Isolation check failed", session_id=s.id, data=result)
        s.isolation_ok = ok

    async def _cleanup(self, s: TestSession, context: BrowserContext | None) -> None:
        s.stop_event.set()  # marks the coming disconnect as intentional
        # Record the full process tree *before* closing: once the browser process exits,
        # its helpers can no longer be found as descendants.
        await asyncio.to_thread(self._sample_session, s)
        try:
            if context is not None:
                await asyncio.wait_for(context.close(), timeout=5)
        except Exception:
            pass
        try:
            if s.browser is not None:
                await asyncio.wait_for(s.browser.close(), timeout=5)
        except Exception:
            pass
        s.browser = None
        # Belt and braces: kill anything with the marker, then any tracked PID still alive.
        killed = await asyncio.to_thread(kill_lab_processes, s.marker)
        killed += await asyncio.to_thread(kill_tracked, s.tracked)
        remaining = await asyncio.to_thread(find_lab_processes, s.marker)
        remaining += [p for p in await asyncio.to_thread(alive_tracked, s.tracked)
                      if p.pid not in {r.pid for r in remaining}]
        s.process_count = len(remaining)
        s.memory_mb = s.memory_rss_mb = s.memory_method = None
        if remaining:
            self.db.log("error", f"Cleanup incomplete: {len(remaining)} process(es) remain",
                        session_id=s.id, data={"pids": [p.pid for p in remaining]})
        else:
            self.db.log("info", f"{s.name} cleaned up: {len(s.tracked)} tracked processes gone "
                                f"(forced kills: {killed})", session_id=s.id)
            s.tracked = set()
            self.db.untrack_session(s.id, os.getpid())

    # ------------------------------------------------------------------ views
    def summary(self) -> dict[str, Any]:
        statuses = [s.status for s in self.sessions.values()]
        return {
            "total": len(statuses),
            "running": statuses.count("running"),
            "starting": statuses.count("starting"),
            "stopping": statuses.count("stopping"),
            "error": statuses.count("error"),
            "idle": statuses.count("idle") + statuses.count("stopped"),
            "max_sessions": settings.max_sessions,
            "max_concurrent": settings.max_concurrent,
            "cpu_threshold": settings.cpu_threshold,
            "ram_threshold": settings.ram_threshold,
            "pressure_block_level": settings.pressure_block_level,
            "admission_blocked_reason": None,
        }

    def snapshot(self, log_limit: int = 50) -> dict[str, Any]:
        summary = self.summary()
        if summary["running"] + summary["starting"] >= settings.max_concurrent:
            summary["admission_blocked_reason"] = f"concurrency limit reached ({settings.max_concurrent})"
        else:
            summary["admission_blocked_reason"] = self._resource_block_reason()
        lab_procs = self.lab_processes()
        lab_mem = {"uss_mb": 0.0, "rss_mb": 0.0}
        methods = set()
        for s in self.sessions.values():
            if s.memory_mb is not None:
                lab_mem["uss_mb"] += s.memory_mb
                lab_mem["rss_mb"] += s.memory_rss_mb or 0
                methods.add(s.memory_method)
        total_mb = self.monitor.memory.total / 1024**2
        try:
            backend_rss = psutil.Process().memory_info().rss / 1024**2
        except psutil.Error:
            backend_rss = None
        return {
            "ts": time.time(),
            "summary": summary,
            "system": {
                **self.monitor.to_dict(),
                "lab_process_count": len(lab_procs),
                "lab_memory": {
                    "uss_mb": round(lab_mem["uss_mb"], 1),
                    "rss_mb": round(lab_mem["rss_mb"], 1),
                    "percent_of_total": round(100 * lab_mem["uss_mb"] / total_mb, 1) if total_mb else 0,
                    "method": "/".join(sorted(m for m in methods if m)) or None,
                    "backend_rss_mb": round(backend_rss, 1) if backend_rss else None,
                },
            },
            "sessions": [s.to_dict() for s in self.sessions.values()],
            "logs": self.db.recent_logs(limit=log_limit),
        }
