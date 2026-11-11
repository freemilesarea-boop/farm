"""End-to-end API tests with real headless Chromium sessions against the local test pages."""
import threading
import time

import pytest
import uvicorn

from app.config import settings
from app.main import app, db
from app.procs import find_lab_processes
import httpx
import os

BASE = f"http://127.0.0.1:{settings.port}"


@pytest.fixture(scope="module")
def server():
    config = uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="warning")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/api/health", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    client = httpx.Client(base_url=BASE, timeout=60)
    yield client
    client.delete("/api/sessions")
    srv.should_exit = True
    t.join(timeout=30)
    assert find_lab_processes(str(os.getpid())) == [], "Chromium processes left after shutdown"


@pytest.fixture(autouse=True)
def _clean(request):
    yield
    if "server" in request.fixturenames:
        client = request.getfixturevalue("server")
        client.post("/api/sessions/stop-all")
        client.delete("/api/sessions")


def _wait(client, pred, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get("/api/state").json()
        if pred(st):
            return st
        time.sleep(0.3)
    raise AssertionError(f"condition not met; last state summary={st['summary']}")


def _by_id(state, sid):
    return next(s for s in state["sessions"] if s["id"] == sid)


def test_test_pages(server):
    assert server.get("/lab/basic").status_code == 200
    assert server.get("/lab/error").status_code == 500
    assert httpx.get(f"{BASE}/lab/whoami", headers={"cookie": "lab_sid=abc"}).json() == {"lab_sid": "abc"}


def test_session_lifecycle_and_cleanup(server):
    sid = server.post("/api/sessions", json={"count": 1, "scenario": "basic"}).json()[0]["id"]
    assert server.post(f"/api/sessions/{sid}/start").status_code == 200
    st = _wait(server, lambda st: _by_id(st, sid)["iterations"] >= 2 and _by_id(st, sid)["isolation_ok"])
    s = _by_id(st, sid)
    assert s["status"] == "running" and s["errors"] == 0 and s["avg_load_ms"] > 0
    assert find_lab_processes(f"{os.getpid()}:{sid}")

    # restart gives a fresh browser
    assert server.post(f"/api/sessions/{sid}/restart").json()["status"] in ("starting", "running")
    _wait(server, lambda st: _by_id(st, sid)["status"] == "running")

    server.post(f"/api/sessions/{sid}/stop")
    st = server.get("/api/state").json()
    assert _by_id(st, sid)["status"] == "stopped"
    assert find_lab_processes(f"{os.getpid()}:{sid}") == []
    server.delete(f"/api/sessions/{sid}")


def test_ten_sessions_independent_control(server):
    ids = [s["id"] for s in server.post("/api/sessions", json={"count": 10, "scenario": "basic"}).json()]
    res = server.post("/api/sessions/start-all").json()
    assert set(ids) <= set(res["started"]), res
    st = _wait(server, lambda st: all(_by_id(st, i)["status"] == "running" and _by_id(st, i)["isolation_ok"] is not None
                                      for i in ids), timeout=60)
    assert all(_by_id(st, i)["isolation_ok"] for i in ids)

    # stopping one session does not affect the others
    server.post(f"/api/sessions/{ids[0]}/stop")
    st = server.get("/api/state").json()
    assert _by_id(st, ids[0])["status"] == "stopped"
    assert all(_by_id(st, i)["status"] == "running" for i in ids[1:])
    assert find_lab_processes(f"{os.getpid()}:{ids[0]}") == []
    assert all(find_lab_processes(f"{os.getpid()}:{i}") for i in ids[1:])

    out = server.post("/api/sessions/stop-all").json()
    assert set(out["stopped"]) == set(ids[1:])
    assert find_lab_processes(str(os.getpid())) == []
    st = server.get("/api/state").json()
    assert st["system"]["lab_process_count"] == 0
    for i in ids:
        server.delete(f"/api/sessions/{i}")


def test_error_session_recorded(server):
    sid = server.post("/api/sessions", json={"count": 1, "scenario": "error"}).json()[0]["id"]
    server.post(f"/api/sessions/{sid}/start")
    st = _wait(server, lambda st: _by_id(st, sid)["status"] == "error", timeout=40)
    s = _by_id(st, sid)
    assert s["errors"] >= 5 and "HTTP 500" in s["last_error"]
    assert st["summary"]["error"] >= 1
    errs = db.recent_logs(session_id=sid, level="error")
    assert any("HTTP 500" in e["message"] for e in errs)
    assert find_lab_processes(f"{os.getpid()}:{sid}") == []
    server.delete(f"/api/sessions/{sid}")


def test_concurrency_limit_blocks_new_starts(server):
    limit = settings.max_concurrent
    ids = [s["id"] for s in server.post("/api/sessions", json={"count": limit + 2}).json()]
    res = server.post("/api/sessions/start-all").json()
    assert len(res["started"]) == limit
    assert len(res["skipped"]) == 2 and "concurrency limit" in res["skipped"][0]["reason"]
    r = server.post(f"/api/sessions/{res['skipped'][0]['id']}/start")
    assert r.status_code == 429
    server.post("/api/sessions/stop-all")
    server.delete("/api/sessions")
    assert find_lab_processes(str(os.getpid())) == []


def test_resource_threshold_blocks(server, monkeypatch):
    from app.main import manager
    sid = server.post("/api/sessions", json={"count": 1}).json()[0]["id"]
    import dataclasses
    import app.session_manager as sm
    monkeypatch.setattr(sm, "settings", dataclasses.replace(settings, cpu_threshold=80.0))
    monkeypatch.setattr(manager.monitor, "sample", lambda: None)
    monkeypatch.setattr(manager.monitor, "cpu_percent", 95.0)
    r = server.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 429 and "CPU" in r.json()["detail"]
    # RAM threshold
    monkeypatch.setattr(manager.monitor, "cpu_percent", 0.0)
    monkeypatch.setattr(sm, "settings", dataclasses.replace(settings, ram_threshold=0.1))
    r = server.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 429 and "RAM" in r.json()["detail"]


def test_max_sessions_registration(server):
    assert server.post("/api/sessions", json={"count": 100}).status_code == 201
    assert len(server.get("/api/sessions").json()) == 100
    r = server.post("/api/sessions", json={"count": 1})
    assert r.status_code == 409
    server.delete("/api/sessions")


def test_scenario_must_be_whitelisted(server):
    r = server.post("/api/sessions", json={"count": 1, "scenario": "https://example.com"})
    assert r.status_code == 422


def test_stop_all_verifies_no_processes_remain(server):
    ids = [s["id"] for s in server.post("/api/sessions", json={"count": 3}).json()]
    server.post("/api/sessions/start-all")
    _wait(server, lambda st: all(_by_id(st, i)["process_count"] > 0 for i in ids))
    procs = server.get("/api/processes").json()
    assert procs["count"] >= 3 and procs["tracked_pids"] >= procs["count"] - 3
    out = server.post("/api/sessions/stop-all").json()
    assert out["verified_clean"] is True and out["remaining_processes"] == 0
    assert server.get("/api/processes").json()["count"] == 0
    assert find_lab_processes(str(os.getpid())) == []


def test_memory_fields_in_state(server):
    sid = server.post("/api/sessions", json={"count": 1}).json()[0]["id"]
    server.post(f"/api/sessions/{sid}/start")
    st = _wait(server, lambda st: _by_id(st, sid)["memory_mb"] is not None)
    s = _by_id(st, sid)
    assert 0 < s["memory_mb"] <= s["memory_rss_mb"] and s["memory_method"] in ("uss", "uss+rss", "rss")
    sysd = st["system"]
    assert sysd["lab_memory"]["uss_mb"] > 0 and sysd["lab_memory"]["rss_mb"] >= sysd["lab_memory"]["uss_mb"]
    mem = sysd["memory"]
    assert mem["total_gb"] > 0 and mem["pressure"]["level"] in ("normal", "warn", "critical", "unknown")
    assert {"total_gb", "used_gb", "percent", "in_mb_s", "out_mb_s"} <= set(mem["swap"])


def test_register_replace_prevents_accumulation(server):
    server.post("/api/sessions", json={"count": 4})
    r = server.post("/api/sessions", json={"count": 2, "replace": True})
    assert r.status_code == 201 and len(r.json()) == 2
    assert server.get("/api/state").json()["summary"]["total"] == 2
    # default is still additive
    server.post("/api/sessions", json={"count": 1})
    assert server.get("/api/state").json()["summary"]["total"] == 3


def test_replace_stops_running_sessions(server):
    sid = server.post("/api/sessions", json={"count": 1}).json()[0]["id"]
    server.post(f"/api/sessions/{sid}/start")
    _wait(server, lambda st: _by_id(st, sid)["status"] == "running")
    server.post("/api/sessions", json={"count": 1, "replace": True})
    assert find_lab_processes(f"{os.getpid()}:{sid}") == []
    assert all(s["id"] != sid for s in server.get("/api/state").json()["sessions"])


def test_memory_pressure_blocks(server, monkeypatch):
    import dataclasses
    import app.session_manager as sm
    from app.main import manager
    sid = server.post("/api/sessions", json={"count": 1}).json()[0]["id"]
    monkeypatch.setattr(manager.monitor, "refresh_memory", lambda: None)
    monkeypatch.setattr(manager.monitor.memory, "pressure", {"level": "warn"})
    r = server.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 429 and "pressure" in r.json()["detail"]
    monkeypatch.setattr(sm, "settings", dataclasses.replace(settings, pressure_block_level="critical"))
    assert server.post(f"/api/sessions/{sid}/start").status_code == 200
