"""Custom URL scenario end-to-end with real headless Chromium (localhost + a pinned allow-listed host)."""
import dataclasses
import os

import pytest

from app.config import settings
from app.procs import find_lab_processes
from tests.test_api import _by_id, _clean, _wait, server  # noqa: F401  (fixtures)

PORT = settings.port
LOCAL = f"http://127.0.0.1:{PORT}"
ALLOWED = f"http://allowed.test:{PORT}"   # pinned to 127.0.0.1 via LAB_CUSTOM_HOST_OVERRIDES


def create(client, url, mode="page_load", count=1, **kw):
    body = {"count": count, "scenario": "custom",
            "custom": {"url": url, "mode": mode, "reload_interval_s": kw.pop("interval", 1), **kw}}
    return client.post("/api/sessions", json=body)


def start_one(client, url, **kw):
    r = create(client, url, **kw)
    assert r.status_code == 201, r.text
    sid = r.json()[0]["id"]
    assert client.post(f"/api/sessions/{sid}/start").status_code == 200
    return sid


# ---------------------------------------------------------------- API validation (backend)
@pytest.mark.parametrize("url, needle", [
    ("https://not-allowed.example/", "not in the allowlist"),
    ("https://www.youtube.com/watch?v=x", "third-party media/social platform"),
    ("file:///etc/passwd", "Only http and https"),
    ("javascript:alert(1)", "Only http and https"),
    ("http://user:pw@localhost/", "Credentials"),
    ("not a url", "whitespace"),
    ("http://10.1.2.3/", "not in the allowlist"),
])
def test_api_rejects_disallowed_urls(server, url, needle):
    r = create(server, url)
    assert r.status_code == 400 and needle in r.json()["detail"], r.text
    assert server.get("/api/state").json()["summary"]["total"] == 0          # nothing registered
    v = server.post("/api/custom/validate", json={"url": url})
    assert v.status_code == 400 and needle in v.json()["detail"]


def test_api_requires_custom_object(server):
    r = server.post("/api/sessions", json={"count": 1, "scenario": "custom"})
    assert r.status_code == 400 and "requires a 'custom' object" in r.json()["detail"]


def test_validate_endpoint_and_config(server):
    v = server.post("/api/custom/validate", json={"url": f"{LOCAL}/lab/video"}).json()
    assert v["ok"] and v["local"] and not v["verification_required"]
    v = server.post("/api/custom/validate", json={"url": f"{ALLOWED}/lab/basic"}).json()
    assert v["host"] == "allowed.test" and v["resolved_ips"] == ["127.0.0.1"]
    cfg = server.get("/api/config").json()
    assert cfg["custom"]["allowed_hosts"] == ["allowed.test"]
    assert cfg["custom"]["verification"]["path"] == "/.well-known/blc-lab-verify.txt"
    assert cfg["custom"]["verification"]["token"].startswith("blc-lab-")
    assert cfg["max_sessions"] == 100


def test_start_revalidates(server, monkeypatch):
    """A session registered while allowed is refused at start if the allowlist no longer has it."""
    import app.session_manager as sm
    r = create(server, f"{ALLOWED}/lab/basic")
    sid = r.json()[0]["id"]
    monkeypatch.setattr(sm, "settings", dataclasses.replace(settings, custom_allowed_hosts=""))
    r = server.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 400 and "not in the allowlist" in r.json()["detail"]


def test_basic_scenarios_unchanged(server):
    r = server.post("/api/sessions", json={"count": 1, "scenario": "basic",
                                           "custom": {"url": "https://evil.example/"}})
    assert r.status_code == 201 and r.json()[0]["custom"] is None
    assert r.json()[0]["target_url"].endswith("/lab/basic")


# ---------------------------------------------------------------- page load mode
def test_page_load_localhost(server):
    sid = start_one(server, f"{LOCAL}/lab/basic", interval=1)
    st = _wait(server, lambda st: _by_id(st, sid)["page_load_count"] >= 3)
    s = _by_id(st, sid)
    assert s["status"] == "running" and s["page_state"] == "loaded" and s["errors"] == 0
    assert s["current_url"] == f"{LOCAL}/lab/basic" and s["custom"]["mode"] == "page_load"
    assert s["avg_load_ms"] > 0 and s["isolation_ok"] is True


