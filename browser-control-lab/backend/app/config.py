"""Runtime configuration. All values may be overridden with LAB_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _default_max_concurrent() -> int:
    # Rough heuristic: ~1 headless Chromium per 300 MB of RAM and 3 per CPU core,
    # whichever is lower, capped at 25. Can be raised explicitly via LAB_MAX_CONCURRENT.
    import psutil

    mem_gb = psutil.virtual_memory().total / (1024**3)
    cores = psutil.cpu_count(logical=True) or 2
    return max(1, min(25, int(mem_gb * 1024 / 300), cores * 3))


@dataclass(frozen=True)
class Settings:
    host: str = os.environ.get("LAB_HOST", "127.0.0.1")
    port: int = _env_int("LAB_PORT", 8000)
    db_path: Path = Path(os.environ.get("LAB_DB_PATH", str(BASE_DIR / "data" / "lab.sqlite3")))

    # Hard cap on *registered* sessions (spec: up to 100).
    max_sessions: int = min(100, _env_int("LAB_MAX_SESSIONS", 100))
    # Cap on *simultaneously running* browsers; depends on the machine.
    max_concurrent: int = _env_int("LAB_MAX_CONCURRENT", 0) or _default_max_concurrent()

    # New sessions are refused while the system is above these thresholds.
    cpu_threshold: float = _env_float("LAB_CPU_THRESHOLD", 85.0)
    ram_threshold: float = _env_float("LAB_RAM_THRESHOLD", 85.0)

    # Seconds between test iterations inside one session.
    iteration_interval: float = _env_float("LAB_ITERATION_INTERVAL", 3.0)
    page_timeout_ms: int = _env_int("LAB_PAGE_TIMEOUT_MS", 15000)

    # Optional explicit Chromium binary (otherwise Playwright's managed Chromium is used).
    chromium_executable: str | None = os.environ.get("LAB_CHROMIUM_EXECUTABLE") or None
    headless: bool = os.environ.get("LAB_HEADLESS", "1") != "0"

    # Only these hosts may ever be loaded by a test session.
    allowed_hosts: frozenset[str] = field(
        default_factory=lambda: frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
    )

    cors_origins: tuple[str, ...] = tuple(
        o.strip()
        for o in os.environ.get(
            "LAB_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",")
        if o.strip()
    )

    @property
    def test_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/lab"


# Command-line marker added to every Chromium we launch, so that our own browser
# processes (and only ours) can be found and cleaned up after a crash.
PROCESS_MARKER = "--blc-lab-marker"

settings = Settings()
