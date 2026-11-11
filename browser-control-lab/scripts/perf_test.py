"""Staged performance test against a running lab backend: 1 -> 5 -> 10 sessions,
then 25 (and beyond, up to --max) ONLY if the previous stage left enough headroom.

    backend/.venv/bin/python scripts/perf_test.py [--hold 20] [--max 25]

Writes reports/perf-<timestamp>.json and .md. Uses only the lab's own localhost API.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

API = "http://127.0.0.1:8000"
BASE_STAGES = [1, 5, 10]
EXTRA_STAGES = [25, 50, 100]
# Headroom rule for going past 10: the previous stage's steady-state CPU and RAM,
# linearly projected to the next stage size, must stay under these limits.
PROJECTED_CPU_LIMIT = 75.0
PROJECTED_RAM_LIMIT = 75.0


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def state():
    return call("GET", "/api/state")


def run_stage(n: int, hold: float, scenario: str) -> dict:
    call("POST", "/api/sessions/stop-all")
    call("DELETE", "/api/sessions")
    base = state()["system"]
    call("POST", "/api/sessions", {"count": n, "scenario": scenario})

    t0 = time.time()
    res = call("POST", "/api/sessions/start-all")
    started = len(res["started"])
    deadline = t0 + max(60, n * 4)
    while time.time() < deadline:
        st = state()
        if st["summary"]["running"] + st["summary"]["error"] >= started:
            break
        time.sleep(0.5)
    ramp_s = time.time() - t0

    cpu, ram, procs = [], [], []
    t_hold = time.time()
    while time.time() - t_hold < hold:
        st = state()
        cpu.append(st["system"]["cpu_percent"])
        ram.append(st["system"]["ram_percent"])
        procs.append(st["system"]["lab_process_count"])
        time.sleep(1)
    st = state()
    sess = st["sessions"]
    loads = [s["avg_load_ms"] for s in sess if s["avg_load_ms"] is not None]
    mem = [s["memory_mb"] for s in sess if s["memory_mb"]]

    t1 = time.time()
    call("POST", "/api/sessions/stop-all")
    stop_s = time.time() - t1
    time.sleep(1)
    leftover = state()["system"]["lab_process_count"]
    call("DELETE", "/api/sessions")

    return {
        "sessions": n,
        "started": started,
        "blocked": len(res["skipped"]),
        "blocked_reason": res["skipped"][0]["reason"] if res["skipped"] else None,
        "running_at_end": sum(1 for s in sess if s["status"] == "running"),
        "error_sessions": sum(1 for s in sess if s["status"] == "error"),
        "ramp_up_s": round(ramp_s, 1),
        "stop_all_s": round(stop_s, 1),
        "baseline_cpu": base["cpu_percent"],
        "baseline_ram": base["ram_percent"],
        "cpu_avg": round(statistics.mean(cpu), 1),
        "cpu_max": round(max(cpu), 1),
        "ram_avg": round(statistics.mean(ram), 1),
        "ram_max": round(max(ram), 1),
        "chromium_procs": max(procs),
        "iterations": sum(s["iterations"] for s in sess),
        "errors": sum(s["errors"] for s in sess),
        "isolation_ok": all(s["isolation_ok"] for s in sess if s["status"] == "running"),
        "load_ms_avg": round(statistics.mean(loads), 1) if loads else None,
        "load_ms_p95": round(sorted(loads)[max(0, int(len(loads) * 0.95) - 1)], 1) if loads else None,
        "mem_mb_per_session": round(statistics.mean(mem), 1) if mem else None,
        "leftover_processes": leftover,
    }


def headroom_ok(prev: dict, nxt: int) -> tuple[bool, str]:
    scale = nxt / prev["sessions"]
    cpu_per = max(0.0, prev["cpu_avg"] - prev["baseline_cpu"])
    ram_per = max(0.0, prev["ram_avg"] - prev["baseline_ram"])
    p_cpu = prev["baseline_cpu"] + cpu_per * scale
    p_ram = prev["baseline_ram"] + ram_per * scale
    ok = p_cpu < PROJECTED_CPU_LIMIT and p_ram < PROJECTED_RAM_LIMIT and prev["errors"] == 0
    why = f"projected CPU {p_cpu:.0f}% / RAM {p_ram:.0f}% for {nxt} sessions (limits {PROJECTED_CPU_LIMIT:.0f}%/{PROJECTED_RAM_LIMIT:.0f}%)"
    return ok, why


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=20, help="seconds to observe each stage")
    ap.add_argument("--max", type=int, default=25, help="largest stage to attempt (25/50/100)")
    ap.add_argument("--scenario", default="basic")
    args = ap.parse_args()

    cfg = call("GET", "/api/config")
    if state()["summary"]["total"]:
        print("Lab is not empty; this test deletes all sessions. Aborting.", file=sys.stderr)
        return 2

    results, notes = [], []
    stages = BASE_STAGES + [s for s in EXTRA_STAGES if s <= args.max]
    for n in stages:
        if n > cfg["max_concurrent"]:
            notes.append(f"Skipped {n}: exceeds max_concurrent={cfg['max_concurrent']} (set LAB_MAX_CONCURRENT to raise it)")
            print(notes[-1])
            break
        if n > 10:
            ok, why = headroom_ok(results[-1], n)
            notes.append(("Running " if ok else "Skipped ") + f"{n}: {why}")
            print(notes[-1])
            if not ok:
                break
        print(f"--- stage {n} sessions ---", flush=True)
        r = run_stage(n, args.hold, args.scenario)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False))
        if r["leftover_processes"]:
            notes.append(f"Stage {n}: {r['leftover_processes']} processes left after stop-all!")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(__file__).resolve().parent.parent / "reports"
    out.mkdir(exist_ok=True)
    meta = {"timestamp": ts, "platform": platform.platform(), "python": platform.python_version(),
            "config": {k: cfg[k] for k in ("max_concurrent", "cpu_threshold", "ram_threshold", "iteration_interval")},
            "hold_s": args.hold, "scenario": args.scenario}
    (out / f"perf-{ts}.json").write_text(json.dumps({"meta": meta, "results": results, "notes": notes}, indent=2))

    cols = ["sessions", "started", "blocked", "ramp_up_s", "cpu_avg", "cpu_max", "ram_avg", "ram_max",
            "chromium_procs", "mem_mb_per_session", "iterations", "errors", "load_ms_avg", "load_ms_p95",
            "isolation_ok", "stop_all_s", "leftover_processes"]
    md = [f"# Performance test {ts}", "", f"- Platform: `{meta['platform']}`", f"- Config: `{meta['config']}`",
          f"- Hold per stage: {args.hold}s, scenario `{args.scenario}`", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in results]
    if notes:
        md += ["", "## Notes", *[f"- {n}" for n in notes]]
    (out / f"perf-{ts}.md").write_text("\n".join(md) + "\n")
    print(f"\nReport: reports/perf-{ts}.md")
    return 0 if all(r["leftover_processes"] == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
