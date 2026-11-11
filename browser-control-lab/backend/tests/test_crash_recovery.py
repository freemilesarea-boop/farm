"""Kill the server with SIGKILL while sessions run, then verify a restart reaps the orphans."""
import os
import signal
import subprocess
import sys
import tempfile
import time

import httpx

from app.procs import find_lab_processes

PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"


def _spawn(env):
    proc = subprocess.Popen([sys.executable, "-m", "app.main"], env=env,
                            cwd=os.path.dirname(os.path.dirname(__file__)),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/api/health", timeout=0.5)
            return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def test_orphans_reaped_after_crash():
    env = {**os.environ, "LAB_PORT": str(PORT),
           "LAB_DB_PATH": os.path.join(tempfile.mkdtemp(prefix="blc-crash-"), "db.sqlite3")}
    first = _spawn(env)
    try:
        httpx.post(f"{BASE}/api/sessions", json={"count": 3})
        httpx.post(f"{BASE}/api/sessions/start-all", timeout=30)
        for _ in range(100):
            st = httpx.get(f"{BASE}/api/state").json()
            if st["summary"]["running"] == 3:
                break
            time.sleep(0.2)
        assert st["summary"]["running"] == 3
        os.kill(first.pid, signal.SIGKILL)  # simulate a crash: no cleanup code runs
        first.wait(timeout=10)
        time.sleep(1)
        # Playwright's driver usually takes its browsers down with it, but if the driver itself
        # is killed hard, Chromium survives. Plant such an orphan explicitly so the recovery
        # path is always exercised.
        from app.config import PROCESS_MARKER, settings
        exe = settings.chromium_executable
        if exe is None:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                exe = p.chromium.executable_path
        fake = subprocess.Popen([exe, "--headless=new", "--no-sandbox", f"{PROCESS_MARKER}={first.pid}:orphan",
                                 f"--user-data-dir={tempfile.mkdtemp(prefix='blc-orphan-')}", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):
            if find_lab_processes(str(first.pid)):
                break
            time.sleep(0.1)
        assert find_lab_processes(str(first.pid)), "orphan simulation failed"

        second = _spawn(env)
        try:
            assert find_lab_processes(str(first.pid)) == [], "orphans not reaped on restart"
            fake.wait(timeout=5)
            st = httpx.get(f"{BASE}/api/state").json()
            assert st["summary"]["total"] == 3 and st["summary"]["running"] == 0
            logs = httpx.get(f"{BASE}/api/logs", params={"level": "warning"}).json()
            assert any("unclean shutdown" in l["message"] for l in logs)
            runs = httpx.get(f"{BASE}/api/runs").json()
            assert all(r["end_reason"] == "recovered_after_crash" for r in runs)
        finally:
            second.send_signal(signal.SIGTERM)
            second.wait(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()
        # last-resort cleanup so a failing test never leaks browsers
        from app.procs import kill_lab_processes
        kill_lab_processes(str(first.pid))
