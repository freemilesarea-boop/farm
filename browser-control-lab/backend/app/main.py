"""FastAPI entry point: REST API + WebSocket for the admin dashboard, plus local test pages."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import settings
from .db import Database
from .session_manager import LimitError, SessionManager
from .custom_target import VERIFY_PATH, CustomURLError
from .security import URLNotAllowed
from .testpages import LOCAL_EXTRA_PAGES, SCENARIOS
from .testpages import router as testpages_router

Scenario = Literal["basic", "heavy", "slow", "flaky", "error", "custom"]


class CustomTarget(BaseModel):
    """User-supplied target. Always validated by the backend (custom_target.CustomPolicy)."""
    url: str = Field(..., min_length=1, max_length=2048)
    mode: Literal["page_load", "video"] = "page_load"
    reload_interval_s: float = Field(10.0, le=3600)
    muted: bool = True
    loop: bool = True


class CreateSessions(BaseModel):
    count: int = Field(1, ge=1, le=100)
    scenario: Scenario = "basic"
    name_prefix: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_\-]*$")
    # True: stop and delete every existing session first, so the lab holds exactly `count`.
    replace: bool = False
    # Required when scenario == "custom"; ignored otherwise.
    custom: CustomTarget | None = None


class ValidateURL(BaseModel):
    url: str = Field(..., max_length=4096)


def _validated_custom(body: CreateSessions) -> dict | None:
    if body.scenario != "custom":
        return None
    if body.custom is None:
        raise HTTPException(status_code=400, detail="scenario 'custom' requires a 'custom' object with a URL")
    try:
        target = settings.custom_policy.validate(body.custom.url)
    except CustomURLError as exc:
        raise HTTPException(status_code=400, detail=f"URL not allowed: {exc}")
    interval = max(settings.custom_min_reload_s, body.custom.reload_interval_s)
    # Snapshot of the form at registration time. Later edits in the UI never change it.
    return {"url": target.input_url, "nav_url": target.url, "kind": target.media_kind,
            "mode": body.custom.mode, "reload_interval_s": interval,
            "muted": body.custom.muted, "loop": body.custom.loop, "registered_at": time.time()}


db = Database(settings.db_path)
manager = SessionManager(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.startup()
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="Local Browser Control Lab", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(testpages_router)


def _not_found(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc).strip("'\""))


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    return {
        "max_sessions": settings.max_sessions,
        "max_concurrent": settings.max_concurrent,
        "cpu_threshold": settings.cpu_threshold,
        "ram_threshold": settings.ram_threshold,
        "pressure_block_level": settings.pressure_block_level,
        "iteration_interval": settings.iteration_interval,
        "scenarios": SCENARIOS,
        "custom": {
            **settings.custom_policy.to_dict(),
            "min_reload_s": settings.custom_min_reload_s,
            "verification": {"path": VERIFY_PATH, "token": settings.verification_token},
            "local_test_pages": LOCAL_EXTRA_PAGES,
        },
    }


@app.post("/api/custom/probe")
async def probe_custom_url(body: ValidateURL):
    """"URL 확인": validate, then open the URL once in a short-lived Chromium under the same
    network policy and report final URL, status, content type and HTML5 video details."""
    try:
        return await manager.probe(body.url)
    except CustomURLError as exc:
        return {"input_url": body.url.strip(), "allowed": False, "blocked_reason": str(exc)}
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@app.post("/api/custom/validate")
async def validate_custom_url(body: ValidateURL):
    """Backend check of a custom URL (same rules the session uses). Never fetches the URL."""
    try:
        t = settings.custom_policy.validate(body.url)
    except CustomURLError as exc:
        raise HTTPException(status_code=400, detail=f"URL not allowed: {exc}")
    return {"ok": True, "url": t.url, "host": t.host, "local": t.local, "resolved_ips": t.ips,
            "verification_required": (not t.local) and settings.custom_policy.require_verification,
            "verify_url": None if t.local else t.verify_url}


@app.get("/api/state")
async def state():
    return manager.snapshot()


@app.get("/api/sessions")
async def list_sessions():
    return [s.to_dict() for s in manager.sessions.values()]


@app.post("/api/sessions", status_code=201)
async def create_sessions(body: CreateSessions):
    custom = _validated_custom(body)  # validate before touching existing sessions
    if body.replace:
        await manager.stop_all(reason="replace")
        for sid in list(manager.sessions):
            await manager.delete_session(sid)
    try:
        created = manager.create_sessions(body.count, body.scenario, body.name_prefix, custom=custom)
    except LimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return [s.to_dict() for s in created]


@app.delete("/api/sessions/{sid}", status_code=204)
async def delete_session(sid: str):
    try:
        await manager.delete_session(sid)
    except KeyError as exc:
        raise _not_found(exc)


@app.delete("/api/sessions", status_code=200)
async def delete_all_sessions():
    ids = list(manager.sessions)
    for sid in ids:
        await manager.delete_session(sid)
    return {"deleted": len(ids)}


async def _control(action: str, sid: str):
    try:
        fn = {"start": manager.start, "stop": manager.stop, "restart": manager.restart}[action]
        s = await fn(sid)
    except KeyError as exc:
        raise _not_found(exc)
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except (CustomURLError, URLNotAllowed) as exc:
        raise HTTPException(status_code=400, detail=f"URL not allowed: {exc}")
    return s.to_dict()


@app.post("/api/sessions/{sid}/start")
async def start_session(sid: str):
    return await _control("start", sid)


@app.post("/api/sessions/{sid}/stop")
async def stop_session(sid: str):
    return await _control("stop", sid)


@app.post("/api/sessions/{sid}/restart")
async def restart_session(sid: str):
    return await _control("restart", sid)


@app.post("/api/sessions/start-all")
async def start_all():
    return await manager.start_all()


@app.post("/api/sessions/stop-all")
async def stop_all():
    return await manager.stop_all()


@app.get("/api/processes")
async def processes():
    """All live browser processes owned by this server (marker search + tracked PIDs)."""
    return await asyncio.to_thread(manager.process_report)


@app.get("/api/logs")
async def logs(limit: int = 200, session_id: str | None = None, level: str | None = None):
    return db.recent_logs(limit=min(limit, 1000), session_id=session_id, level=level)


@app.get("/api/runs")
async def runs(limit: int = 100, session_id: str | None = None):
    return db.list_runs(limit=min(limit, 1000), session_id=session_id)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(manager.snapshot())
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        pass


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
