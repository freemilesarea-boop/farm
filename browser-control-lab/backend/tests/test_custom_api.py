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
    ("https://www.twitch.tv/somechannel", "third-party media/social platform"),
    ("https://www.youtube.com/channel/x", "YouTube single-session test needs one video URL"),
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


# ================================================================ v2: input preservation, direct video files, probe
def test_input_url_preserved_verbatim(server):
    raw = f"HTTP://LOCALHOST:{PORT}/lab/video?Clip=A%20B#t=0.5"
    s = create(server, raw, mode="video").json()[0]
    assert s["custom"]["url"] == raw                                   # exactly as typed
    assert s["custom"]["nav_url"] == f"http://localhost:{PORT}/lab/video?Clip=A%20B#t=0.5"
    assert s["custom"]["kind"] == "page" and s["custom"]["mode"] == "video"
    assert "lab/video" in s["target_url"] and "/lab/player" not in s["target_url"]


def test_direct_webm_file_plays_in_internal_player(server):
    url = f"{LOCAL}/lab/static/test-2s.webm"
    sid = start_one(server, url, mode="video", muted=True, loop=True)
    st = _wait(server, lambda st: (_by_id(st, sid)["video"] or {}).get("repeats", 0) >= 1, timeout=40)
    s = _by_id(st, sid)
    assert s["custom"]["kind"] == "video_file" and s["custom"]["url"] == url
    assert "/lab/player?src=" in s["current_url"]                      # internal wrapper page
    v = s["video"]
    assert v["current_src"] == url and v["state"] in ("playing", "ended") and 1.5 < v["duration"] < 2.5
    assert v["ready_state_name"] in ("HAVE_FUTURE_DATA", "HAVE_ENOUGH_DATA", "HAVE_CURRENT_DATA", "HAVE_METADATA")
    assert v["network_state_name"] in ("NETWORK_IDLE", "NETWORK_LOADING") and v["error"] is None
    assert v["muted"] is True and v["paused"] is False or v["ended"] is True


def test_direct_mp4_h264_file(server):
    """H.264/AAC MP4 plays in the bundled Chromium build used here (verified: build 153)."""
    sid = start_one(server, f"{LOCAL}/lab/static/test-2s-h264.mp4", mode="video", muted=False, loop=True)
    st = _wait(server, lambda st: (_by_id(st, sid)["video"] or {}).get("repeats", 0) >= 1, timeout=40)
    v = _by_id(st, sid)["video"]
    assert v["error"] is None and v["muted"] is False and abs(v["duration"] - 2.0) < 0.2


def test_unplayable_file_records_media_error(server):
    sid = start_one(server, f"{LOCAL}/lab/static/broken.mp4", mode="video")
    st = _wait(server, lambda st: _by_id(st, sid)["errors"] >= 1, timeout=30)
    s = _by_id(st, sid)
    assert "Video playback failed to start" in s["last_error"] or "media error" in s["last_error"]
    assert "코덱" in s["last_error"]                                     # actionable hint
    assert s["video"]["state"] == "error"


def test_redirect_to_allowed_video_plays(server):
    sid = start_one(server, f"{LOCAL}/lab/redirect-video", mode="video", loop=True)
    st = _wait(server, lambda st: (_by_id(st, sid)["video"] or {}).get("state") in ("playing", "ended"), timeout=30)
    assert _by_id(st, sid)["current_url"].endswith("/lab/static/test-2s.webm")


def test_registered_config_not_changed_by_later_registration(server):
    a = create(server, f"{LOCAL}/lab/video", mode="video", muted=True, loop=False).json()[0]
    b = create(server, f"{LOCAL}/lab/static/test-2s.webm", mode="video", muted=False, loop=True).json()[0]
    sessions = {s["id"]: s for s in server.get("/api/sessions").json()}
    assert sessions[a["id"]]["custom"]["url"] == f"{LOCAL}/lab/video"
    assert sessions[a["id"]]["custom"]["loop"] is False and sessions[a["id"]]["custom"]["muted"] is True
    assert sessions[b["id"]]["custom"]["url"].endswith("test-2s.webm")
    # restart keeps the original snapshot
    server.post(f"/api/sessions/{a['id']}/start")
    st = _wait(server, lambda st: (_by_id(st, a["id"])["video"] or {}).get("state") in ("playing", "ended"))
    assert _by_id(st, a["id"])["custom"]["url"] == f"{LOCAL}/lab/video"
    assert _by_id(st, a["id"])["video"]["loop"] is False


