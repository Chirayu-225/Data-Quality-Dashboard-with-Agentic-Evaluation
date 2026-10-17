"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  JobProgress,
  JobState,
  StageInfo,
  ScanCancelledError,
  cancelScanJob,
  pollScanJob,
  submitScanJob,
} from "./runScanJob";

const EMPTY: JobProgress = { rows_considered: 0, total_rows: 0 };

function setJobParam(id: string | null) {
  try {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("job", id);
    else url.searchParams.delete("job");
    window.history.replaceState(null, "", url.toString());
  } catch {
    // URL sync is a convenience; the scan itself is unaffected.
  }
}

/** Owns one scan's lifecycle. The job id lives in the URL (?job=...), so
 *  a refresh or a reopened link re-attaches to the same server-side job
 *  instead of orphaning it. Unmounting only stops POLLING — it never
 *  cancels; cancelling is the explicit `cancel()`. */
export function useScanJob<T>(submitUrl: string) {
  const [result, setResult] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<JobState>("queued");
  const [progress, setProgress] = useState<JobProgress>(EMPTY);
  const [stages, setStages] = useState<StageInfo[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  // Elapsed time. The SERVER is the clock of record (it survives a page
  // refresh and gives the exact final duration); between polls the browser
  // just extends the last server reading so the display ticks smoothly.
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const anchorRef = useRef<{ server: number; at: number; frozen: boolean }>({ server: 0, at: 0, frozen: true });
  const abortRef = useRef<AbortController | null>(null);
  const jobIdRef = useRef<string | null>(null);

  const attach = useCallback(async (jobId: string) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    jobIdRef.current = jobId;
    setJobId(jobId);
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await pollScanJob<T>(
        jobId,
        (s, p, elapsed, st) => {
          setStatus(s);
          setProgress(p);
          setStages(st);
          const terminal = s === "done" || s === "error" || s === "cancelled";
          anchorRef.current = { server: elapsed, at: performance.now(), frozen: terminal };
          setElapsedSeconds(elapsed);
        },
        ctrl.signal
      );
      setResult(data);
    } catch (e) {
      if (ctrl.signal.aborted) return; // superseded or unmounted: stay silent
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
      setJobParam(null);
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }, []);

  const start = useCallback(
    async (formData: FormData) => {
      abortRef.current?.abort();
      setLoading(true);
      setError(null);
      setResult(null);
      setStatus("queued");
      setProgress(EMPTY);
      setStages([]);
      anchorRef.current = { server: 0, at: performance.now(), frozen: false };
      setElapsedSeconds(0);
      try {
        const id = await submitScanJob(submitUrl, formData);
        setJobParam(id);
        await attach(id);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      }
    },
    [submitUrl, attach]
  );

  const cancel = useCallback(async () => {
    const id = jobIdRef.current;
    anchorRef.current = { ...anchorRef.current, frozen: true };
    abortRef.current?.abort();
    setLoading(false);
    setError(new ScanCancelledError().message);
    setJobParam(null);
    if (id) await cancelScanJob(id).catch(() => undefined);
  }, []);

  // Tick the display while a scan is in flight.
  useEffect(() => {
    if (!loading) return;
    const t = setInterval(() => {
      const a = anchorRef.current;
      if (!a.frozen) setElapsedSeconds(a.server + (performance.now() - a.at) / 1000);
    }, 200);
    return () => clearInterval(t);
  }, [loading]);

  // Re-attach after refresh / reopened link.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("job");
    if (id) void attach(id);
    return () => abortRef.current?.abort();
  }, [attach]);

  return { result, loading, error, status, progress, stages, jobId, elapsedSeconds, start, cancel, setResult };
}
