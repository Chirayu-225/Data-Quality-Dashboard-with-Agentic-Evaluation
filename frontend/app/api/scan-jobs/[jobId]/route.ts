const SERVICE_URL = process.env.PYTHON_SERVICE_URL ?? "http://localhost:8000";

// Poll half of the async scan flow — shared by single-table and schema
// jobs (the Python service serves both from GET /scans/jobs/{id}).
export async function GET(
  request: Request,
  { params }: { params: Promise<{ jobId: string }> }
) {
  try {
    const { jobId } = await params;
    // ?events=true returns the stage timeline too (used for "Download run record").
    const wantEvents = new URL(request.url).searchParams.get("events") === "true";
    const res = await fetch(
      `${SERVICE_URL}/scans/jobs/${encodeURIComponent(jobId)}${wantEvents ? "?events=true" : ""}`,
      { cache: "no-store" }
    );
    const data = await res.json();
    return Response.json(data, { status: res.status });
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

// Cancel: stops a queued/running job so it stops spending provider quota.
export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ jobId: string }> }
) {
  try {
    const { jobId } = await params;
    const res = await fetch(`${SERVICE_URL}/scans/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
      cache: "no-store",
    });
    const data = await res.json();
    return Response.json(data, { status: res.status });
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
