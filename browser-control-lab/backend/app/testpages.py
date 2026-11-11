"""Self-hosted test pages. Test sessions may only target these (or other localhost) pages."""
from __future__ import annotations

import asyncio
import html
import random

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

router = APIRouter(prefix="/lab", tags=["test-pages"])

SCENARIOS: dict[str, dict[str, str]] = {
    "basic": {"path": "/lab/basic", "label": "Basic page (HTML + JS + local asset)"},
    "heavy": {"path": "/lab/heavy", "label": "Heavy DOM (2,000 elements)"},
    "slow": {"path": "/lab/slow?delay_ms=800", "label": "Slow server response (800 ms)"},
    "flaky": {"path": "/lab/flaky?fail_rate=0.3", "label": "Flaky (30% HTTP 500)"},
    "error": {"path": "/lab/error", "label": "Always fails (HTTP 500)"},
}


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;color:#1f2937}}
.cell{{display:inline-block;width:18px;height:18px;margin:1px;background:#c7d2fe}}</style>
<script src="/lab/asset.js" defer></script></head>
<body><h1 id="title">{html.escape(title)}</h1>{body}
<p>Session storage value: <code id="sid">(none)</code></p></body></html>"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    items = "".join(
        f'<li><a href="{s["path"]}">{html.escape(s["label"])}</a> &mdash; <code>{k}</code></li>'
        for k, s in SCENARIOS.items()
    )
    return _page("Local Browser Control Lab - test pages", f"<ul>{items}</ul>")


@router.get("/asset.js")
async def asset() -> Response:
    js = """
document.addEventListener('DOMContentLoaded', () => {
  const v = localStorage.getItem('lab_sid');
  const el = document.getElementById('sid');
  if (el) el.textContent = v || '(none)';
  document.body.dataset.ready = '1';
});
"""
    return Response(js, media_type="application/javascript")


@router.get("/basic", response_class=HTMLResponse)
async def basic() -> str:
    return _page("Basic test page", "<p>This page is served by the lab backend on localhost.</p>")


@router.get("/heavy", response_class=HTMLResponse)
async def heavy() -> str:
    cells = '<span class="cell"></span>' * 2000
    return _page("Heavy DOM test page", f"<div>{cells}</div>")


@router.get("/slow", response_class=HTMLResponse)
async def slow(delay_ms: int = 800) -> str:
    await asyncio.sleep(max(0, min(delay_ms, 10_000)) / 1000)
    return _page("Slow test page", f"<p>Delayed by {int(delay_ms)} ms.</p>")


@router.get("/flaky")
async def flaky(fail_rate: float = 0.3) -> Response:
    if random.random() < max(0.0, min(fail_rate, 1.0)):
        return PlainTextResponse("Simulated failure", status_code=500)
    return HTMLResponse(_page("Flaky test page", "<p>Lucky this time.</p>"))


@router.get("/error")
async def error() -> Response:
    return PlainTextResponse("Simulated server error", status_code=500)


@router.get("/whoami")
async def whoami(request: Request) -> JSONResponse:
    """Echo the lab cookie so a session can verify its cookie jar is isolated."""
    return JSONResponse({"lab_sid": request.cookies.get("lab_sid")})
