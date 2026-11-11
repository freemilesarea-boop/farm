"use client";
import { useEffect, useRef, useState } from "react";
import { api, LabState, WS_URL } from "./api";

export type Connection = "connecting" | "live" | "polling" | "offline";

/** Real-time state via WebSocket (1 s push); falls back to HTTP polling while the socket is down. */
export function useLabState() {
  const [state, setState] = useState<LabState | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let closed = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      try {
        setState(await api<LabState>("/api/state"));
        setConnection((c) => (c === "live" ? c : "polling"));
      } catch {
        setConnection((c) => (c === "live" ? c : "offline"));
      }
    };
    const startPolling = () => {
      if (!pollTimer) {
        poll();
        pollTimer = setInterval(poll, 2000);
      }
    };
    const stopPolling = () => {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
    };

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => {
        setConnection("live");
        stopPolling();
      };
      ws.onmessage = (ev) => setState(JSON.parse(ev.data));
      ws.onclose = () => {
        if (closed) return;
        setConnection("polling");
        startPolling();
        retryTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };
    connect();

    return () => {
      closed = true;
      stopPolling();
      if (retryTimer) clearTimeout(retryTimer);
      wsRef.current?.close();
    };
  }, []);

  return { state, connection };
}
