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

        expect(page.get_by_text("테스트 브라우저 메모리")).to_be_visible()
        expect(page.get_by_text("메모리 압력 · Swap")).to_be_visible()
        print("✓ memory panels visible (system / test browsers / pressure+swap)")

        page.get_by_label("세션 수").fill(str(n))
        page.get_by_role("button", name="세션 등록").click()  # empty lab: registers directly
        expect(page.get_by_text(f"{n}개 추가 등록 (총 {n}개)")).to_be_visible()
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
        expect(page.get_by_text("브라우저 프로세스 0개 확인")).to_be_visible()
        with urllib.request.urlopen(f"{API}/api/processes") as r:
            procs = json.load(r)
        assert procs["count"] == 0, procs
        print(f"✓ stop-all: 0 running, verified 0 browser processes (marker + {procs['tracked_pids']} tracked PIDs)")
        time.sleep(1.5)
        page.screenshot(path=str(shots / "dashboard-stopped.png"), full_page=True)

        # Registering again must not silently accumulate: a choice dialog appears.
        page.get_by_label("세션 수").fill("3")
        page.get_by_role("button", name="세션 등록").click()
        dialog = page.get_by_role("alertdialog")
        expect(dialog).to_contain_text(f"이미 {n}개 세션이 등록되어 있습니다")
        dialog.get_by_role("button", name=f"기존 {n}개 교체 → 3개").click()
        wait(lambda s: s["summary"]["total"] == 3, msg="replace")
        print(f"✓ re-register shows a choice; '교체' left exactly 3 sessions (not {n + 3})")
        page.get_by_label("세션 수").fill("2")
        page.get_by_role("button", name="세션 등록").click()
        page.get_by_role("alertdialog").get_by_role("button", name="2개 추가 → 총 5개").click()
        wait(lambda s: s["summary"]["total"] == 5, msg="add")
        print("✓ '추가' explicitly accumulated to 5")
        page.get_by_label("세션 수").fill("1")
        page.get_by_role("button", name="세션 등록").click()
        page.get_by_role("alertdialog").get_by_role("button", name="취소").click()
        time.sleep(1)
        assert state()["summary"]["total"] == 5
        print("✓ '취소' registered nothing")

        page.get_by_role("button", name="전체 삭제").click()
        wait(lambda s: s["summary"]["total"] == 0, msg="delete-all")
        print("✓ delete-all")

        # ---- custom URL scenario
        expect(page.get_by_text("현재 등록 0개")).to_be_visible()  # UI caught up with delete-all
        page.get_by_label("테스트 시나리오").select_option("custom")
        url_box = page.get_by_label("테스트 URL")
        expect(url_box).to_be_visible()
        url_box.fill("https://www.youtube.com/watch?v=abc")
        page.get_by_label("세션 수").fill("2")
        page.get_by_role("button", name="세션 등록").click()
        expect(page.get_by_text("third-party media/social platform")).to_be_visible()
        assert state()["summary"]["total"] == 0
        print("✓ custom: YouTube URL refused by the backend, error shown, nothing registered")

        url_box.fill(f"{API}/lab/video")
        page.get_by_label("모드").select_option("video")
        expect(page.get_by_label("Muted")).to_be_checked()
        page.get_by_role("button", name="URL 확인").click()
        expect(page.get_by_text("허용됨 (localhost)")).to_be_visible()
        page.get_by_role("button", name="세션 등록").click()
        wait(lambda s: s["summary"]["total"] == 2, msg="custom register")
        page.get_by_role("button", name="전체 시작").click()
        st = wait(lambda s: all((x["video"] or {}).get("repeats", 0) >= 1 for x in s["sessions"]),
                  msg="custom video repeats", timeout=60)
        expect(page.get_by_text("반복 1회").first.or_(page.get_by_text("반복 2회").first)).to_be_visible()
        v = st["sessions"][0]["video"]
        print(f"✓ custom video: 2 sessions playing, state={v['state']} t={v['current_time']}/{v['duration']}s "
              f"repeats={v['repeats']}")
        time.sleep(1)
        page.screenshot(path=str(shots / "dashboard-custom-video.png"), full_page=True)
        page.get_by_role("button", name="전체 중지").click()
        wait(lambda s: s["summary"]["running"] == 0 and s["system"]["lab_process_count"] == 0, msg="custom stop-all")
        with urllib.request.urlopen(f"{API}/api/processes") as r:
            assert json.load(r)["count"] == 0
        print("✓ custom stop-all: 0 browser processes")
        page.get_by_role("button", name="전체 삭제").click()
        wait(lambda s: s["summary"]["total"] == 0, msg="delete-all 2")
        browser.close()
    print("E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