@pytest.mark.parametrize("path, expect", [
    ("/lab/video", dict(allowed=True, is_html=True, is_direct_video=False, video_found=True, http_status=200)),
    ("/lab/static/test-2s.webm", dict(allowed=True, is_html=False, is_direct_video=True, video_found=True,
                                       content_type="video/webm", media_kind="video_file")),
    ("/lab/static/test-2s-h264.mp4", dict(allowed=True, is_direct_video=True, content_type="video/mp4")),
    ("/lab/basic", dict(allowed=True, is_html=True, video_found=False)),
    ("/lab/redirect-video", dict(allowed=True, is_direct_video=True)),
    ("/lab/redirect-external", dict(allowed=False)),
    ("/lab/redirect-ip", dict(allowed=False)),
])
def test_probe_reports(server, path, expect):
    r = server.post("/api/custom/probe", json={"url": f"{LOCAL}{path}"}).json()
    assert r["input_url"] == f"{LOCAL}{path}"
    for k, v in expect.items():
        assert r[k] == v if k != "content_type" else r[k].startswith(v), (k, r)
    if r["allowed"]:
        assert r["final_url"] and r["blocked_reason"] is None
        if r["video_found"]:
            assert r["video"]["duration"] and r["video"]["ready_state_name"]
    else:
        assert "차단" in r["blocked_reason"]
    if path == "/lab/redirect-video":
        assert r["final_url"].endswith("/lab/static/test-2s.webm") and r["redirects"]
    assert find_lab_processes(f"{os.getpid()}:probe") == []


@pytest.mark.parametrize("url, needle", [("https://www.youtube.com/watch?v=1", "third-party"),
                                         ("file:///etc/passwd", "Only http and https"),
                                         ("https://not-allowed.example/v.mp4", "allowlist")])
def test_probe_blocked_urls(server, url, needle):
    r = server.post("/api/custom/probe", json={"url": url}).json()
    assert r["allowed"] is False and needle in r["blocked_reason"] and r["input_url"] == url


def test_probe_respects_resource_guard(server, monkeypatch):
    from app.main import manager
    monkeypatch.setattr(manager.monitor.memory, "pressure", {"level": "warn"})
    monkeypatch.setattr(manager.monitor, "refresh_memory", lambda: None)
    r = server.post("/api/custom/probe", json={"url": f"{LOCAL}/lab/video"})
    assert r.status_code == 429 and "pressure" in r.json()["detail"]


def test_player_page_validates_and_escapes(server):
    assert server.get("/lab/player", params={"src": "https://evil.example/x.mp4"}).status_code == 400
    assert server.get("/lab/player", params={"src": "file:///etc/passwd"}).status_code == 400
    evil = f"{LOCAL}/x.mp4?a=</script><script>alert(1)</script>"
    r = server.get("/lab/player", params={"src": evil.replace(" ", "")})
    assert r.status_code == 200 and "<script>alert(1)" not in r.text and "\\u003c/script" in r.text


def test_stop_all_after_direct_video_and_probe(server):
    start_one(server, f"{LOCAL}/lab/static/test-2s.webm", mode="video")
    start_one(server, f"{LOCAL}/lab/static/test-2s-h264.mp4", mode="video")
    server.post("/api/custom/probe", json={"url": f"{LOCAL}/lab/video"})
    _wait(server, lambda st: st["summary"]["running"] == 2)
    out = server.post("/api/sessions/stop-all").json()
    assert out["verified_clean"] and out["remaining_processes"] == 0
    assert server.get("/api/processes").json()["count"] == 0 and find_lab_processes(str(os.getpid())) == []


# ================================================================ v3: register my own host from the dashboard
MINE = f"http://mine.test:{PORT}"   # pinned to 127.0.0.1, but not allow-listed


