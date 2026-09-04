// 服务只绑 127.0.0.1；避免 localhost 优先解析 ::1 导致 fetch 失败。
export const CONTROL_PLANE_URL = process.env.NEXT_PUBLIC_CONTROL_PLANE_URL ?? "http://127.0.0.1:8000";

async function toError(res: Response): Promise<Error> {
  // 优先透传 FastAPI 的 detail（如 MiniMax API Key 未配置），失败时退回 statusText。
  let detail = "";
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") detail = body.detail;
  } catch {
    /* 非 JSON 响应(如网关错误页)直接忽略 */
  }
  return new Error(detail ? `${res.status} ${detail}` : `${res.status} ${res.statusText}`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${CONTROL_PLANE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw await toError(res);
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ ok: boolean }>("/health"),
  asrHealth: () => request<Record<string, unknown>>("/api/asr/health"),
  ttsHealth: () => request<Record<string, unknown>>("/api/tts/health"),
  listTtsSpeakers: () => request<string[]>("/api/tts/speakers"),
  listTtsVoices: () => request<Record<string, unknown>[]>("/api/tts/voices"),
  deleteTtsVoice: (voiceId: string) => request<Record<string, unknown>>(`/api/tts/voices/${encodeURIComponent(voiceId)}`, { method: "DELETE" }),
  registerTtsVoice: (body: FormData) =>
    fetch(`${CONTROL_PLANE_URL}/api/tts/voices`, { method: "POST", body }).then(async (res) => {
      if (!res.ok) throw await toError(res);
      return res.json() as Promise<Record<string, unknown>>;
    }),
  previewTts: async (body: { text: string; voice?: string; language?: string; instruct?: string; sample_rate?: number; provider?: string }) => {
    const res = await fetch(`${CONTROL_PLANE_URL}/api/tts/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await toError(res);
    return await res.blob();
  },
  getSettings: () => request<Record<string, unknown>>("/api/settings"),
  saveSettings: (body: unknown) => request<Record<string, unknown>>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
  token: (body: { account_id: string; object_id?: string; call_id?: string; role?: string }) =>
    request<{ url: string; token: string; roomName: string }>("/api/token", { method: "POST", body: JSON.stringify(body) }),
  createCall: (body: unknown) => request<Record<string, unknown>>("/api/calls", { method: "POST", body: JSON.stringify(body) }),
  listCalls: (accountId = "acc-001", status = "") =>
    request<Record<string, unknown>[]>(`/api/calls?account_id=${accountId}&status=${status}`),
  getCall: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}`),
  deleteCall: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}`, { method: "DELETE" }),
  clearEndedCalls: (accountId = "acc-001") =>
    request<Record<string, unknown>>(`/api/calls?account_id=${accountId}`, { method: "DELETE" }),
  hangup: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}/hangup`, { method: "POST" }),
  settle: (id: string) => request<Record<string, unknown>>(`/api/calls/${id}/settle`, { method: "POST" }),
  searchKnowledge: (query: string, accountId = "acc-001") =>
    request<Record<string, unknown>[]>(`/api/knowledge/search?query=${encodeURIComponent(query)}&account_id=${accountId}`),
  listObjects: (accountId = "acc-001") =>
    request<Record<string, unknown>[]>(`/api/objects?account_id=${accountId}`),
  getObject: (id: string) => request<Record<string, unknown>>(`/api/objects/${id}`),
  getObjectTopics: (id: string) => request<Record<string, unknown>[]>(`/api/objects/${id}/topics`),
  listGlobalInsights: () => request<Record<string, unknown>[]>("/api/insights"),
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
    // id 形如 md:accounts/...（含斜杠），必须走 query 参数：放 path 会被路由层 404。
    request<Record<string, unknown>>(`/api/knowledge?knowledge_id=${encodeURIComponent(id)}&account_id=${accountId}`, { method: "DELETE" }),
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
  supervisorResume: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/resume-agent`, { method: "POST" }),
  supervisorTakeover: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/takeover`, { method: "POST" }),
  supervisorTransfer: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/transfer`, { method: "POST" }),
  markWhatsappHandled: (id: string, handled = true) =>
    request<Record<string, unknown>>(`/api/calls/${id}/whatsapp/handled`, { method: "POST", body: JSON.stringify({ handled }) }),
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
  setupStatus: () => request<SetupStatus>("/api/setup"),
  setupDownload: () => fetch(`${CONTROL_PLANE_URL}/api/setup/download`, { method: "POST" }).then(async (res) => {
    if (!res.ok) throw await toError(res);
    return res.json() as Promise<{ started: boolean }>;
  }),
};

export type SetupModelStatus = {
  name: string;
  repo: string;
  present: boolean;
  required: boolean;
  size_bytes?: number;
};

export type SetupStatus = {
  ready: boolean;
  models: SetupModelStatus[];
  error?: string;
};
