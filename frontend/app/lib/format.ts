/** Live clock for a running scan: 0:07, 2:41, 1:02:03. */
export function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

/** Human duration for a finished scan: 8.4 s, 1 min 42 s, 1 h 3 min. */
export function formatDuration(totalSeconds: number): string {
  const t = Math.max(0, totalSeconds);
  if (t < 1) return `${Math.max(1, Math.round(t * 1000))} ms`;
  if (t < 60) return `${t < 10 ? t.toFixed(1) : Math.round(t)} s`;
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = Math.round(t % 60);
  if (h > 0) return `${h} h ${m} min`;
  return `${m} min ${s} s`;
}
