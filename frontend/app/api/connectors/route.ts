import { forward, serviceHeaders } from "../../lib/serviceProxy";

export async function GET() {
  return forward("/connectors", { headers: serviceHeaders() });
}
