"""Safety helpers: localhost-only URL policy and secret redaction for logs."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .config import settings


class URLNotAllowed(ValueError):
    pass


def is_local_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https", "ws", "wss"):
        # data:, about:, blob: etc. never leave the machine; allow them.
        return parts.scheme in ("data", "about", "blob")
    host = (parts.hostname or "").lower()
    return host in settings.allowed_hosts or f"[{host}]" in settings.allowed_hosts


def assert_local_url(url: str) -> str:
    if not is_local_url(url):
        raise URLNotAllowed(f"Only localhost targets are allowed (got {redact(url)!r})")
    return url


_SECRET_PATTERNS = [
    # key=value / key: value pairs (query strings, headers, JSON-ish)
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|access_token|refresh_token|api[_-]?key|"
        r"authorization|auth|session[_-]?id|cookie|set-cookie)\b(\s*[\"']?\s*[:=]\s*[\"']?)"
        r"((?:(?:bearer|basic)\s+)?[^\s\"'&,;]+)"
    ),
    # Bearer / Basic credentials
    re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9\-._~+/]+=*)"),
    # user:pass@host in URLs
    re.compile(r"(?i)(://)([^/\s:@]+):([^/\s@]+)@"),
]


def redact(text: str) -> str:
    if not text:
        return text
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    out = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)} [REDACTED]", out)
    out = _SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)}[REDACTED]@", out)
    return out
