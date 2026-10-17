import { forward, serviceHeaders } from "../../lib/serviceProxy";

export async function GET() {
  return forward("/connections", { headers: serviceHeaders() });
}

export async function POST(request: Request) {
  const body = await request.text();
  return forward("/connections", {
    method: "POST",
    headers: serviceHeaders({ "Content-Type": "application/json" }),
    body,
  });
}
