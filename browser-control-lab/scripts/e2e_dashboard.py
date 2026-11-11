"""End-to-end check of the dashboard UI: drive the real Next.js page with Playwright.

Requires the backend (default http://127.0.0.1:8000) and the dashboard
(default http://127.0.0.1:3000) to be running. Run with the backend venv:

    backend/.venv/bin/python scripts/e2e_dashboard.py [--sessions 10] [--screenshot-dir reports]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

API = os.environ.get("LAB_API", "http://127.0.0.1:8000")
UI = os.environ.get("LAB_UI", "http://127.0.0.1:3000")


def state() -> dict:
    with urllib.request.urlopen(f"{API}/api/state") as r:
        return json.load(r)


def wait(pred, timeout=60.0, msg="condition"):
    end = time.time() + timeout
    while time.time() < end:
        st = state()
        if pred(st):
            return st
        time.sleep(0.5)
    raise AssertionError(f"timeout waiting for {msg}: {st['summary']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--screenshot-dir", default="reports")
    args = ap.parse_args()
    n = args.sessions
    shots = Path(args.screenshot_dir)
    shots.mkdir(parents=True, exist_ok=True)

    assert state()["summary"]["total"] == 0, "start from an empty lab (use 전체 삭제 first)"
    with sync_playwright() as p:
        # The dashboard only needs localhost, so lock this browser down the same way as lab sessions.
        browser = p.chromium.launch(executable_path=os.environ.get("LAB_CHROMIUM_EXECUTABLE") or None,
                                    args=["--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, "
                                          "EXCLUDE localhost, EXCLUDE 127.0.0.1", "--disable-background-networking",
                                          "--disable-component-update"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
        page.on("dialog", lambda d: d.accept())
        page.goto(UI)
        expect(page.get_by_text("실시간 연결")).to_be_visible(timeout=15000)
        print("✓ dashboard connected over WebSocket")

        page.get_by_label("세션 수").fill(str(n))
        page.get_by_role("button", name="세션 등록").click()
        expect(page.get_by_text(f"{n}개 세션 등록")).to_be_visible()
        print(f"✓ registered {n} sessions from the UI")

        page.get_by_role("button", name="전체 시작").click()
        st = wait(lambda s: s["summary"]["running"] == n and all(x["isolation_ok"] for x in s["sessions"]),
                  msg=f"{n} running sessions")
        print(f"✓ start-all: {st['summary']['running']} running, isolation OK on all, "
              f"{st['system']['lab_process_count']} Chromium processes, CPU {st['system']['cpu_percent']}% "
              f"RAM {st['system']['ram_percent']}%")
        time.sleep(4)  # let a few iterations accumulate for the screenshot
        page.screenshot(path=str(shots / "dashboard-running.png"), full_page=True)

        # Independent control: stop only the first row
        first = st["sessions"][0]
        row = page.get_by_role("row").filter(has_text=first["id"])
        row.get_by_role("button", name="중지", exact=True).click()
        st = wait(lambda s: next(x for x in s["sessions"] if x["id"] == first["id"])["status"] == "stopped",
                  msg="single stop")
        others = [x for x in st["sessions"] if x["id"] != first["id"]]
        assert all(x["status"] == "running" for x in others), "other sessions were affected"
        print(f"✓ stopped {first['name']} alone; other {len(others)} still running")

        row.get_by_role("button", name="시작", exact=True).click()
        wait(lambda s: s["summary"]["running"] == n, msg="single start")
        print(f"✓ restarted {first['name']} from its row")

        page.get_by_role("button", name="전체 중지").click()
        st = wait(lambda s: s["summary"]["running"] == 0 and s["system"]["lab_process_count"] == 0,
                  msg="stop-all + cleanup")
        expect(page.get_by_text("개 중지")).to_be_visible()
        print(f"✓ stop-all: 0 running, {st['system']['lab_process_count']} Chromium processes remain")
        time.sleep(1.5)
        page.screenshot(path=str(shots / "dashboard-stopped.png"), full_page=True)

        page.get_by_role("button", name="전체 삭제").click()
        wait(lambda s: s["summary"]["total"] == 0, msg="delete-all")
        print("✓ delete-all")
        browser.close()
    print("E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
