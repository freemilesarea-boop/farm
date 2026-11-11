"""Parsers for macOS/Linux memory sources, tested with captured sample outputs."""
import subprocess
import sys
import time

import psutil

from app import sysmem
from app.procs import alive_tracked, kill_tracked, snapshot_pids

# Synthetic sample in the exact format of macOS `vm_stat` (16 KB pages, as on Apple silicon).
# Not captured from a real machine: values chosen to resemble a busy 24 GB Mac.
VM_STAT_MAC = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                5821.
Pages active:                            470115.
Pages inactive:                          463370.
Pages speculative:                         5004.
Pages throttled:                              0.
Pages wired down:                        152030.
Pages purgeable:                          10238.
"Translation faults":                1872233458.
Pages copy-on-write:                   36829478.
Pages zero filled:                    968207413.
Pages reactivated:                     45812110.
Pages purged:                           6017321.
File-backed pages:                       330544.
Anonymous pages:                         607945.
Pages stored in compressor:              663812.
Pages occupied by compressor:            172003.
Decompressions:                        88913432.
Compressions:                         111530911.
Pageins:                               16346231.
Pageouts:                                221877.
Swapins:                                3196498.
Swapouts:                               3591240.
"""


def test_parse_vm_stat():
    vm = sysmem.parse_vm_stat(VM_STAT_MAC)
    page = 16384
    assert vm["page_size"] == page
    assert vm["pages_wired_down"] == 152030 * page
    assert vm["anonymous_pages"] == 607945 * page
    assert vm["pages_occupied_by_compressor"] == 172003 * page
    assert vm["translation_faults"] == 1872233458 * page


def test_mac_breakdown_matches_activity_monitor_formula():
    total = 24 * 1024**3
    b = sysmem.mac_breakdown(sysmem.parse_vm_stat(VM_STAT_MAC), total)
    page = 16384
    assert b["app"] == (607945 - 10238) * page          # anonymous - purgeable
    assert b["wired"] == 152030 * page
    assert b["compressed"] == 172003 * page
    assert b["cached"] == (330544 + 10238) * page        # file-backed + purgeable
    assert b["used"] == b["app"] + b["wired"] + b["compressed"]
    used_gb = b["used"] / 1024**3
    assert abs(used_gb - 14.06) < 0.01  # (597707 + 152030 + 172003) pages * 16 KB


def test_pressure_levels():
    assert sysmem.mac_pressure_level(1) == "normal"
    assert sysmem.mac_pressure_level(2) == "warn"
    assert sysmem.mac_pressure_level(4) == "critical"
    assert sysmem.mac_pressure_level(None) == "unknown"
    psi = sysmem.parse_psi("some avg10=12.50 avg60=3.00 avg300=1.00 total=1\nfull avg10=0.40 avg60=0 avg300=0 total=1\n")
    assert psi == {"some_avg10": 12.5, "full_avg10": 0.4}
    assert sysmem.psi_level(psi) == "warn"
    assert sysmem.psi_level({"some_avg10": 1.0, "full_avg10": 0.0}) == "normal"
    assert sysmem.psi_level({"some_avg10": 20.0, "full_avg10": 6.0}) == "critical"
    assert sysmem.psi_level({}) == "unknown"


def test_memory_reader_and_swap_rates():
    r = sysmem.MemoryReader()
    a = r.read()
    time.sleep(0.2)
    b = r.read().to_dict()
    assert a.total > 0 and 0 < b["used_percent"] <= 100
    assert b["swap"]["in_mb_s"] >= 0 and b["swap"]["out_mb_s"] >= 0
    assert b["pressure"]["level"] in ("normal", "warn", "critical", "unknown")


def test_process_memory_uss_le_rss():
    m = sysmem.process_memory([psutil.Process()])
    assert m["method"] == "uss" and 0 < m["uss_mb"] <= m["rss_mb"]


def test_tracked_pids_killed_even_without_marker_or_parent():
    # A process that is NOT a descendant of anything we search and has no marker:
    # only the (pid, create_time) record can find it -- like a re-parented helper.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    try:
        tracked = snapshot_pids([psutil.Process(proc.pid)])
        assert alive_tracked(tracked)
        assert kill_tracked(tracked) == 1
        proc.wait(timeout=5)
        assert alive_tracked(tracked) == []
    finally:
        if proc.poll() is None:
            proc.kill()


def test_tracked_pid_reuse_guard():
    # Same PID but a different start time must never be killed.
    me = psutil.Process()
    assert alive_tracked({(me.pid, me.create_time() - 1000)}) == []
