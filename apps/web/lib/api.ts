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
  asrHealth: () => request<Record<string, unknown>>("/api/asr/health"),
  ttsHealth: () => request<Record<string, unknown>>("/api/tts/health"),
  listTtsSpeakers: () => request<string[]>("/api/tts/speakers"),
  listTtsVoices: () => request<Record<string, unknown>[]>("/api/tts/voices"),
  registerTtsVoice: (body: FormData) =>
    fetch(`${CONTROL_PLANE_URL}/api/tts/voices`, { method: "POST", body }).then(async (res) => {
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json() as Promise<Record<string, unknown>>;
    }),
  previewTts: async (body: { text: string; voice?: string; language?: string; instruct?: string; sample_rate?: number }) => {
    const res = await fetch(`${CONTROL_PLANE_URL}/api/tts/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return await res.blob();
  },
  getSettings: () => request<Record<string, unknown>>("/api/settings"),
  saveSettings: (body: unknown) => request<Record<string, unknown>>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
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
  updateObject: (id: string, body: unknown) =>
    request<Record<string, unknown>>(`/api/objects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteObject: (id: string) =>
    request<Record<string, unknown>>(`/api/objects/${id}`, { method: "DELETE" }),
  importObjects: (body: unknown, accountId = "acc-001") =>
    request<Record<string, unknown>>(`/api/objects/import?account_id=${accountId}`, { method: "POST", body: JSON.stringify(body) }),
  listKnowledge: (accountId = "acc-001") => request<Record<string, unknown>[]>(`/api/knowledge?account_id=${accountId}`),
  deleteKnowledge: (id: string, accountId = "acc-001") =>
    request<Record<string, unknown>>(`/api/knowledge/${id}?account_id=${accountId}`, { method: "DELETE" }),
  importKnowledge: (body: { account_id: string; path?: string; content: string }) =>
    request<Record<string, unknown>>("/api/knowledge/import", { method: "POST", body: JSON.stringify(body) }),
  listPersonas: () => request<Record<string, unknown>[]>("/api/personas"),
  getPersona: (id: string) => request<Record<string, unknown>>(`/api/personas/${id}`),
  createPersona: (body: unknown) => request<Record<string, unknown>>("/api/personas", { method: "POST", body: JSON.stringify(body) }),
  updatePersona: (id: string, body: unknown) =>
    request<Record<string, unknown>>(`/api/personas/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deletePersona: (id: string) => request<Record<string, unknown>>(`/api/personas/${id}`, { method: "DELETE" }),
  updatePersonas: (body: unknown) => request<Record<string, unknown>>("/api/personas", { method: "PUT", body: JSON.stringify(body) }),
  getTurns: (id: string) => request<Record<string, unknown>[]>(`/api/calls/${id}/turns`),
  getSettlement: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}/settlement`),
  activeCalls: () => request<Record<string, unknown>[]>("/api/supervisor/active-calls"),
  supervisorJoin: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/join`, { method: "POST" }),
  supervisorPause: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/pause-agent`, { method: "POST" }),
  supervisorTakeover: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/takeover`, { method: "POST" }),
  supervisorTransfer: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/transfer`, { method: "POST" }),
  reportsSummary: () => request<Record<string, unknown>>("/api/reports/summary"),
  reportsCalls: () => request<Record<string, unknown>[]>("/api/reports/calls"),
  reportsUsage: () => request<Record<string, unknown>>("/api/reports/usage"),
  listTemplates: (accountId = "acc-001") => request<Record<string, unknown>[]>(`/api/templates?account_id=${accountId}`),
  getTemplate: (id: string) => request<Record<string, unknown>>(`/api/templates/${id}`),
  createTemplate: (body: unknown) => request<Record<string, unknown>>("/api/templates", { method: "POST", body: JSON.stringify(body) }),
  updateTemplate: (id: string, body: unknown) =>
    request<Record<string, unknown>>(`/api/templates/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteTemplate: (id: string) => request<Record<string, unknown>>(`/api/templates/${id}`, { method: "DELETE" }),
  listAudit: (accountId = "", action = "", callId = "") =>
    request<Record<string, unknown>[]>(
      `/api/audit?account_id=${encodeURIComponent(accountId)}&action=${encodeURIComponent(action)}&call_id=${encodeURIComponent(callId)}`,
    ),
};
