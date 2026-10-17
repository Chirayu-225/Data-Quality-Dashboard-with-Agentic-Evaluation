import { forward, serviceHeaders } from "../../../../../lib/serviceProxy";

export async function POST(request: Request, { params }: { params: Promise<{ key: string }> }) {
  const { key } = await params;
  const body = await request.text();
  return forward(`/connectors/${encodeURIComponent(key)}/oauth/start`, {
    method: "POST",
    headers: serviceHeaders({ "Content-Type": "application/json" }),
    body,
  });
}
