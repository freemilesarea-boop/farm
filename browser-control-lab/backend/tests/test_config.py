"""lab.env loading and the protection settings that must survive a higher concurrency limit."""
import dataclasses
import importlib
import os

from app import config
from app.config import load_env_file


def test_env_file_loading(tmp_path, monkeypatch):
    for k in ("LAB_MAX_CONCURRENT", "LAB_X_ONLY_IN_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAB_RAM_THRESHOLD", "70")  # already set in the shell
    f = tmp_path / "lab.env"
    f.write_text("# comment\n\nLAB_MAX_CONCURRENT=50  # inline\nexport LAB_X_ONLY_IN_FILE='a'\n"
                 "LAB_RAM_THRESHOLD=99\nPATH=/evil\nnot a line\n")
    applied = load_env_file(f)
    assert applied == {"LAB_MAX_CONCURRENT": "50", "LAB_X_ONLY_IN_FILE": "a"}
    assert os.environ["LAB_MAX_CONCURRENT"] == "50"
    assert os.environ["LAB_RAM_THRESHOLD"] == "70"  # shell wins over file
    assert os.environ["PATH"] != "/evil"            # non-LAB_ keys ignored
    monkeypatch.delenv("LAB_X_ONLY_IN_FILE")


def test_missing_env_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_settings_with_lab_env_50(tmp_path, monkeypatch):
    """Simulate the Mac mini: lab.env sets 50; nothing else changes."""
    f = tmp_path / "lab.env"
    f.write_text("LAB_MAX_CONCURRENT=50\n")
    for k in ("LAB_MAX_CONCURRENT", "LAB_MAX_SESSIONS", "LAB_RAM_THRESHOLD", "LAB_PRESSURE_BLOCK_LEVEL",
              "LAB_CPU_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAB_ENV_FILE", str(f))
    try:
        cfg = importlib.reload(config)
        s = cfg.settings
        assert s.max_concurrent == 50
        assert s.max_sessions == 100
        assert s.ram_threshold == 85.0 and s.cpu_threshold == 85.0
        assert s.pressure_block_level == "warn"
        assert "127.0.0.1" in s.allowed_hosts and "localhost" in s.allowed_hosts
    finally:
        monkeypatch.undo()
        importlib.reload(config)  # restore the suite's pinned settings


def test_shell_overrides_lab_env(tmp_path, monkeypatch):
    f = tmp_path / "lab.env"
    f.write_text("LAB_MAX_CONCURRENT=50\n")
    monkeypatch.setenv("LAB_ENV_FILE", str(f))
    monkeypatch.setenv("LAB_MAX_CONCURRENT", "7")
    try:
        assert importlib.reload(config).settings.max_concurrent == 7
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_guards_still_block_with_limit_50(monkeypatch):
    """With 50 allowed and almost nothing running, RAM >= 85% or pressure >= warn must still block."""
    import app.session_manager as sm
    from app.sysmem import MemorySnapshot

    s50 = dataclasses.replace(sm.settings, max_concurrent=50, cpu_threshold=85.0)
    monkeypatch.setattr(sm, "settings", s50)
    mgr = sm.SessionManager.__new__(sm.SessionManager)  # no Playwright needed
    mgr.sessions = {}
    mgr.monitor = sm.SystemMonitor()
    monkeypatch.setattr(mgr.monitor, "refresh_memory", lambda: None)
    mgr.monitor.cpu_percent = 10.0
    gb = 1024**3

    mgr.monitor.memory = MemorySnapshot(total=16 * gb, used=int(8 * gb), pressure={"level": "normal"})
    assert mgr.admission_check() is None                           # 50% RAM, normal -> allowed

    mgr.monitor.memory = MemorySnapshot(total=16 * gb, used=int(13.7 * gb), pressure={"level": "normal"})
    assert "RAM 86% >= threshold 85%" in mgr.admission_check()     # 85.6% -> blocked

    mgr.monitor.memory = MemorySnapshot(total=16 * gb, used=int(8 * gb), pressure={"level": "warn"})
    assert "pressure 'warn'" in mgr.admission_check()              # warn -> blocked

    mgr.monitor.memory = MemorySnapshot(total=16 * gb, used=int(8 * gb), pressure={"level": "critical"})
    assert "pressure 'critical'" in mgr.admission_check()          # critical -> blocked


def test_suite_is_isolated_from_lab_env_and_shell():
    """Even with lab.env at 100 (or LAB_MAX_CONCURRENT=100 in the shell), the suite runs at 10,
    so no test can launch more than ~12 real browsers."""
    import app.session_manager as sm
    assert sm.settings.max_concurrent == 10
    assert sm.settings.max_sessions == 100


def test_repo_lab_env_value():
    from pathlib import Path
    values = {}
    for line in (Path(config.PROJECT_DIR) / "lab.env").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    assert values == {"LAB_MAX_CONCURRENT": "100"}  # only the concurrency limit is set there
