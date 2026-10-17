// Client half of the async scan flow: submit -> job id -> poll until a
// terminal state. The synchronous /api/scan routes still exist, but a
// large semantic scan can run for many minutes and a held-open request
// gets cut off by browsers, proxies and hosting platforms long before.

export interface JobProgress {
  rows_considered: number;
  total_rows: number;
  /** Rows whose model reply couldn't be parsed: attempted, NOT reviewed. */
  rows_unreadable?: number;
}

export interface StageInfo {
  id: "checks" | "instruction" | "semantic" | "verify" | "score";
  status: "pending" | "running" | "done" | "skipped";
  started_at: number | null;
  finished_at: number | null;
  /** Integers only (never row content). */
  counts: Record<string, number>;
  reason: string | null;
}

export type JobState = "queued" | "running" | "done" | "error" | "cancelled";

interface JobStatusBody<T> {
  job_id: string;
  status: JobState;
  progress: JobProgress;
  stages?: StageInfo[];
  /** Server-measured seconds since submission (frozen once finished). */
  elapsed_seconds?: number;
  result: T | null;
  error: string | null;
}

/** Thrown when the user (or another tab) cancelled the job. Not a failure. */
export class ScanCancelledError extends Error {
  constructor(message = "Scan cancelled. Nothing was kept: a cancelled scan produces no results.") {
    super(message);
    this.name = "ScanCancelledError";
  }
}

const POLL_INTERVAL_MS = 1500;
const MAX_POLL_INTERVAL_MS = 5000;

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("Aborted", "AbortError"));
    });
  });
}

/** Submits `formData`; resolves to the job id. */
export async function submitScanJob(
  submitUrl: string,
  formData: FormData,
  signal?: AbortSignal
): Promise<string> {
  const submit = await fetch(submitUrl, { method: "POST", body: formData, signal });
  const body = await submit.json();
  if (submit.status === 429) {
    throw new Error("The server is busy with other scans right now. Wait a moment and try again.");
  }
  if (!submit.ok) {
    throw new Error(body.detail || body.error || `Scan failed (HTTP ${submit.status})`);
  }
  return body.job_id as string;
}

/** Polls until a TERMINAL state, then stops: returns the result on
 *  done, throws on error / cancelled / expired (410) / lost (404). */
export async function pollScanJob<T>(
  jobId: string,
  onProgress: (status: JobState, progress: JobProgress, elapsedSeconds: number, stages: StageInfo[]) => void,
  signal?: AbortSignal
): Promise<T> {
  let interval = POLL_INTERVAL_MS;
  for (;;) {
    const res = await fetch(`/api/scan-jobs/${encodeURIComponent(jobId)}`, { signal });
    if (res.status === 410) {
      throw new Error("These results expired and were deleted from the server. Please run the scan again.");
    }
    if (res.status === 404) {
      throw new Error("This scan was lost — the server restarted, or the link is wrong. Please run it again.");
    }
    const body = (await res.json()) as JobStatusBody<T> & { detail?: string };
    if (!res.ok) {
      throw new Error(body.detail || body.error || `Scan failed (HTTP ${res.status})`);
    }
    onProgress(body.status, body.progress, body.elapsed_seconds ?? 0, body.stages ?? []);
    if (body.status === "done" && body.result) return body.result;
    if (body.status === "cancelled") throw new ScanCancelledError(body.error ?? undefined);
    if (body.status === "error") {
      // The server's error text includes a traceback; show only the first line.
      throw new Error((body.error ?? "Scan failed").split("\n")[0]);
    }
    await sleep(interval, signal);
    interval = Math.min(MAX_POLL_INTERVAL_MS, Math.round(interval * 1.15));
  }
}

export async function cancelScanJob(jobId: string): Promise<void> {
  await fetch(`/api/scan-jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
}
