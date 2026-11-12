"""Runtime configuration. All values may be overridden with LAB_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent


def load_env_file(path: Path) -> dict[str, str]:
    """Load ``LAB_*=value`` lines from a machine settings file into os.environ.

    Values already present in the environment win, so a one-off
    ``LAB_MAX_CONCURRENT=10 ./scripts/dev.sh`` still overrides the file. Only LAB_*
    keys are accepted; blank lines and ``#`` comments are ignored. Returns what was applied.
    """
    applied: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return applied
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("#", 1)[0].strip().strip('"').strip("'")
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key.startswith("LAB_") or key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


# Per-machine settings (e.g. the dedicated Mac mini). Override path with LAB_ENV_FILE.
ENV_FILE = Path(os.environ.get("LAB_ENV_FILE", str(PROJECT_DIR / "lab.env")))
ENV_FILE_APPLIED = load_env_file(ENV_FILE)


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
    # Refuse new browsers while OS memory pressure is at/above this level
    # ("warn" or "critical"; "off" disables). macOS: kern.memorystatus_vm_pressure_level.
    pressure_block_level: str = os.environ.get("LAB_PRESSURE_BLOCK_LEVEL", "warn").lower()

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

    # ---- custom URL scenario (see app/custom_target.py and README "Custom URL")
    custom_allowed_hosts: str = os.environ.get("LAB_CUSTOM_ALLOWED_HOSTS", "")
    custom_resource_hosts: str = os.environ.get("LAB_CUSTOM_RESOURCE_HOSTS", "")
    custom_allow_private_network: bool = os.environ.get("LAB_CUSTOM_ALLOW_PRIVATE_NETWORK", "0") == "1"
    custom_require_verification: bool = os.environ.get("LAB_CUSTOM_REQUIRE_VERIFICATION", "1") != "0"
    custom_host_overrides: str = os.environ.get("LAB_CUSTOM_HOST_OVERRIDES", "")
    # Bounds for user-chosen reload interval (seconds)
    custom_min_reload_s: float = _env_float("LAB_CUSTOM_MIN_RELOAD_S", 2.0)

    cors_origins: tuple[str, ...] = tuple(
        o.strip()
        for o in os.environ.get(
            "LAB_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",")
        if o.strip()
    )

    @property
    def custom_policy(self):
        from .custom_target import registered_hosts
        return _policy_cache(self.custom_allowed_hosts, self.custom_resource_hosts,
                             self.custom_allow_private_network, self.custom_require_verification,
                             self.custom_host_overrides, registered_hosts(self.registered_hosts_path))

    @property
    def registered_hosts_path(self) -> Path:
        """Hosts registered from the dashboard after the ownership check."""
        return self.db_path.parent / "registered_hosts.json"

    @property
    def verification_token(self) -> str:
        from .custom_target import load_or_create_token
        return load_or_create_token(self.db_path.parent / "verification_token.txt")

    @property
    def test_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/lab"


from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=8)
def _policy_cache(allowed: str, resources: str, allow_private: bool, require_verification: bool, overrides: str,
                  registered: tuple[str, ...] = ()):
    from .custom_target import CustomPolicy
    return CustomPolicy.from_strings(allowed, resources, allow_private, require_verification, overrides, registered)


# Command-line marker added to every Chromium we launch, so that our own browser
# processes (and only ours) can be found and cleaned up after a crash.
PROCESS_MARKER = "--blc-lab-marker"

settings = Settings()
