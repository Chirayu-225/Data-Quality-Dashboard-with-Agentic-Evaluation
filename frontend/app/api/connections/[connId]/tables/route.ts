import { forward, serviceHeaders } from "../../../../lib/serviceProxy";

export async function GET(_req: Request, { params }: { params: Promise<{ connId: string }> }) {
  const { connId } = await params;
  return forward(`/connections/${encodeURIComponent(connId)}/tables`, { headers: serviceHeaders() });
}