def test_register_host_requires_ownership_token(server):
    import app.main as main
    from fastapi.responses import PlainTextResponse
    main.manager._verified_origins.clear()
    url = f"{MINE}/lab/video"

    # Not allowed yet: probe says so and offers registration; sessions are refused.
    p = server.post("/api/custom/probe", json={"url": url}).json()
    assert not p["allowed"] and p["registrable_host"] == "mine.test"
    assert create(server, url).status_code == 400

    # Registration without the token file fails and persists nothing.
    r = server.post("/api/custom/register", json={"url": url}).json()
    assert not r["registered"] and r["verified"] is False and "소유권 확인 실패" in r["blocked_reason"]
    assert "mine.test" not in server.get("/api/config").json()["custom"]["allowed_hosts"]

    token = settings.verification_token

    async def verify():
        return PlainTextResponse(token + "\n")
    main.app.add_api_route("/.well-known/blc-lab-verify.txt", verify)
    try:
        r = server.post("/api/custom/register", json={"url": url}).json()
        assert r["registered"] and r["verified"] and r["video_found"], r
        cfg = server.get("/api/config").json()["custom"]
        assert "mine.test" in cfg["allowed_hosts"] and cfg["registered_hosts"] == ["mine.test"]
        assert server.post("/api/custom/probe", json={"url": url}).json()["allowed"]
        # ... and the video now plays in a session
        sid = start_one(server, url, mode="video", interval=1)
        st = _wait(server, lambda st: (_by_id(st, sid).get("video") or {}).get("state") == "playing", timeout=40)
        assert _by_id(st, sid)["errors"] == 0
    finally:
        main.app.router.routes[:] = [r for r in main.app.router.routes
                                     if getattr(r, "path", "") != "/.well-known/blc-lab-verify.txt"]
        main.manager._verified_origins.clear()
        server.post("/api/sessions/stop-all")
        server.delete("/api/sessions")

    # Removing it closes the door again.
    assert server.delete("/api/custom/registered/mine.test").status_code == 200
    assert "mine.test" not in server.get("/api/config").json()["custom"]["allowed_hosts"]
    assert create(server, url).status_code == 400
    assert server.delete("/api/custom/registered/mine.test").status_code == 404


@pytest.mark.parametrize("url, needle", [
    ("https://www.youtube.com/watch?v=x", "third-party media/social platform"),
    ("https://chzzk.naver.com/live/x", "third-party media/social platform"),
    ("ftp://mine.test/x.mp4", "Only http and https"),
])
def test_register_refuses_platforms_and_bad_urls(server, url, needle):
    r = server.post("/api/custom/register", json={"url": url}).json()
    assert not r["registered"] and needle in r["blocked_reason"]
    p = server.post("/api/custom/probe", json={"url": url}).json()
    assert p["registrable_host"] is None


# ================================================================ YouTube single-session compatibility test
YT = f"http://www.youtube.com:{PORT}/watch?v=AAAAAAAAAAA"   # fake YouTube served by the test app


@pytest.fixture
def fake_youtube(server, monkeypatch):
    """Serve a YouTube-like watch page (#movie_player + 2 s WebM). With ?next=1 the page 'autoplays'
    another video when it ends, like YouTube does."""
    import app.main as main
    from fastapi.responses import HTMLResponse
    monkeypatch.setattr(main.manager, "youtube_cooldown_s", 0)
    main.manager._youtube_last_start = None
    hits = []

    async def watch(v: str = "", next: int = 0):
        hits.append(v)
        js = ("document.querySelector('video').addEventListener('ended', () => "
              "{ location.href = '/watch?v=BBBBBBBBBBB'; });") if next else ""
        return HTMLResponse(f'<!doctype html><title>fake yt</title><div id="movie_player">'
                            f'<video src="/lab/static/test-2s.webm" muted playsinline></video></div><script>{js}</script>')
    main.app.add_api_route("/watch", watch)
    yield hits
    main.app.router.routes[:] = [r for r in main.app.router.routes if getattr(r, "path", "") != "/watch"]
    main.manager._youtube_last_start = None


def yt_create(client, url=YT, count=1, **kw):
    body = {"count": count, "scenario": "custom", "replace": kw.pop("replace", False),
            "custom": {"url": url, "mode": kw.pop("mode", "video"), "loop": kw.pop("loop", False),
                       "reload_interval_s": kw.pop("interval", 10), "muted": True}}
    return client.post("/api/sessions", json=body)


def test_youtube_urls_recognised(server):
    for url in [YT, f"http://youtu.be:{PORT}/AAAAAAAAAAA", "https://www.youtube.com/shorts/AAAAAAAAAAA"]:
        v = server.post("/api/custom/validate", json={"url": url})
        assert v.status_code == 200 and v.json()["youtube_single_session"], v.text
    r = yt_create(server, f"http://youtu.be:{PORT}/AAAAAAAAAAA")
    assert r.status_code == 201 and r.json()[0]["custom"]["kind"] == "youtube" and r.json()[0]["youtube"]


@pytest.mark.parametrize("kw, needle", [
    ({"count": 2}, "세션 수는 1개만"),
    ({"count": 100}, "세션 수는 1개만"),
    ({"loop": True}, "loop"),
    ({"mode": "page_load"}, "새로고침"),
])
def test_youtube_rejects_multi_loop_reload(server, kw, needle):
    r = yt_create(server, **kw)
    assert r.status_code == 400 and needle in r.json()["detail"], r.text
    assert server.get("/api/state").json()["summary"]["total"] == 0


