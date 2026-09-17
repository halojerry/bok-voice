declare global {
  interface Window {
    __BOK_CONFIG__?: { cpUrl?: string; livekitUrl?: string };
  }
}

// 服务只绑 127.0.0.1；避免 localhost 优先解析 ::1 导致 fetch 失败。
// 运行时求值（构建期常量会把地址烤进产物，节点本地托管即失效）：
// 节点注入的 window.__BOK_CONFIG__.cpUrl 优先，云端托管回退构建期 env/默认值。
export function apiBase(): string {
  if (typeof window !== "undefined" && window.__BOK_CONFIG__?.cpUrl) {
    return window.__BOK_CONFIG__.cpUrl;
  }
  return process.env.NEXT_PUBLIC_CONTROL_PLANE_URL ?? "http://127.0.0.1:8000";
}

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

/**
 * 鉴权请求头（B4 契约 §4）：localStorage 存在 `bok_token` 时附 Bearer 头。
 * 无 token / 服务端渲染（静态导出预渲染）一律返回空表——本函数只在客户端生效。
 */
export function authHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const token = window.localStorage.getItem("bok_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * 401 统一处理（契约 §4）：清 token 并硬跳登录页。
 * 豁免口一：/api/auth/*（登录失败、me() 探测要留在原页展示错误）；
 * 豁免口二：已经在登录页时不重复跳转（避免刷新循环）。
 */
function handleUnauthorized(path: string) {
  if (typeof window === "undefined") return;
  if (path.startsWith("/api/auth/")) return;
  if (window.location.pathname.startsWith("/login")) return;
  window.localStorage.removeItem("bok_token");
  window.location.href = "/login/";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // correlation 透传:前端生成 request_id,audit 行可与前端动作对账;
  // call_id 由调用方在 headers 显式带(init.headers 里已有则不覆盖)。
  const extra = new Headers(init?.headers);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Request-ID": crypto.randomUUID(),
    ...authHeaders(),
  };
  extra.forEach((v, k) => {
    headers[k] = v;
  });
  const res = await fetch(`${apiBase()}${path}`, { ...init, headers });
  if (!res.ok) {
    if (res.status === 401) handleUnauthorized(path);
    throw await toError(res);
  }
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
    // FormData 不能手写 Content-Type（边界由浏览器生成），只补鉴权头。
    fetch(`${apiBase()}/api/tts/voices`, { method: "POST", body, headers: authHeaders() }).then(async (res) => {
      if (!res.ok) throw await toError(res);
      return res.json() as Promise<Record<string, unknown>>;
    }),
  previewTts: async (body: { text: string; voice?: string; language?: string; instruct?: string; sample_rate?: number; provider?: string }) => {
    const res = await fetch(`${apiBase()}/api/tts/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await toError(res);
    return await res.blob();
  },
  // 罐头音试听(2026-09-11):词条列表(垫话资产走 /api/tts/filler-preview 二进制端点,
  // 由调用方直接 fetch blob,不经 JSON request)。
  listQaEntries: (enabled = 1) =>
    request<Record<string, unknown>[]>(`/api/qa-entries?enabled=${enabled}`),
  // ---- B4 会话/权限/员工管理（契约预埋，见 .superpowers/sdd/2026-09-14-b4-permissions/CONTRACT.md） ----
  login: (username: string, password: string) =>
    request<{ token: string } & Record<string, unknown>>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  me: () => request<SessionInfo>("/api/auth/me"),
  changeMyPassword: (oldPassword: string, newPassword: string) =>
    request<Record<string, unknown>>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  // 后端返回 {"users": [...]} 信封（B1 形状），此处解包成数组供消费方直用。
  listUsers: () => request<{ users?: UserRow[] }>("/api/users").then((r) => r.users ?? []),
  createUser: (body: { username: string; password: string; role?: string; display_name?: string; permissions?: string[] }) =>
    request<UserRow>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (id: string, body: { password?: string; status?: string; display_name?: string; permissions?: string[] }) =>
    request<UserRow>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  // 快答库管理 CRUD（B3 owner 语义：user 只改自己的，共享=admin/root；hit 走 agent 通道不在此）。
  listQaAll: (accountId = "acc-001") =>
    request<Record<string, unknown>[]>(`/api/qa-entries?account_id=${encodeURIComponent(accountId)}`),
  createQa: (body: unknown) => request<Record<string, unknown>>("/api/qa-entries", { method: "POST", body: JSON.stringify(body) }),
  patchQa: (id: string, body: unknown) =>
    request<Record<string, unknown>>(`/api/qa-entries/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteQa: (id: string) => request<Record<string, unknown>>(`/api/qa-entries/${id}`, { method: "DELETE" }),
  getSettings: () => request<Record<string, unknown>>("/api/settings"),
  saveSettings: (body: unknown) => request<Record<string, unknown>>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
  // 电话边缘站点（P1.5）：站点下拉 + 一次性把 SIP 供应商凭据注册成 outbound trunk。
  // 注册成功返回 trunk_id——调用方回填设置表单的 sip.trunk_id（保存后 campaign 按站点取）。
  listSites: (accountId = "acc-001") =>
    request<Record<string, unknown>[]>(`/api/sip/sites?account_id=${encodeURIComponent(accountId)}`),
  // 建站（P1.5 T7）：最小 body = name + livekit_url；**幂等**——同账号同名已存在
  // 时 CP 返回既有行（不重复建、不改写字段），调用方按返回 id 选中即可。
  createSite: (body: { name: string; livekit_url?: string; sip_edge?: string; numbers?: string[]; region?: string; account_id?: string }) =>
    request<Record<string, unknown>>("/api/sip/sites", { method: "POST", body: JSON.stringify(body) }),
  registerSipTrunk: (
    siteId: string,
    body: { address: string; numbers: string[]; auth_username?: string; auth_password?: string },
  ) =>
    request<{ trunk_id: string; site: Record<string, unknown> }>(
      `/api/sip/sites/${encodeURIComponent(siteId)}/trunk`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  // 官方 TokenSourceResponse 契约({serverUrl, participantToken});TokenSource
  // 直连本响应,无需键名映射。
  token: (body: { account_id: string; object_id?: string; call_id?: string; role?: string }) =>
    request<{ serverUrl: string; participantToken: string }>("/api/token", { method: "POST", body: JSON.stringify(body) }),
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
  // 单发外呼（P1.5）：对象页「立即外呼」——建一通 outbound 通话并直接派 agent
  // （dial 块与 campaign 同一 build_dial_block；无 phone=400）。
  dialNow: (
    objectId: string,
    body: { template_id?: string; persona_id?: string; language?: string; site_id?: string } = {},
  ) =>
    request<{ call_id: string; status: string }>(
      `/api/objects/${encodeURIComponent(objectId)}/dial-now`,
      { method: "POST", body: JSON.stringify(body) },
    ),
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
  // 静默旁听：只订阅 token（can_publish 全关）；start 记审计、stop 补时长。
  supervisorListen: (id: string) => request<Record<string, unknown>>(`/api/supervisor/${id}/listen`, { method: "POST" }),
  supervisorListenStop: (id: string, seconds: number) =>
    request<Record<string, unknown>>(`/api/supervisor/${id}/listen/stop`, { method: "POST", body: JSON.stringify({ seconds }) }),
  markWhatsappHandled: (id: string, handled = true) =>
    request<Record<string, unknown>>(`/api/calls/${id}/whatsapp/handled`, { method: "POST", body: JSON.stringify({ handled }) }),
  // 名册认领池（Wave1 outbound campaign）：status/channel 空=不过滤；
  // claim/unclaim/handled 均返回更新后的条目，handled 联动来源通话横幅状态。
  listRoster: (status = "", channel = "", accountId = "acc-001") =>
    request<Record<string, unknown>[]>(
      `/api/roster?account_id=${encodeURIComponent(accountId)}&status=${encodeURIComponent(status)}&channel=${encodeURIComponent(channel)}`,
    ),
  rosterClaim: (id: string, claimedBy = "acc-001") =>
    request<Record<string, unknown>>(`/api/roster/${id}/claim`, { method: "POST", body: JSON.stringify({ claimed_by: claimedBy }) }),
  rosterUnclaim: (id: string) =>
    request<Record<string, unknown>>(`/api/roster/${id}/unclaim`, { method: "POST" }),
  rosterHandled: (id: string, handled = true) =>
    request<Record<string, unknown>>(`/api/roster/${id}/handled`, { method: "POST", body: JSON.stringify({ handled }) }),
  // 外呼战役（Wave 12）：建波次/启停/进度。启停非法迁移后端 409，UI 按 status 显隐按钮。
  createCampaign: (body: unknown) => request<Record<string, unknown>>("/api/campaigns", { method: "POST", body: JSON.stringify(body) }),
  listCampaigns: () => request<Record<string, unknown>[]>("/api/campaigns"),
  getCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}`),
  startCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}/start`, { method: "POST" }),
  pauseCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}/pause`, { method: "POST" }),
  stopCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}/stop`, { method: "POST" }),
  deleteCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}`, { method: "DELETE" }),
  // 改战役配置（2026-09-17 调度三字段）：running 服务端 409 锁定，字段全 optional。
  updateCampaign: (id: string, body: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/api/campaigns/${id}`, { method: "PUT", body: JSON.stringify(body) }),
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
  // 节点注册表（P1 平台面，root 专属）：清单 + 吊销/解除吊销（kill-switch UI）。
  // unrevoke 后端 409=节点本就未吊销（live），404=节点不存在，均由页面内联展示。
  listNodes: () => request<NodeRow[]>("/api/nodes"),
  revokeNode: (nodeId: string) =>
    request<{ node_id: string; revoked: boolean }>(
      `/api/nodes/${encodeURIComponent(nodeId)}/revoke`,
      { method: "POST" },
    ),
  unrevokeNode: (nodeId: string) =>
    request<{ node_id: string; revoked: boolean }>(
      `/api/nodes/${encodeURIComponent(nodeId)}/unrevoke`,
      { method: "POST" },
    ),
  setupStatus: () => request<SetupStatus>("/api/setup"),
  setupDownload: () => fetch(`${apiBase()}/api/setup/download`, { method: "POST", headers: authHeaders() }).then(async (res) => {
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

// ---- B4 会话与权限类型（契约预埋） ----
export type SessionInfo = {
  user_id: string;
  username: string;
  display_name?: string;
  role: "root" | "admin" | "user";
  org_id?: string;
  account_id: string;
  /** 有效权限键（目录 8 键，见 CONTRACT.md；admin/root=全部 grantable） */
  permissions: string[];
};

export type UserRow = {
  id: string;
  username: string;
  display_name?: string;
  role: string;
  status: string;
  account_id?: string;
  /** 有效权限（user 角色）；admin/root 为全部 grantable 键 */
  permissions?: string[];
  created_at?: string;
};

// ---- 节点注册表（P1，root 平台面；与 CP nodes_store.list_nodes 输出对齐） ----
export type NodeRow = {
  node_id: string;
  org_id?: string;
  name: string;
  platform: string;
  version?: string;
  /** online（心跳窗口内）/ offline / revoked（sticky 吊销） */
  status: "online" | "offline" | "revoked";
  /** root=人工吊销（sticky）；auto_clone=克隆检出自动吊销（重注册可复活） */
  revoked_source?: string;
  last_seen_at?: string | null;
  license_id?: string;
  /** 指纹只出前 12 位 hex（可辨识、不可还原） */
  fingerprint_prefix?: string;
};
