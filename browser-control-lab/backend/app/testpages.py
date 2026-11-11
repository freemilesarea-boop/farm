"""Self-hosted test pages. Test sessions may only target these (or other localhost) pages."""
from __future__ import annotations

import asyncio
import html
import random

from fastapi import APIRouter, Request, Response
from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

STATIC = Path(__file__).resolve().parent / "static"

router = APIRouter(prefix="/lab", tags=["test-pages"])

SCENARIOS: dict[str, dict[str, str]] = {
    "basic": {"path": "/lab/basic", "label": "Basic page (HTML + JS + local asset)"},
    "heavy": {"path": "/lab/heavy", "label": "Heavy DOM (2,000 elements)"},
    "slow": {"path": "/lab/slow?delay_ms=800", "label": "Slow server response (800 ms)"},
    "flaky": {"path": "/lab/flaky?fail_rate=0.3", "label": "Flaky (30% HTTP 500)"},
    "error": {"path": "/lab/error", "label": "Always fails (HTTP 500)"},
}

# Extra local pages (not scenarios) used by the custom-URL feature and its tests.
LOCAL_EXTRA_PAGES = {
    "/lab/video": "HTML5 <video> page (2 s WebM) for Video Playback mode",
    "/lab/redirect-local": "302 -> /lab/basic (same host)",
    "/lab/redirect-external": "302 -> an external host (must be blocked)",
    "/lab/redirect-ip": "302 -> a cloud-metadata IP literal (must be blocked)",
    "/lab/static/test-2s.webm": "Direct video file (WebM/VP9)",
    "/lab/static/test-2s-h264.mp4": "Direct video file (MP4 H.264/AAC)",
    "/lab/static/test-2s-vp9.mp4": "Direct video file (MP4 VP9)",
    "/lab/redirect-video": "302 -> /lab/static/test-2s.webm (same host)",
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
    extra = "".join(f'<li><a href="{p}">{p}</a> &mdash; {html.escape(d)}</li>' for p, d in LOCAL_EXTRA_PAGES.items())
    return _page("Local Browser Control Lab - test pages", f"<ul>{items}</ul><h2>Custom URL test pages</h2><ul>{extra}</ul>")


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


_MEDIA = {"test-2s.webm": "video/webm", "test-2s-h264.mp4": "video/mp4", "test-2s-vp9.mp4": "video/mp4",
          "broken.mp4": "video/mp4"}  # broken.mp4: random bytes, for playback-error tests


@router.get("/static/{name}")
async def test_media(name: str) -> Response:
    if name not in _MEDIA:
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(STATIC / name, media_type=_MEDIA[name])


@router.get("/redirect-video")
async def redirect_video() -> RedirectResponse:
    return RedirectResponse("/lab/static/test-2s.webm", status_code=302)


@router.get("/player", response_class=HTMLResponse)
async def player(src: str = "") -> Response:
    """Internal wrapper for *direct video file* URLs: a page whose only content is a
    <video> pointing at ``src``. ``src`` must pass the same custom-URL policy as any
    session target (checked again here, and the browser still enforces it per request).
    The URL is injected as JSON (never as raw HTML), so it cannot break out of the page."""
    import json

    from .config import settings
    from .custom_target import CustomURLError
    try:
        target = settings.custom_policy.validate(src, resolve=False)
    except CustomURLError as exc:
        return HTMLResponse(_page("Player: URL not allowed", f"<p id='err'>{html.escape(str(exc))}</p>"),
                            status_code=400)
    src_json = json.dumps(target.url).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Lab player</title>
<style>body{{margin:0;background:#111;color:#ddd;font:13px system-ui}} video{{width:100%;max-height:90vh}}
#src{{padding:6px 10px;word-break:break-all}}</style></head>
<body><video id="v" controls playsinline preload="auto"></video><div id="src"></div>
<script>const s={src_json};const v=document.getElementById('v');v.src=s;
document.getElementById('src').textContent='Direct video: '+s;</script></body></html>""")


@router.get("/video", response_class=HTMLResponse)
async def video_page(autoplay: int = 0) -> str:
    auto = " autoplay muted" if autoplay else ""
    return _page("Video test page",
                 f'<video id="v" src="/lab/static/test-2s.webm" width="320" height="180" '
                 f'controls playsinline preload="auto"{auto}></video>')


@router.get("/redirect-local")
async def redirect_local() -> RedirectResponse:
    return RedirectResponse("/lab/basic", status_code=302)


@router.get("/redirect-external")
async def redirect_external() -> RedirectResponse:
    # Fixed target (not an open redirect): used to prove redirect hops leaving the
    # allowlist are blocked.
    return RedirectResponse("http://blocked-by-lab.example/", status_code=302)


@router.get("/redirect-ip")
async def redirect_ip() -> RedirectResponse:
    # Fixed SSRF probe target (IP literal: not covered by DNS pinning) for tests.
    return RedirectResponse("http://169.254.169.254/latest/meta-data/", status_code=302)