def test_page_load_allowlisted_domain(server):
    sid = start_one(server, f"{ALLOWED}/lab/basic", interval=1)
    st = _wait(server, lambda st: _by_id(st, sid)["page_load_count"] >= 2)
    s = _by_id(st, sid)
    assert s["errors"] == 0 and s["current_url"].startswith("http://allowed.test:")


@pytest.mark.parametrize("path, target", [("/lab/redirect-external", "blocked-by-lab.example"),
                                          ("/lab/redirect-ip", "169.254.169.254")])
def test_redirect_out_of_allowlist_blocked(server, path, target):
    """Redirect hops bypass Playwright's route guard, so the network-level policy must stop them
    (including IP literals, which DNS pinning alone would not cover)."""
    sid = start_one(server, f"{LOCAL}{path}", interval=1)
    st = _wait(server, lambda st: _by_id(st, sid)["errors"] >= 1)
    s = _by_id(st, sid)
    assert "Blocked" in s["last_error"] and target in s["last_error"], s["last_error"]
    assert s["blocked_requests"] >= 1 and s["page_load_count"] == 0
    logs = server.get("/api/logs", params={"session_id": sid, "level": "warning"}).json()
    assert any(target in l["message"] for l in logs)


def test_redirect_within_allowlist_ok(server):
    sid = start_one(server, f"{LOCAL}/lab/redirect-local", interval=1)
    st = _wait(server, lambda st: _by_id(st, sid)["page_load_count"] >= 1)
    assert _by_id(st, sid)["current_url"].endswith("/lab/basic") and _by_id(st, sid)["errors"] == 0


def test_external_subresources_blocked(server):
    """A page on an allowed host cannot pull from non-allowed hosts (only resource hosts)."""
    from app.main import app
    from fastapi.responses import HTMLResponse

    async def page():
        return HTMLResponse('<html><body><img src="https://evil.example/pixel.png">'
                            f'<img src="http://cdn.allowed.test:{PORT}/lab/asset.js"></body></html>')
    app.add_api_route("/test-only/subres", page)
    try:
        sid = start_one(server, f"{ALLOWED}/test-only/subres", interval=1)
        st = _wait(server, lambda st: _by_id(st, sid)["page_load_count"] >= 1 and _by_id(st, sid)["blocked_requests"] >= 1)
        logs = server.get("/api/logs", params={"session_id": sid, "level": "warning"}).json()
        assert any("evil.example" in l["message"] for l in logs)
        assert not any("cdn.allowed.test" in l["message"] for l in logs)
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/test-only/subres"]


# ---------------------------------------------------------------- video playback mode
def test_video_detect_play_and_loop_count(server):
    sid = start_one(server, f"{LOCAL}/lab/video", mode="video", muted=True, loop=True, interval=1)
    st = _wait(server, lambda st: (_by_id(st, sid)["video"] or {}).get("repeats", 0) >= 2, timeout=40)
    v = _by_id(st, sid)["video"]
    assert v["present"] and v["state"] in ("playing", "ended") and v["muted"] is True and v["loop"] is True
    assert 1.5 < v["duration"] < 2.5 and 0 <= v["current_time"] <= v["duration"]
    assert _by_id(st, sid)["errors"] == 0 and _by_id(st, sid)["page_load_count"] == 1   # no reloads in video mode


def test_video_no_loop_stops_after_one(server):
    sid = start_one(server, f"{LOCAL}/lab/video", mode="video", muted=True, loop=False, interval=1)
    st = _wait(server, lambda st: (_by_id(st, sid)["video"] or {}).get("state") == "ended", timeout=30)
    import time
    time.sleep(3)
    v = _by_id(server.get("/api/state").json(), sid)["video"]
    assert v["state"] == "ended" and v["repeats"] == 1


def test_video_unmuted_autoplay(server):
    sid = start_one(server, f"{LOCAL}/lab/video", mode="video", muted=False, loop=True, interval=1)
    st = _wait(server, lambda st: (_by_id(st, sid)["video"] or {}).get("state") == "playing", timeout=30)
    assert _by_id(st, sid)["video"]["muted"] is False


