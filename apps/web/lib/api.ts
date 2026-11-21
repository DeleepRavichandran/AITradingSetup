"use client";
import { useCallback, useEffect, useRef, useState } from "react";

const KEY = "nseql_token";
export const getToken = () => (typeof window === "undefined" ? "" : window.localStorage.getItem(KEY) || "");
export const setToken = (t: string) => window.localStorage.setItem(KEY, t);

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const t = getToken();
  if (t) headers.Authorization = `Bearer ${t}`;
  const r = await fetch(`/api${path}`, { ...init, headers: { ...headers, ...(init?.headers as any) }, cache: "no-store" });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j); } catch {}
    if (r.status === 401 && typeof window !== "undefined") window.dispatchEvent(new Event("nseql-unauthorized"));
    throw new ApiError(r.status, msg);
  }
  return r.json();
}
export const post = <T = any>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

/** Fetch `path` (null = skip). `pollMs` re-fetches on an interval. */
export function useApi<T = any>(path: string | null, pollMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!path);
  const seq = useRef(0);
  const reload = useCallback(async () => {
    if (!path) { setData(null); setLoading(false); return; }
    const id = ++seq.current;
    try {
      const d = await api<T>(path);
      if (id === seq.current) { setData(d); setError(null); }
    } catch (e: any) {
      if (id === seq.current) setError(e.message);
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    setLoading(!!path);
    reload();
    if (!pollMs || !path) return;
    const h = setInterval(reload, pollMs);
    return () => clearInterval(h);
  }, [reload, pollMs, path]);
  return { data, error, loading, reload };
}

/** Poll a background job until it finishes. */
export function useJob(jobId: string | null) {
  const { data, error } = useApi<any>(jobId ? `/jobs/${jobId}` : null, 1500);
  return { job: data, error, running: !!jobId && (!data || data.status === "running") };
}

export function useQuery(): URLSearchParams {
  const [q, setQ] = useState(new URLSearchParams());
  useEffect(() => { setQ(new URLSearchParams(window.location.search)); }, []);
  return q;
}

// ---------------------------------------------------------------- formatting
const nf = (d: number) => new Intl.NumberFormat("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
export const num = (x: any, d = 2) => (x === null || x === undefined || Number.isNaN(x) ? "—" : nf(d).format(x));
export const pct = (x: any, d = 1) => (x === null || x === undefined ? "—" : `${nf(d).format(x * 100)}%`);
export const inr = (x: any, d = 0) => (x === null || x === undefined ? "—" : `₹${nf(d).format(x)}`);
export const short = (s: any, n = 10) => (s ? String(s).slice(0, n) : "—");
export const day = (s: any) => (s ? String(s).slice(0, 10) : "—");
