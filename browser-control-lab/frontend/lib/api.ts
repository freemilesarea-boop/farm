export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
export const WS_URL = API_URL.replace(/^http/, "ws") + "/ws";

export type SessionStatus = "idle" | "starting" | "running" | "stopping" | "stopped" | "error";
export type Scenario = "basic" | "heavy" | "slow" | "flaky" | "error";

export interface Session {
  id: string;
  name: string;
  scenario: Scenario;
  target_url: string;
  status: SessionStatus;
  created_at: number;
  started_at: number | null;
  uptime_s: number;
  iterations: number;
  errors: number;
  last_load_ms: number | null;
  last_ttfb_ms: number | null;
  avg_load_ms: number | null;
  isolation_ok: boolean | null;
  blocked_requests: number;
  last_error: string | null;
  memory_mb: number | null;
  process_count: number;
}

export interface LogEntry {
  id: number;
  ts: number;
  level: "info" | "warning" | "error";
  session_id: string | null;
  message: string;
  data: string | null;
}

export interface LabState {
  ts: number;
  summary: {
    total: number;
    running: number;
    starting: number;
    stopping: number;
    error: number;
    idle: number;
    max_sessions: number;
    max_concurrent: number;
    cpu_threshold: number;
    ram_threshold: number;
    admission_blocked_reason: string | null;
  };
  system: {
    cpu_percent: number;
    ram_percent: number;
    ram_used_gb: number;
    ram_total_gb: number;
    cpu_count: number;
    lab_process_count: number;
  };
  sessions: Session[];
  logs: LogEntry[];
}

export class ApiError extends Error {}

export async function api<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_URL + path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(`${res.status}: ${detail}`);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}
