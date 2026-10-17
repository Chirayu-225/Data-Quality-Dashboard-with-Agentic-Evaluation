const SERVICE_URL = process.env.PYTHON_SERVICE_URL ?? "http://localhost:8000";

// Submit half of the async scan flow: forwards the multipart body to the
// Python service's /scans/schema/jobs and returns { job_id } immediately — the
// slow scan itself runs in the background there, so this request never
// has to outlive a proxy/browser/platform timeout. See
// app/lib/runScanJob.ts for the polling half.
export async function POST(request: Request) {
  try {
    const incomingForm = await request.formData();
    const res = await fetch(`${SERVICE_URL}/scans/schema/jobs`, {
      method: "POST",
      body: incomingForm,
      cache: "no-store",
    });
    const data = await res.json();
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
