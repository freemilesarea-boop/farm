"use client";

import { useMemo, useState } from "react";
import { api, LabState, LogEntry, PressureLevel, Scenario, Session, SessionStatus } from "@/lib/api";
import { Connection, useLabState } from "@/lib/useLabState";

const SCENARIOS: { value: Scenario; label: string }[] = [
  { value: "basic", label: "basic — 기본 페이지" },
  { value: "heavy", label: "heavy — DOM 2,000개" },
  { value: "slow", label: "slow — 800ms 지연" },
  { value: "flaky", label: "flaky — 30% 실패" },
  { value: "error", label: "error — 항상 500" },
];

const STATUS_STYLE: Record<SessionStatus, string> = {
  idle: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  stopped: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  starting: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200",
  stopping: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200",
  running: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200",
  error: "bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-200",
};

const STATUS_LABEL: Record<SessionStatus, string> = {
  idle: "대기",
  stopped: "중지됨",
  starting: "시작 중",
  stopping: "중지 중",
  running: "실행 중",
  error: "오류",
};

function fmtDuration(s: number): string {
  if (!s) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

function fmtMs(v: number | null): string {
  return v == null ? "—" : `${Math.round(v)} ms`;
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("ko-KR", { hour12: false });
}

export default function Dashboard() {
  const { state, connection } = useLabState();
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  async function run(label: string, fn: () => Promise<unknown>, okText?: (r: any) => string) {
    setBusy(label);
    try {
      const r = await fn();
      if (okText) setNotice({ kind: "ok", text: okText(r) });
    } catch (e) {
      setNotice({ kind: "error", text: (e as Error).message });
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Local Browser Control Lab</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            로컬 테스트 페이지 전용 Playwright Chromium 세션 관리자 (localhost 외 요청 차단)
          </p>
        </div>
        <ConnectionBadge connection={connection} />
      </header>

      {notice && (
        <div
          role="status"
          className={`mb-4 flex items-start justify-between gap-4 rounded-lg border px-4 py-2 text-sm ${
            notice.kind === "ok"
              ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
              : "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200"
          }`}
        >
          <span>{notice.text}</span>
          <button className="opacity-60 hover:opacity-100" onClick={() => setNotice(null)} aria-label="닫기">
            ✕
          </button>
        </div>
      )}

      {!state ? (
        <div className="rounded-xl border border-dashed border-slate-300 p-10 text-center text-slate-500 dark:border-slate-700">
          백엔드에 연결하는 중… (FastAPI 서버가 실행 중인지 확인하세요)
        </div>
      ) : (
        <>
          <Stats state={state} />
          <MemoryPanel state={state} />
          <Controls state={state} busy={busy} run={run} />
          <SessionTable sessions={state.sessions} busy={busy} run={run} />
          <Logs logs={state.logs} sessions={state.sessions} />
        </>
      )}
    </main>
  );
}

function ConnectionBadge({ connection }: { connection: Connection }) {
  const map: Record<Connection, [string, string]> = {
    live: ["실시간 연결", "bg-emerald-500"],
    polling: ["폴링 (WebSocket 재연결 중)", "bg-amber-500"],
    connecting: ["연결 중", "bg-slate-400"],
    offline: ["백엔드 연결 끊김", "bg-rose-500"],
  };
  const [label, dot] = map[connection];
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium dark:border-slate-800 dark:bg-slate-900">
      <span className={`h-2 w-2 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 ${className}`}>
      {children}
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: string; tone?: string }) {
  return (
    <Card>
      <div className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</div>
      <div className={`mt-1 text-3xl font-semibold tabular-nums ${tone ?? ""}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{sub}</div>}
    </Card>
  );
}

function Meter({ label, percent, threshold, detail }: { label: string; percent: number; threshold: number; detail: string }) {
  const over = percent >= threshold;
  const near = percent >= threshold * 0.8;
  const bar = over ? "bg-rose-500" : near ? "bg-amber-500" : "bg-sky-500";
  return (
    <Card>
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</span>
        <span className="text-xs text-slate-500 dark:text-slate-400">임계치 {threshold}%</span>
      </div>
      <div className={`mt-1 text-3xl font-semibold tabular-nums ${over ? "text-rose-600 dark:text-rose-400" : ""}`}>
        {percent.toFixed(1)}%
      </div>
      <div className="relative mt-2 h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
        <div className={`h-full ${bar} transition-all`} style={{ width: `${Math.min(100, percent)}%` }} />
        <div className="absolute inset-y-0 w-px bg-slate-900/40 dark:bg-white/50" style={{ left: `${Math.min(100, threshold)}%` }} />
      </div>
      <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{detail}</div>
    </Card>
  );
}

function Stats({ state }: { state: LabState }) {
  const { summary: s, system } = state;
  return (
    <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      <Stat label="전체 세션" value={s.total} sub={`최대 등록 ${s.max_sessions}`} />
      <Stat
        label="실행 중"
        value={s.running}
        tone="text-emerald-600 dark:text-emerald-400"
        sub={`동시 실행 한도 ${s.max_concurrent}${s.starting ? ` · 시작 중 ${s.starting}` : ""}`}
      />
      <Stat label="오류" value={s.error} tone={s.error ? "text-rose-600 dark:text-rose-400" : ""} sub="오류로 종료된 세션" />
      <Stat label="Chromium 프로세스" value={system.lab_process_count} sub="이 랩이 띄운 프로세스 수" />
      <Meter label="CPU" percent={system.cpu_percent} threshold={s.cpu_threshold} detail={`${system.cpu_count} 코어 · 3초 평균`} />
      <Meter
        label="시스템 RAM"
        percent={system.memory.used_percent}
        threshold={s.ram_threshold}
        detail={`${system.memory.used_gb} / ${system.memory.total_gb} GB · psutil ${system.memory.psutil_percent}%`}
      />
    </section>
  );
}

const PRESSURE_STYLE: Record<PressureLevel, [string, string]> = {
  normal: ["정상", "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200"],
  warn: ["경고", "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200"],
  critical: ["위험", "bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-200"],
  unknown: ["측정 불가", "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300"],
};

const BREAKDOWN: Record<string, [string, string]> = {
  used: ["사용", "bg-sky-500"],
  app: ["앱", "bg-sky-500"],
  wired: ["와이어드", "bg-violet-500"],
  compressed: ["압축", "bg-amber-500"],
  cached: ["캐시된 파일", "bg-slate-300 dark:bg-slate-600"],
  free: ["여유", "bg-slate-100 dark:bg-slate-800"],
};

function fmtGb(v: number) {
  return `${v.toFixed(v < 10 ? 2 : 1)} GB`;
}

function MemoryPanel({ state }: { state: LabState }) {
  const m = state.system.memory;
  const lab = state.system.lab_memory;
  const [plabel, pstyle] = PRESSURE_STYLE[m.pressure.level] ?? PRESSURE_STYLE.unknown;
  const parts = Object.entries(m.breakdown_gb).filter(([k]) => k in BREAKDOWN);
  const partsTotal = Math.max(m.total_gb, parts.reduce((a, [, v]) => a + v, 0));
  const otherGb = Math.max(0, m.used_gb - lab.uss_mb / 1024);
  const isMac = m.method.startsWith("vm_stat");
  return (
    <section className="mb-6 grid gap-3 lg:grid-cols-3">
      <Card>
        <div className="flex items-baseline justify-between">
          <h3 className="text-xs font-medium text-slate-500 dark:text-slate-400">시스템 메모리 구성</h3>
          <span className="text-xs text-slate-500 dark:text-slate-400" title={m.method}>
            {isMac ? "Activity Monitor 방식" : "total − available"}
          </span>
        </div>
        <div className="mt-2 flex h-3 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800" aria-label="메모리 구성 막대">
          {parts.map(([k, v]) => (
            <div key={k} className={BREAKDOWN[k][1]} style={{ width: `${(100 * v) / partsTotal}%` }} title={`${BREAKDOWN[k][0]} ${fmtGb(v)}`} />
          ))}
        </div>
        <ul className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
          {parts.map(([k, v]) => (
            <li key={k} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5">
                <i className={`inline-block h-2 w-2 rounded-sm ${BREAKDOWN[k][1]}`} />
                {BREAKDOWN[k][0]}
              </span>
              <span className="tabular-nums text-slate-600 dark:text-slate-300">{fmtGb(v)}</span>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-[11px] leading-snug text-slate-500 dark:text-slate-400">
          사용 = {isMac ? "앱 + 와이어드 + 압축" : "전체 − 사용 가능"} = <b>{fmtGb(m.used_gb)}</b> ({m.used_percent}%). psutil 값 {m.psutil_percent}%는
          {isMac ? " 캐시 일부를 사용으로 계산해 더 높게 나옵니다." : " 비교용입니다."} 차단 기준은 사용 %입니다.
        </p>
      </Card>

      <Card>
        <div className="flex items-baseline justify-between">
          <h3 className="text-xs font-medium text-slate-500 dark:text-slate-400">테스트 브라우저 메모리</h3>
          <span className="text-xs text-slate-500 dark:text-slate-400">{state.system.lab_process_count}개 프로세스</span>
        </div>
        <div className="mt-1 text-3xl font-semibold tabular-nums">
          {lab.uss_mb >= 1024 ? `${(lab.uss_mb / 1024).toFixed(2)} GB` : `${Math.round(lab.uss_mb)} MB`}
          <span className="ml-2 text-sm font-normal text-slate-500">전체 RAM의 {lab.percent_of_total}%</span>
        </div>
        <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
          <dt className="text-slate-500">USS 합계 (종료 시 회수)</dt>
          <dd className="text-right tabular-nums">{Math.round(lab.uss_mb)} MB</dd>
          <dt className="text-slate-500" title="공유 라이브러리를 프로세스마다 중복 계산">RSS 합계 (공유 포함·과대)</dt>
          <dd className="text-right tabular-nums">{Math.round(lab.rss_mb)} MB</dd>
          <dt className="text-slate-500">나머지 시스템 사용</dt>
          <dd className="text-right tabular-nums">{fmtGb(otherGb)}</dd>
          <dt className="text-slate-500">백엔드 서버 RSS</dt>
          <dd className="text-right tabular-nums">{lab.backend_rss_mb ?? "—"} MB</dd>
        </dl>
        {lab.method && lab.method !== "uss" && (
          <p className="mt-2 text-[11px] text-amber-700 dark:text-amber-400">
            일부 프로세스의 USS를 읽지 못해 RSS로 대체했습니다({lab.method}). 이 경우 값이 실제보다 큽니다.
          </p>
        )}
      </Card>

      <Card>
        <div className="flex items-baseline justify-between">
          <h3 className="text-xs font-medium text-slate-500 dark:text-slate-400">메모리 압력 · Swap</h3>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            차단 기준: {state.summary.pressure_block_level === "off" ? "사용 안 함" : `'${state.summary.pressure_block_level}' 이상`}
          </span>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <span className={`rounded-full px-3 py-1 text-sm font-semibold ${pstyle}`}>{plabel}</span>
          <span className="text-[11px] text-slate-500" title={m.pressure.source}>
            {m.pressure.source === "kern.memorystatus_vm_pressure_level" ? "macOS 메모리 압력" : m.pressure.source === "/proc/pressure/memory" ? `Linux PSI ${m.pressure.value ?? 0}%` : m.pressure.source}
          </span>
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
          <dt className="text-slate-500">Swap 사용</dt>
          <dd className="text-right tabular-nums">
            {m.swap.total_gb ? `${fmtGb(m.swap.used_gb)} / ${fmtGb(m.swap.total_gb)}` : "Swap 없음"}
          </dd>
          <dt className="text-slate-500">Swap in / out</dt>
          <dd className={`text-right tabular-nums ${m.swap.out_mb_s > 1 ? "text-amber-600 dark:text-amber-400" : ""}`}>
            {m.swap.in_mb_s} / {m.swap.out_mb_s} MB/s
          </dd>
          <dt className="text-slate-500">압축 메모리</dt>
          <dd className="text-right tabular-nums">{m.breakdown_gb.compressed != null ? fmtGb(m.breakdown_gb.compressed) : "—"}</dd>
        </dl>
        <p className="mt-2 text-[11px] leading-snug text-slate-500 dark:text-slate-400">
          압력이 &lsquo;경고&rsquo;로 바뀌거나 Swap out이 계속 늘면 세션 수를 줄이세요.
        </p>
      </Card>
    </section>
  );
}

type Runner = (label: string, fn: () => Promise<unknown>, okText?: (r: any) => string) => Promise<void>;

function Controls({ state, busy, run }: { state: LabState; busy: string | null; run: Runner }) {
  const [count, setCount] = useState(6);
  const [scenario, setScenario] = useState<Scenario>("basic");
  const [confirming, setConfirming] = useState(false);
  const existing = state.summary.total;
  const running = state.summary.running + state.summary.starting;
  const remaining = state.summary.max_sessions - existing;
  const anyActive = state.sessions.some((s) => ["starting", "running", "stopping"].includes(s.status));
  const limit = state.summary.max_concurrent;

  const register = (replace: boolean) =>
    run(
      "create",
      () => api("/api/sessions", { method: "POST", body: JSON.stringify({ count, scenario, replace }) }),
      (r) => (replace ? `기존 ${existing}개를 삭제하고 ${r.length}개로 교체했습니다` : `${r.length}개 추가 등록 (총 ${existing + r.length}개)`),
    ).then(() => setConfirming(false));

  return (
    <Card className="mb-6">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-slate-500 dark:text-slate-400">
          세션 수
          <input
            type="number"
            min={1}
            max={100}
            value={count}
            onChange={(e) => {
              setCount(Math.max(1, Math.min(100, Number(e.target.value) || 1)));
              setConfirming(false);
            }}
            className="w-24 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-slate-500 dark:text-slate-400">
          테스트 시나리오 (로컬 페이지)
          <select
            value={scenario}
            onChange={(e) => setScenario(e.target.value as Scenario)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
          >
            {SCENARIOS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <Btn disabled={!!busy || confirming} onClick={() => (existing === 0 ? register(false) : setConfirming(true))}>
          세션 등록
        </Btn>
        <div className="mx-1 hidden h-8 w-px bg-slate-200 sm:block dark:bg-slate-800" />
        <Btn
          variant="primary"
          disabled={!!busy || state.sessions.length === 0}
          onClick={() =>
            run("start-all", () => api("/api/sessions/start-all", { method: "POST" }), (r) =>
              r.skipped.length
                ? `${r.started.length}개 시작, ${r.skipped.length}개 차단 — ${r.skipped[0].reason}`
                : `${r.started.length}개 시작`,
            )
          }
        >
          {busy === "start-all" ? "시작 중…" : "전체 시작"}
        </Btn>
        <Btn
          variant="danger"
          disabled={!!busy || !anyActive}
          onClick={() =>
            run("stop-all", () => api("/api/sessions/stop-all", { method: "POST" }), (r) =>
              r.verified_clean
                ? `${r.stopped.length}개 중지 · 브라우저 프로세스 0개 확인`
                : `${r.stopped.length}개 중지 · 경고: 프로세스 ${r.remaining_processes}개가 남아 있습니다`,
            )
          }
        >
          {busy === "stop-all" ? "중지 중…" : "전체 중지"}
        </Btn>
        <Btn
          disabled={!!busy || state.sessions.length === 0}
          onClick={() => {
            if (confirm("모든 세션을 삭제할까요? 실행 중인 세션은 먼저 중지됩니다.")) {
              run("delete-all", () => api("/api/sessions", { method: "DELETE" }), (r) => `${r.deleted}개 삭제`);
            }
          }}
        >
          전체 삭제
        </Btn>
        <div className="ml-auto text-right text-xs text-slate-500 dark:text-slate-400">
          <div>
            현재 등록 <b className="text-slate-900 dark:text-slate-100">{existing}</b>개 · 실행 {running}개 · 동시 실행 한도 {limit}
          </div>
          {state.summary.admission_blocked_reason ? (
            <div className="font-medium text-amber-700 dark:text-amber-400">신규 실행 차단: {state.summary.admission_blocked_reason}</div>
          ) : (
            <div>신규 실행 가능</div>
          )}
        </div>
      </div>

      {confirming && (
        <div
          role="alertdialog"
          aria-label="세션 등록 방식 선택"
          className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100"
        >
          <p className="font-medium">
            이미 {existing}개 세션이 등록되어 있습니다{running ? ` (실행 중 ${running}개)` : ""}. {count}개를 어떻게 등록할까요?
          </p>
          <ul className="mt-1 list-disc pl-5 text-xs">
            <li>
              <b>교체</b>: 기존 세션을 모두 중지·삭제하고 정확히 {count}개만 남깁니다.
            </li>
            <li>
              <b>추가</b>: 기존 {existing}개에 더해 총 {existing + count}개가 됩니다.
              {existing + count > limit && ` 동시 실행 한도(${limit})를 넘으므로 '전체 시작' 시 ${limit}개까지만 실행됩니다.`}
              {existing + count > state.summary.max_sessions && ` 최대 등록 수(${state.summary.max_sessions})를 넘어 등록할 수 없습니다.`}
            </li>
          </ul>
          <div className="mt-2 flex flex-wrap gap-2">
            <Btn variant="primary" disabled={!!busy} onClick={() => register(true)}>
              기존 {existing}개 교체 → {count}개
            </Btn>
            <Btn disabled={!!busy || count > remaining} onClick={() => register(false)}>
              {count}개 추가 → 총 {existing + count}개
            </Btn>
            <Btn disabled={!!busy} onClick={() => setConfirming(false)}>
              취소
            </Btn>
          </div>
        </div>
      )}
    </Card>
  );
}

function Btn({
  children,
  onClick,
  disabled,
  variant = "default",
  small,
  title,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  variant?: "default" | "primary" | "danger";
  small?: boolean;
  title?: string;
}) {
  const styles = {
    default:
      "border border-slate-300 bg-white text-slate-800 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:hover:bg-slate-800",
    primary: "bg-sky-600 text-white hover:bg-sky-700",
    danger: "bg-rose-600 text-white hover:bg-rose-700",
  }[variant];
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={`${styles} ${small ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm"} rounded-md font-medium transition disabled:cursor-not-allowed disabled:opacity-40`}
    >
      {children}
    </button>
  );
}

function SessionTable({ sessions, busy, run }: { sessions: Session[]; busy: string | null; run: Runner }) {
  const [filter, setFilter] = useState<"all" | SessionStatus>("all");
  const shown = useMemo(
    () => (filter === "all" ? sessions : sessions.filter((s) => (filter === "stopped" ? ["stopped", "idle"].includes(s.status) : s.status === filter))),
    [sessions, filter],
  );

  const act = (sid: string, action: "start" | "stop" | "restart") =>
    run(`${action}:${sid}`, () => api(`/api/sessions/${sid}/${action}`, { method: "POST" }));
  const del = (sid: string) => run(`delete:${sid}`, () => api(`/api/sessions/${sid}`, { method: "DELETE" }));

  return (
    <Card className="mb-6 p-0">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
        <h2 className="text-sm font-semibold">세션 ({sessions.length})</h2>
        <div className="flex gap-1 text-xs">
          {(["all", "running", "error", "stopped"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`rounded-md px-2 py-1 ${filter === f ? "bg-slate-900 text-white dark:bg-white dark:text-slate-900" : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"}`}
            >
              {{ all: "전체", running: "실행 중", error: "오류", stopped: "대기/중지" }[f]}
            </button>
          ))}
        </div>
      </div>
      {sessions.length === 0 ? (
        <p className="px-4 py-10 text-center text-sm text-slate-500">등록된 세션이 없습니다. 위에서 세션을 등록하세요.</p>
      ) : (
        <div className="max-h-[520px] overflow-auto">
          <table className="w-full min-w-[900px] text-sm">
            <thead className="sticky top-0 bg-slate-50 text-left text-xs text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <tr>
                <th className="px-4 py-2 font-medium">세션</th>
                <th className="px-2 py-2 font-medium">상태</th>
                <th className="px-2 py-2 font-medium">시나리오</th>
                <th className="px-2 py-2 text-right font-medium">실행시간</th>
                <th className="px-2 py-2 text-right font-medium">반복</th>
                <th className="px-2 py-2 text-right font-medium">최근 로딩</th>
                <th className="px-2 py-2 text-right font-medium">평균 로딩</th>
                <th className="px-2 py-2 text-right font-medium">TTFB</th>
                <th className="px-2 py-2 text-center font-medium">격리</th>
                <th className="px-2 py-2 text-right font-medium" title="USS: 세션 종료 시 회수되는 메모리">메모리(USS)</th>
                <th className="px-2 py-2 text-right font-medium">오류</th>
                <th className="px-4 py-2 text-right font-medium">제어</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {shown.map((s) => {
                const active = ["starting", "running", "stopping"].includes(s.status);
                return (
                  <tr key={s.id} className="hover:bg-slate-50/60 dark:hover:bg-slate-800/40">
                    <td className="px-4 py-2">
                      <div className="font-medium">{s.name}</div>
                      <div className="font-mono text-[11px] text-slate-500">{s.id}</div>
                    </td>
                    <td className="px-2 py-2">
                      <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[s.status]}`} title={s.last_error ?? undefined}>
                        {STATUS_LABEL[s.status]}
                      </span>
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">{s.scenario}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{fmtDuration(s.uptime_s)}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{s.iterations}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{fmtMs(s.last_load_ms)}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{fmtMs(s.avg_load_ms)}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{fmtMs(s.last_ttfb_ms)}</td>
                    <td className="px-2 py-2 text-center">
                      {s.isolation_ok == null ? "—" : s.isolation_ok ? <span className="text-emerald-600" title="localStorage·쿠키 격리 확인">✓</span> : <span className="text-rose-600">✗</span>}
                    </td>
                    <td
                      className="px-2 py-2 text-right tabular-nums"
                      title={s.memory_rss_mb != null ? `USS ${s.memory_mb} MB · RSS ${s.memory_rss_mb} MB (${s.memory_method})` : undefined}
                    >
                      {s.memory_mb == null ? "—" : `${Math.round(s.memory_mb)} MB`}
                      {s.process_count > 0 && <div className="text-[11px] text-slate-500">{s.process_count} proc</div>}
                    </td>
                    <td className={`px-2 py-2 text-right tabular-nums ${s.errors ? "text-rose-600 dark:text-rose-400" : ""}`} title={s.last_error ?? undefined}>
                      {s.errors}
                    </td>
                    <td className="px-4 py-2">
                      <div className="flex justify-end gap-1">
                        <Btn small disabled={!!busy || active} onClick={() => act(s.id, "start")}>
                          시작
                        </Btn>
                        <Btn small disabled={!!busy || !active} onClick={() => act(s.id, "stop")}>
                          중지
                        </Btn>
                        <Btn small disabled={!!busy} onClick={() => act(s.id, "restart")}>
                          재시작
                        </Btn>
                        <Btn small disabled={!!busy} onClick={() => del(s.id)} title="삭제">
                          ✕
                        </Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Logs({ logs, sessions }: { logs: LogEntry[]; sessions: Session[] }) {
  const names = useMemo(() => Object.fromEntries(sessions.map((s) => [s.id, s.name])), [sessions]);
  const [onlyErrors, setOnlyErrors] = useState(false);
  const shown = onlyErrors ? logs.filter((l) => l.level !== "info") : logs;
  const tone = { info: "text-slate-500", warning: "text-amber-600 dark:text-amber-400", error: "text-rose-600 dark:text-rose-400" };
  return (
    <Card className="p-0">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-800">
        <h2 className="text-sm font-semibold">최근 실행 로그</h2>
        <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
          <input type="checkbox" checked={onlyErrors} onChange={(e) => setOnlyErrors(e.target.checked)} />
          경고·오류만
        </label>
      </div>
      <ul className="max-h-80 overflow-auto font-mono text-xs">
        {shown.length === 0 && <li className="px-4 py-6 text-center text-slate-500">로그 없음</li>}
        {shown.map((l) => (
          <li key={l.id} className="flex gap-3 border-b border-slate-100 px-4 py-1.5 last:border-0 dark:border-slate-800">
            <span className="shrink-0 text-slate-400">{fmtTime(l.ts)}</span>
            <span className={`w-14 shrink-0 uppercase ${tone[l.level]}`}>{l.level}</span>
            <span className="w-24 shrink-0 truncate text-slate-500">{l.session_id ? names[l.session_id] ?? l.session_id : "system"}</span>
            <span className="break-all">{l.message}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
