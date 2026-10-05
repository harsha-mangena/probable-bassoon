// Typed client for the Front Desk Python API (proxied same-origin via next.config.mjs).

export interface Msg { speaker: "agent" | "caller"; text: string }
export interface Slot { index: number; start: string; end: string; label: string }
export interface Booking {
  id: string; service: string; start: string; end: string; label: string;
  customer_name: string | null; customer_phone: string | null; status: string;
}

export async function api<T>(method: string, path: string, body?: unknown)
  : Promise<{ status: number; data: T }> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const data = (await r.json().catch(() => ({}))) as T;
  return { status: r.status, data };
  }