def test_youtube_single_playback_then_stops(server, fake_youtube):
    r = yt_create(server)
    assert r.status_code == 201
    sid = r.json()[0]["id"]
    # a second YouTube session can be neither registered nor added through the API
    assert yt_create(server).status_code == 409
    assert server.get("/api/state").json()["summary"]["total"] == 1

    assert server.post(f"/api/sessions/{sid}/start").status_code == 200
    st = _wait(server, lambda st: _by_id(st, sid)["status"] in ("stopped", "error"), timeout=60)
    s = _by_id(st, sid)
    assert s["status"] == "stopped", s
    assert s["errors"] == 0 and s["page_load_count"] == 1
    assert s["video"]["state"] == "ended" and s["video"]["present"]
    assert s["video"]["duration"] and s["peak_process_count"] > 0 and s["peak_memory_mb"]
    assert find_lab_processes(f"{os.getpid()}:{sid}") == []
    # nothing restarts after the end: the page was opened exactly once
    import time
    time.sleep(3)
    s = _by_id(server.get("/api/state").json(), sid)
    assert s["status"] == "stopped" and s["page_load_count"] == 1 and fake_youtube == ["AAAAAAAAAAA"]


def test_youtube_autoplay_next_video_ends_run(server, fake_youtube):
    sid = yt_create(server, YT + "&next=1").json()[0]["id"]
    server.post(f"/api/sessions/{sid}/start")
    st = _wait(server, lambda st: _by_id(st, sid)["status"] in ("stopped", "error"), timeout=60)
    s = _by_id(st, sid)
    assert s["status"] == "stopped" and s["page_load_count"] == 1, s
    assert find_lab_processes(f"{os.getpid()}:{sid}") == []


def test_youtube_cooldown_start_all_and_second_start(server, fake_youtube, monkeypatch):
    import app.main as main
    sid = yt_create(server).json()[0]["id"]
    local = create(server, f"{LOCAL}/lab/basic", count=3).json()
    # start-all never starts the YouTube session, but still starts the others
    r = server.post("/api/sessions/start-all").json()
    assert sid not in r["started"] and len(r["started"]) == 3
    assert any(x["id"] == sid and "YouTube" in x["reason"] for x in r["skipped"])
    server.post("/api/sessions/stop-all")

    monkeypatch.setattr(main.manager, "youtube_cooldown_s", 600)
    assert server.post(f"/api/sessions/{sid}/start").status_code == 200
    # second start (manual or restart) while running / within the cooldown is refused
    r2 = server.post(f"/api/sessions/{sid}/restart")
    assert r2.status_code == 429 and "분에 한 번" in r2.json()["detail"], r2.text
    assert _by_id(server.get("/api/state").json(), sid)["status"] in ("starting", "running")  # not stopped by it
    st = _wait(server, lambda st: _by_id(st, sid)["status"] in ("stopped", "error"), timeout=60)
    assert server.post(f"/api/sessions/{sid}/start").status_code == 429
    assert all(_by_id(st, x["id"])["status"] != "running" for x in local)


def test_youtube_stop_cleans_processes(server, fake_youtube):
    import time
    sid = yt_create(server).json()[0]["id"]
    server.post(f"/api/sessions/{sid}/start")
    _wait(server, lambda st: _by_id(st, sid)["status"] == "running", timeout=30)
    time.sleep(0.5)
    r = server.post("/api/sessions/stop-all").json()
    assert r["verified_clean"] and r["remaining_processes"] == 0
    assert find_lab_processes(f"{os.getpid()}:{sid}") == []


def test_localhost_multi_session_still_allowed(server):
    r = create(server, f"{LOCAL}/lab/video", mode="video", count=5, loop=True)
    assert r.status_code == 201 and len(r.json()) == 5
    r = server.post("/api/sessions/start-all").json()
    assert len(r["started"]) == 5
    server.post("/api/sessions/stop-all")


@pytest.mark.parametrize("url", ["https://www.twitch.tv/x", "https://www.tiktok.com/@a/video/1",
                                 "https://chzzk.naver.com/live/x", "https://www.instagram.com/reel/x"])
def test_other_platforms_still_blocked(server, url):
    for count in (1, 3):
        r = create(server, url, mode="video", count=count, loop=False)
        assert r.status_code == 400 and "third-party media/social platform" in r.json()["detail"]
