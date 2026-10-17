// Server-side helpers for the Next API routes that forward to the Python
// service. The optional shared secret (API_ACCESS_TOKEN) is read from the
// server environment only and attached here, so it never reaches the browser.

export const SERVICE_URL = process.env.PYTHON_SERVICE_URL ?? "http://localhost:8000";

export function serviceHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = process.env.API_ACCESS_TOKEN;
  return token ? { ...extra, "X-Access-Token": token } : extra;
}

export async function forward(path: string, init: RequestInit = {}): Promise<Response> {
  try {
    const res = await fetch(`${SERVICE_URL}${path}`, { cache: "no-store", ...init });
    const data = await res.json().catch(() => ({}));
    const headers = new Headers();
    const retryAfter = res.headers.get("retry-after");
    if (retryAfter) headers.set("retry-after", retryAfter);
    return Response.json(data, { status: res.status, headers });
  } catch (err) {
    return Response.json(
      {
        error: "Could not reach the Python service.",
        detail: err instanceof Error ? err.message : String(err),
      },
      { status: 503 }
    );
  }
}
