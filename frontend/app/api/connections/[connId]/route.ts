import { forward, serviceHeaders } from "../../../lib/serviceProxy";

export async function DELETE(_req: Request, { params }: { params: Promise<{ connId: string }> }) {
  const { connId } = await params;
  return forward(`/connections/${encodeURIComponent(connId)}`, {
    method: "DELETE",
    headers: serviceHeaders(),
  });
}
