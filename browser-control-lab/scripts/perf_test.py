"""Staged performance test: how CPU / RAM / browser memory scale with concurrent sessions.

Runs against a running lab backend (localhost only). For every stage it registers N
sessions (replacing any previous ones), starts them, holds for --hold seconds while
sampling once per second, then stops everything and *verifies* that no browser process
is left. A 0-session baseline is measured first.

    backend/.venv/bin/python scripts/perf_test.py                    # 1 -> 5 -> 10
    backend/.venv/bin/python scripts/perf_test.py --stages 1,2,4,6,8  # custom
    backend/.venv/bin/python scripts/perf_test.py --max 25            # also try 25 if headroom

Stages beyond 10 (and any stage while memory pressure is not 'normal') only run if the
previous stage leaves headroom. Output: reports/perf-<ts>.{md,json,csv,html}.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import platform
import statistics
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

API = "http://127.0.0.1:8000"
EXTRA_STAGES = [25, 50, 100]
PROJECTED_CPU_LIMIT = 75.0
PROJECTED_RAM_LIMIT = 75.0
LEVELS = {"unknown": -1, "normal": 0, "warn": 1, "critical": 2}


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def state():
    return call("GET", "/api/state")


def sample(st: dict) -> dict:
    s, m, lab = st["system"], st["system"]["memory"], st["system"]["lab_memory"]
    return {
        "t": round(st["ts"], 1),
        "running": st["summary"]["running"],
        "cpu": s["cpu_percent"],
        "ram_used_pct": m["used_percent"],
        "ram_psutil_pct": m["psutil_percent"],
        "ram_used_gb": m["used_gb"],
        "lab_uss_mb": lab["uss_mb"],
        "lab_rss_mb": lab["rss_mb"],
        "procs": s["lab_process_count"],
        "pressure": m["pressure"]["level"],
        "swap_used_gb": m["swap"]["used_gb"],
        "swap_in_mb_s": m["swap"]["in_mb_s"],
        "swap_out_mb_s": m["swap"]["out_mb_s"],
    }


def hold(seconds: float) -> list[dict]:
    out, t0 = [], time.time()
    while time.time() - t0 < seconds:
        out.append(sample(state()))
        time.sleep(1)
    return out


def agg(series: list[dict], key: str, fn=statistics.mean) -> float:
    vals = [x[key] for x in series if x[key] is not None]
    return round(fn(vals), 2) if vals else 0.0


def worst_pressure(series: list[dict]) -> str:
    return max((x["pressure"] for x in series), key=lambda l: LEVELS.get(l, -1), default="unknown")


def run_stage(n: int, hold_s: float, scenario: str) -> dict:
    call("POST", "/api/sessions", {"count": n, "scenario": scenario, "replace": True})
    t0 = time.time()
    res = call("POST", "/api/sessions/start-all")
    started = len(res["started"])
    deadline = t0 + max(60, n * 5)
    while time.time() < deadline:
        st = state()
        if st["summary"]["running"] + st["summary"]["error"] >= started:
            break
        time.sleep(0.5)
    ramp_s = time.time() - t0
    swap_before = sample(state())["swap_used_gb"]
    series = hold(hold_s)
    st = state()
    sess = st["sessions"]
    loads = sorted(s["avg_load_ms"] for s in sess if s["avg_load_ms"] is not None)
    per_uss = [s["memory_mb"] for s in sess if s["memory_mb"]]
    per_rss = [s["memory_rss_mb"] for s in sess if s["memory_rss_mb"]]

    t1 = time.time()
    stop = call("POST", "/api/sessions/stop-all")
    stop_s = time.time() - t1
    time.sleep(1)
    procs_after = call("GET", "/api/processes")["count"]
    call("DELETE", "/api/sessions")

    return {
        "sessions": n,
        "started": started,
        "blocked": len(res["skipped"]),
        "blocked_reason": res["skipped"][0]["reason"] if res["skipped"] else None,
        "running_at_end": sum(1 for s in sess if s["status"] == "running"),
        "error_sessions": sum(1 for s in sess if s["status"] == "error"),
        "ramp_up_s": round(ramp_s, 1),
        "cpu_avg": agg(series, "cpu"),
        "cpu_max": agg(series, "cpu", max),
        "ram_used_pct_avg": agg(series, "ram_used_pct"),
        "ram_used_pct_max": agg(series, "ram_used_pct", max),
        "ram_psutil_pct_avg": agg(series, "ram_psutil_pct"),
        "ram_used_gb_avg": agg(series, "ram_used_gb"),
        "lab_uss_mb_avg": agg(series, "lab_uss_mb"),
        "lab_rss_mb_avg": agg(series, "lab_rss_mb"),
        "uss_mb_per_session": round(statistics.mean(per_uss), 1) if per_uss else None,
        "rss_mb_per_session": round(statistics.mean(per_rss), 1) if per_rss else None,
        "memory_method": "/".join(sorted({s["memory_method"] for s in sess if s["memory_method"]})) or None,
        "chromium_procs": int(agg(series, "procs", max)),
        "pressure_worst": worst_pressure(series),
        "swap_used_gb_delta": round(agg(series, "swap_used_gb", max) - swap_before, 2),
        "swap_out_mb_s_max": agg(series, "swap_out_mb_s", max),
        "iterations": sum(s["iterations"] for s in sess),
        "errors": sum(s["errors"] for s in sess),
        "isolation_ok": all(s["isolation_ok"] for s in sess if s["status"] == "running"),
        "load_ms_avg": round(statistics.mean(loads), 1) if loads else None,
        "load_ms_p95": round(loads[max(0, int(len(loads) * 0.95) - 1)], 1) if loads else None,
        "stop_all_s": round(stop_s, 1),
        "stop_verified_clean": stop.get("verified_clean"),
        "leftover_processes": procs_after,
        "series": series,
    }


def headroom(prev: dict, base: dict, nxt: int, cfg: dict) -> tuple[bool, str]:
    scale = nxt / prev["sessions"]
    p_cpu = base["cpu_avg"] + max(0.0, prev["cpu_avg"] - base["cpu_avg"]) * scale
    p_ram = base["ram_used_pct_avg"] + max(0.0, prev["ram_used_pct_avg"] - base["ram_used_pct_avg"]) * scale
    reasons = []
    if p_cpu >= PROJECTED_CPU_LIMIT:
        reasons.append(f"projected CPU {p_cpu:.0f}%")
    if p_ram >= min(PROJECTED_RAM_LIMIT, cfg["ram_threshold"]):
        reasons.append(f"projected RAM {p_ram:.0f}%")
    if LEVELS.get(prev["pressure_worst"], -1) >= 1:
        reasons.append(f"memory pressure was '{prev['pressure_worst']}'")
    if prev["swap_used_gb_delta"] > 0.25:
        reasons.append(f"swap grew {prev['swap_used_gb_delta']} GB")
    if prev["errors"]:
        reasons.append(f"{prev['errors']} errors")
    why = f"projected CPU {p_cpu:.0f}% / RAM {p_ram:.0f}% for {nxt} sessions"
    return (not reasons), (why if not reasons else "; ".join(reasons))


def capacity(base: dict, results: list[dict], cfg: dict, total_gb: float) -> dict:
    """Least-squares slope of RAM-used and CPU vs. sessions -> rough max sessions."""
    pts = [(0, base["ram_used_gb_avg"], base["cpu_avg"])] + \
          [(r["started"], r["ram_used_gb_avg"], r["cpu_avg"]) for r in results if r["started"]]
    if len(pts) < 2:
        return {}
    xs = [p[0] for p in pts]
    mx = statistics.mean(xs)
    den = sum((x - mx) ** 2 for x in xs) or 1

    def slope(i):
        my = statistics.mean(p[i] for p in pts)
        return sum((p[0] - mx) * (p[i] - my) for p in pts) / den

    gb_per, cpu_per = slope(1), slope(2)
    ram_cap = (cfg["ram_threshold"] / 100 * total_gb - base["ram_used_gb_avg"]) / gb_per if gb_per > 0 else None
    cpu_cap = (cfg["cpu_threshold"] - base["cpu_avg"]) / cpu_per if cpu_per > 0 else None
    return {
        "ram_gb_per_session": round(gb_per, 3),
        "cpu_pct_per_session": round(cpu_per, 2),
        "est_max_sessions_by_ram": int(ram_cap) if ram_cap else None,
        "est_max_sessions_by_cpu": int(cpu_cap) if cpu_cap else None,
    }


# ----------------------------------------------------------------------------- output
COLS = [("sessions", "세션"), ("started", "시작"), ("cpu_avg", "CPU 평균%"), ("cpu_max", "CPU 최대%"),
        ("ram_used_pct_avg", "RAM 사용%"), ("ram_psutil_pct_avg", "RAM psutil%"), ("ram_used_gb_avg", "RAM GB"),
        ("lab_uss_mb_avg", "브라우저 USS MB"), ("lab_rss_mb_avg", "브라우저 RSS MB"),
        ("uss_mb_per_session", "세션당 USS"), ("chromium_procs", "프로세스"), ("pressure_worst", "메모리 압력"),
        ("swap_used_gb_delta", "Swap 증가 GB"), ("load_ms_avg", "평균 로딩ms"), ("errors", "오류"),
        ("stop_all_s", "중지 s"), ("leftover_processes", "잔여 프로세스")]


def svg_chart(rows: list[dict], keys: list[tuple[str, str, str]], title: str, unit: str) -> str:
    W, H, L, B, T, R = 560, 260, 48, 32, 28, 16
    xs = [r["sessions"] for r in rows]
    vmax = max([max(r[k] or 0 for r in rows) for k, _, _ in keys] + [1]) * 1.1
    xmax = max(xs) or 1
    px = lambda x: L + (W - L - R) * x / xmax
    py = lambda v: H - B - (H - B - T) * (v or 0) / vmax
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(title)}">',
             f'<text x="{L}" y="18" class="t">{html.escape(title)}</text>']
    for i in range(5):
        v = vmax * i / 4
        parts.append(f'<line x1="{L}" x2="{W-R}" y1="{py(v):.1f}" y2="{py(v):.1f}" class="g"/>'
                     f'<text x="{L-6}" y="{py(v)+4:.1f}" class="a" text-anchor="end">{v:.0f}</text>')
    for x in xs:
        parts.append(f'<text x="{px(x):.1f}" y="{H-B+16}" class="a" text-anchor="middle">{x}</text>')
    parts.append(f'<text x="{W-R}" y="{H-4}" class="a" text-anchor="end">세션 수 · {html.escape(unit)}</text>')
    for k, label, color in keys:
        pts = " ".join(f"{px(r['sessions']):.1f},{py(r[k]):.1f}" for r in rows)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
        for r in rows:
            parts.append(f'<circle cx="{px(r["sessions"]):.1f}" cy="{py(r[k]):.1f}" r="3" fill="{color}">'
                         f'<title>{label}: {r[k]} ({r["sessions"]} sessions)</title></circle>')
    legend = "".join(f'<span><i style="background:{c}"></i>{html.escape(l)}</span>' for _, l, c in keys)
    return "<div>" + "".join(parts) + "</svg>" + f'<div class="lg">{legend}</div></div>'


def write_reports(out: Path, ts: str, meta: dict, base: dict, results: list[dict], notes: list[str], cap: dict):
    rows = [{**base, "sessions": 0, "started": 0}] + results
    slim = [{k: v for k, v in r.items() if k != "series"} for r in rows]
    (out / f"perf-{ts}.json").write_text(json.dumps(
        {"meta": meta, "baseline": base, "results": results, "capacity": cap, "notes": notes},
        indent=2, ensure_ascii=False))

    with open(out / f"perf-{ts}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(slim[-1].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(slim)

    md = [f"# 성능 테스트 {ts}", "",
          f"- 플랫폼: `{meta['platform']}` · CPU {meta['cpu_count']}코어 · RAM {meta['ram_total_gb']} GB",
          f"- 설정: `{meta['config']}`",
          f"- 단계당 유지 {meta['hold_s']}초, 시나리오 `{meta['scenario']}`, 세션 0 = 유휴 기준선", "",
          "| " + " | ".join(h for _, h in COLS) + " |", "|" + "---|" * len(COLS)]
    md += ["| " + " | ".join("—" if r.get(k) is None else str(r.get(k)) for k, _ in COLS) + " |" for r in slim]
    if cap:
        md += ["", "## 용량 추정 (선형 근사, 참고용)",
               f"- 세션 1개당 RAM 증가: **{cap['ram_gb_per_session']} GB**, CPU 증가: **{cap['cpu_pct_per_session']}%p**",
               f"- RAM 임계치({meta['config']['ram_threshold']}%) 기준 최대 약 **{cap['est_max_sessions_by_ram']}개**, "
               f"CPU 임계치({meta['config']['cpu_threshold']}%) 기준 최대 약 **{cap['est_max_sessions_by_cpu']}개**",
               "- 실제 한도는 둘 중 작은 값이며, 메모리 압력이 'warn'이 되면 그 전에 차단됩니다."]
    md += ["", "## 지표 설명",
           "- **RAM 사용%**: OS 기준 사용량. macOS는 Activity Monitor 방식(App+Wired+Compressed, `vm_stat`), Linux는 total−available.",
           "- **RAM psutil%**: psutil `virtual_memory().percent`. macOS에서는 파일 캐시 일부를 사용으로 계산해 더 높게 나옵니다.",
           "- **브라우저 USS**: 테스트 Chromium 프로세스들의 고유 메모리 합(종료 시 회수되는 양). "
           "**RSS**는 공유 메모리를 프로세스마다 중복 계산해 실제보다 큽니다.",
           "- **메모리 압력**: macOS `kern.memorystatus_vm_pressure_level`, Linux PSI. 단계 중 최악 값.",
           "- **잔여 프로세스**: 전체 중지 후 `/api/processes`(마커 검색 + 추적 PID)로 확인한 수. 0이어야 정상."]
    if notes:
        md += ["", "## 진행 기록", *[f"- {n}" for n in notes]]
    (out / f"perf-{ts}.md").write_text("\n".join(md) + "\n")

    table = "".join(
        "<tr>" + "".join(f"<td>{html.escape('—' if r.get(k) is None else str(r.get(k)))}</td>" for k, _ in COLS) + "</tr>"
        for r in slim)
    charts = svg_chart(slim, [("cpu_avg", "CPU 평균 %", "#2563eb"), ("ram_used_pct_avg", "RAM 사용 %", "#16a34a"),
                              ("ram_psutil_pct_avg", "RAM psutil %", "#a3a3a3")], "CPU · RAM (%)", "%")
    charts += svg_chart(slim, [("lab_uss_mb_avg", "브라우저 USS 합계 MB", "#9333ea"),
                               ("lab_rss_mb_avg", "브라우저 RSS 합계 MB", "#f59e0b")], "테스트 브라우저 메모리 (MB)", "MB")
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>성능 테스트 {ts}</title>
<style>:root{{color-scheme:light dark;--fg:#0f172a;--mut:#64748b;--bg:#fff;--grid:#e2e8f0}}
@media (prefers-color-scheme:dark){{:root{{--fg:#e2e8f0;--mut:#94a3b8;--bg:#0b1220;--grid:#1e293b}}}}
body{{font-family:system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px;color:var(--fg);background:var(--bg)}}
table{{border-collapse:collapse;font-size:12px;width:100%;display:block;overflow-x:auto}}
td,th{{border-bottom:1px solid var(--grid);padding:4px 6px;text-align:right;white-space:nowrap}}
svg{{width:100%;max-width:560px}} .t{{font-size:13px;fill:var(--fg);font-weight:600}} .a{{font-size:10px;fill:var(--mut)}}
.g{{stroke:var(--grid)}} .lg{{font-size:12px;color:var(--mut);display:flex;gap:12px;margin:-4px 0 16px}}
.lg i{{display:inline-block;width:10px;height:3px;margin-right:4px;vertical-align:middle}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}</style></head><body>
<h1>성능 테스트 {ts}</h1><p>{html.escape(meta['platform'])} · {meta['cpu_count']}코어 · {meta['ram_total_gb']} GB ·
유지 {meta['hold_s']}s · 시나리오 {html.escape(meta['scenario'])}</p>
<div class="charts">{charts}</div>
<table><thead><tr>{''.join(f'<th>{html.escape(h)}</th>' for _, h in COLS)}</tr></thead><tbody>{table}</tbody></table>
<p>{'<br>'.join(html.escape(n) for n in notes)}</p></body></html>"""
    (out / f"perf-{ts}.html").write_text(page)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="1,5,10", help="comma-separated session counts (default 1,5,10)")
    ap.add_argument("--hold", type=float, default=20, help="seconds to observe each stage")
    ap.add_argument("--max", type=int, default=10, help="also try 25/50/100 up to this, if headroom allows")
    ap.add_argument("--scenario", default="basic")
    args = ap.parse_args()

    cfg = call("GET", "/api/config")
    st = state()
    if st["summary"]["total"]:
        print("Lab is not empty; this test deletes all sessions. Stop/delete them first "
              "(대시보드 '전체 삭제'). Aborting.", file=sys.stderr)
        return 2

    stages = sorted({int(x) for x in args.stages.split(",") if x.strip()})
    stages += [s for s in EXTRA_STAGES if s <= args.max and s > max(stages)]
    print("--- baseline (0 sessions) ---", flush=True)
    base_series = hold(min(10.0, args.hold))
    base = {k: v for k, v in {
        "cpu_avg": agg(base_series, "cpu"), "cpu_max": agg(base_series, "cpu", max),
        "ram_used_pct_avg": agg(base_series, "ram_used_pct"), "ram_used_pct_max": agg(base_series, "ram_used_pct", max),
        "ram_psutil_pct_avg": agg(base_series, "ram_psutil_pct"), "ram_used_gb_avg": agg(base_series, "ram_used_gb"),
        "lab_uss_mb_avg": 0.0, "lab_rss_mb_avg": 0.0, "chromium_procs": 0,
        "pressure_worst": worst_pressure(base_series), "swap_used_gb_delta": 0.0, "errors": 0,
        "leftover_processes": 0, "series": base_series}.items()}
    print(json.dumps({k: v for k, v in base.items() if k != "series"}, ensure_ascii=False))

    results, notes = [], []
    for n in stages:
        if n > cfg["max_concurrent"]:
            notes.append(f"{n}개 단계 생략: 동시 실행 한도 {cfg['max_concurrent']} 초과 (LAB_MAX_CONCURRENT로 조정)")
            print(notes[-1])
            break
        if results and (n > 10 or LEVELS.get(results[-1]["pressure_worst"], -1) >= 1):
            ok, why = headroom(results[-1], base, n, cfg)
            notes.append(f"{n}개 단계 {'진행' if ok else '생략'}: {why}")
            print(notes[-1])
            if not ok:
                break
        print(f"--- stage {n} sessions ---", flush=True)
        r = run_stage(n, args.hold, args.scenario)
        results.append(r)
        print(json.dumps({k: v for k, v in r.items() if k != "series"}, ensure_ascii=False), flush=True)
        if r["leftover_processes"] or not r["stop_verified_clean"]:
            notes.append(f"{n}개 단계: 전체 중지 후 프로세스 {r['leftover_processes']}개 잔존!")
        if r["blocked"]:
            notes.append(f"{n}개 단계: {r['blocked']}개 실행 차단 — {r['blocked_reason']}")

    mem = st["system"]["memory"]
    meta = {"timestamp": datetime.now().strftime("%Y%m%d-%H%M%S"), "platform": platform.platform(),
            "cpu_count": st["system"]["cpu_count"], "ram_total_gb": mem["total_gb"],
            "memory_method": mem["method"],
            "config": {k: cfg[k] for k in ("max_concurrent", "cpu_threshold", "ram_threshold",
                                           "pressure_block_level", "iteration_interval")},
            "hold_s": args.hold, "scenario": args.scenario}
    cap = capacity(base, results, cfg, mem["total_gb"])
    out = Path(__file__).resolve().parent.parent / "reports"
    out.mkdir(exist_ok=True)
    write_reports(out, meta["timestamp"], meta, base, results, notes, cap)
    print(f"\nReport: reports/perf-{meta['timestamp']}.md  (.html .csv .json)")
    clean = all(r["leftover_processes"] == 0 and r["stop_verified_clean"] for r in results)
    print("Cleanup after every stage:", "OK (0 processes left)" if clean else "FAILED")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
