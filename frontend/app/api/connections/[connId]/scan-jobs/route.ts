import { forward, serviceHeaders } from "../../../../lib/serviceProxy";

// Submit half of the async flow for a saved connection. Polling reuses
// /api/scan-jobs/[jobId] (the service serves every job kind from one place).
export async function POST(request: Request, { params }: { params: Promise<{ connId: string }> }) {
  const { connId } = await params;
  const form = await request.formData();
  return forward(`/connections/${encodeURIComponent(connId)}/scan/jobs`, {
    method: "POST",
    headers: serviceHeaders(),
    body: form,
  });
}
