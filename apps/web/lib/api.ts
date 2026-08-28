export const CONTROL_PLANE_URL = process.env.NEXT_PUBLIC_CONTROL_PLANE_URL ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${CONTROL_PLANE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ ok: boolean }>("/health"),
  token: (body: { account_id: string; object_id?: string; call_id?: string }) =>
    request<{ url: string; token: string; roomName: string }>("/api/token", { method: "POST", body: JSON.stringify(body) }),
  createCall: (body: unknown) => request<Record<string, unknown>>("/api/calls", { method: "POST", body: JSON.stringify(body) }),
  listCalls: (accountId = "acc-001", status = "") =>
    request<Record<string, unknown>[]>(`/api/calls?account_id=${accountId}&status=${status}`),
  getCall: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}`),
  hangup: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}/hangup`, { method: "POST" }),
  settle: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}/settle`, { method: "POST" }),
  searchKnowledge: (query: string, accountId = "acc-001") =>
    request<Record<string, unknown>[]>(`/api/knowledge/search?query=${encodeURIComponent(query)}&account_id=${accountId}`),
  listObjects: (accountId = "acc-001") =>
    request<Record<string, unknown>[]>(`/api/objects?account_id=${accountId}`),
  getObject: (id: string) => request<Record<string, unknown>>(`/api/objects/${id}`),
  createObject: (body: unknown, accountId = "acc-001") =>
    request<Record<string, unknown>>(`/api/objects?account_id=${accountId}`, { method: "POST", body: JSON.stringify(body) }),
  importObjects: (body: unknown, accountId = "acc-001") =>
    request<Record<string, unknown>>(`/api/objects/import?account_id=${accountId}`, { method: "POST", body: JSON.stringify(body) }),
  importKnowledge: (body: unknown) => request<Record<string, unknown>>("/api/knowledge/import", { method: "POST", body: JSON.stringify(body) }),
  listPersonas: () => request<Record<string, unknown>[]>("/api/personas"),
  getPersona: (id: string) => request<Record<string, unknown>>(`/api/personas/${id}`),
  updatePersonas: (body: unknown) => request<Record<string, unknown>>("/api/personas", { method: "PUT", body: JSON.stringify(body) }),
  getTurns: (id: string) => request<Record<string, unknown>[]>(`/api/calls/${id}/turns`),
  getSettlement: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}/settlement`),
  activeCalls: () => request<Record<string, unknown>[]>("/api/supervisor/active-calls"),
  supervisorJoin: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/join`, { method: "POST" }),
  supervisorPause: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/pause-agent`, { method: "POST" }),
  supervisorTakeover: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/takeover`, { method: "POST" }),
  supervisorTransfer: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/transfer`, { method: "POST" }),
};
