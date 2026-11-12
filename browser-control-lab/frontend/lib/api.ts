export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
export const WS_URL = API_URL.replace(/^http/, "ws") + "/ws";

export type SessionStatus = "idle" | "starting" | "running" | "stopping" | "stopped" | "error";
export type Scenario = "basic" | "heavy" | "slow" | "flaky" | "error" | "custom";

export type CustomMode = "page_load" | "video";

export interface CustomConfig {
  /** exactly what the user typed (snapshot at registration) */
  url: string;
  /** normalised URL the browser navigates to */
  nav_url?: string;
  kind?: "page" | "video_file" | "youtube";
  registered_at?: number;
  mode: CustomMode;
  reload_interval_s: number;
  muted: boolean;
  loop: boolean;
}

export type VideoState = "playing" | "paused" | "ended" | "error" | "none";

export interface VideoStatus {
  present: boolean;
  state: VideoState;
  paused?: boolean;
  ended?: boolean;
  current_time?: number | null;
  duration?: number | null;
  ready_state?: number;
  ready_state_name?: string | null;
  network_state?: number;
  network_state_name?: string | null;
  error?: { code: number; name: string | null; message: string | null } | null;
  current_src?: string | null;
  video_count?: number;
  repeats?: number;
  muted?: boolean;
  loop?: boolean;
  hint?: string;
}

export interface ProbeResult {
  input_url: string;
  allowed: boolean;
  blocked_reason: string | null;
  normalized_url?: string;
  host?: string;
  local?: boolean;
  resolved_ips?: string[];
  media_kind?: "page" | "video_file" | null;
  verification_required?: boolean;
  verified?: boolean | null;
  final_url?: string | null;
  redirects?: string[];
  http_status?: number | null;
  content_type?: string | null;
  is_direct_video?: boolean | null;
  is_html?: boolean | null;
  video_found?: boolean | null;
  video?: VideoStatus | null;
  blocked_subrequests?: string[];
  error?: string;
  /** set when the URL is fine except that its host is not allowed yet ("내 서버 등록" possible) */
  registrable_host?: string | null;
  /** /api/custom/register only */
  registered?: boolean;
  already_allowed?: boolean;
}

export interface LabConfig {
  max_sessions: number;
  max_concurrent: number;
  custom: {
    allowed_hosts: string[];
    resource_hosts: string[];
    allow_private_network: boolean;
    require_verification: boolean;
    config_problems: string[];
    /** hosts registered from the dashboard after the ownership check (removable) */
    registered_hosts: string[];
    blocked_platform_examples: string[];
    min_reload_s: number;
    verification: { path: string; token: string };
    local_test_pages: Record<string, string>;
  };
}

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
  /** USS sum of the session's processes (memory freed when it stops) */
  memory_mb: number | null;
  /** RSS sum (counts shared pages once per process, so it overstates) */
  memory_rss_mb: number | null;
  memory_method: string | null;
  process_count: number;
  custom: CustomConfig | null;
  current_url: string | null;
  page_state: "idle" | "loading" | "loaded" | "error";
  page_load_count: number;
  video: VideoStatus | null;
  peak_memory_mb?: number | null;
  peak_process_count?: number;
  /** YouTube single-session compatibility test */
  youtube?: boolean;
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
    pressure_block_level: string;
    admission_blocked_reason: string | null;
  };
  system: {
    cpu_percent: number;
    ram_percent: number;
    ram_used_gb: number;
    ram_total_gb: number;
    cpu_count: number;
    lab_process_count: number;
    memory: SystemMemory;
    lab_memory: {
      uss_mb: number;
      rss_mb: number;
      percent_of_total: number;
      method: string | null;
      backend_rss_mb: number | null;
    };
  };
  sessions: Session[];
  logs: LogEntry[];
}

export type PressureLevel = "normal" | "warn" | "critical" | "unknown";

export interface SystemMemory {
  total_gb: number;
  used_gb: number;
  available_gb: number;
  /** OS-appropriate used % (macOS: App + Wired + Compressed, like Activity Monitor) */
  used_percent: number;
  /** raw psutil virtual_memory().percent, for comparison */
  psutil_percent: number;
  method: string;
  breakdown_gb: Record<string, number>;
  pressure: { level: PressureLevel; source?: string; value?: number | null };
  swap: { total_gb: number; used_gb: number; percent: number; in_mb_s: number; out_mb_s: number };
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

/** youtube.com / youtu.be video URL → YouTube single-session compatibility test (mirrors the backend rule). */
export function isYouTubeUrl(url: string): boolean {
  try {
    const u = new URL(url.trim());
    const h = u.hostname.toLowerCase().replace(/\.$/, "");
    return ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"].includes(h);
  } catch {
    return false;
  }
}