def test_video_missing_records_error(server, monkeypatch):
    import app.session_manager as sm
    monkeypatch.setattr(sm, "VIDEO_FIND_TIMEOUT_MS", 800)
    sid = start_one(server, f"{LOCAL}/lab/basic", mode="video", interval=1)
    st = _wait(server, lambda st: _by_id(st, sid)["errors"] >= 1, timeout=30)
    s = _by_id(st, sid)
    assert "No HTML5 <video>" in s["last_error"] and s["video"]["present"] is False
    st = _wait(server, lambda st: _by_id(st, sid)["status"] == "error", timeout=40)  # 5 consecutive -> error
    assert find_lab_processes(f"{os.getpid()}:{sid}") == []


# ---------------------------------------------------------------- ownership verification
def test_ownership_verification(server, monkeypatch):
    import app.main as main
    import app.session_manager as sm
    from fastapi.responses import PlainTextResponse
    strict = dataclasses.replace(settings, custom_require_verification=True)
    monkeypatch.setattr(sm, "settings", strict)
    monkeypatch.setattr(main, "settings", strict)
    main.manager._verified_origins.clear()

    # 1) target without the file -> session error, nothing tested
    sid = start_one(server, f"{ALLOWED}/lab/basic", interval=1)
    st = _wait(server, lambda st: _by_id(st, sid)["status"] == "error", timeout=30)
    s = _by_id(st, sid)
    assert "Ownership verification failed" in s["last_error"] and s["page_load_count"] == 0

    # 2) serve the token -> verified, test runs
    token = settings.verification_token

    async def verify():
        return PlainTextResponse(token + "\n")
    main.app.add_api_route("/.well-known/blc-lab-verify.txt", verify)
    try:
        sid2 = start_one(server, f"{ALLOWED}/lab/basic", interval=1)
        st = _wait(server, lambda st: _by_id(st, sid2)["page_load_count"] >= 1, timeout=30)
        assert _by_id(st, sid2)["errors"] == 0
        logs = server.get("/api/logs", params={"session_id": sid2}).json()
        assert any("Ownership verified" in l["message"] for l in logs)
    finally:
        main.app.router.routes[:] = [r for r in main.app.router.routes
                                     if getattr(r, "path", "") != "/.well-known/blc-lab-verify.txt"]
        main.manager._verified_origins.clear()

    # 3) localhost targets never need verification
    sid3 = start_one(server, f"{LOCAL}/lab/basic", interval=1)
    _wait(server, lambda st: _by_id(st, sid3)["page_load_count"] >= 1, timeout=30)


# ---------------------------------------------------------------- protection + cleanup
def test_stop_all_cleans_custom_sessions(server):
    start_one(server, f"{LOCAL}/lab/video", mode="video", interval=1)
    start_one(server, f"{ALLOWED}/lab/basic", interval=1)
    server.post("/api/sessions", json={"count": 1, "scenario": "basic"})
    server.post("/api/sessions/start-all")
    _wait(server, lambda st: st["summary"]["running"] == 3)
    out = server.post("/api/sessions/stop-all").json()
    assert out["verified_clean"] is True and out["remaining_processes"] == 0
    assert server.get("/api/processes").json()["count"] == 0
    assert find_lab_processes(str(os.getpid())) == []


def test_guards_block_custom_sessions(server, monkeypatch):
    from app.main import manager
    from app.sysmem import MemorySnapshot
    r = create(server, f"{LOCAL}/lab/video", mode="video")
    sid = r.json()[0]["id"]
    monkeypatch.setattr(manager.monitor, "refresh_memory", lambda: None)
    gb = 1024**3
    monkeypatch.setattr(manager.monitor, "memory", MemorySnapshot(total=16 * gb, used=int(14 * gb),
                                                                  pressure={"level": "normal"}))
    r = server.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 429 and "RAM 88% >= threshold 85%" in r.json()["detail"]
    monkeypatch.setattr(manager.monitor, "memory", MemorySnapshot(total=16 * gb, used=int(4 * gb),
                                                                  pressure={"level": "warn"}))
    r = server.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 429 and "pressure 'warn'" in r.json()["detail"]
