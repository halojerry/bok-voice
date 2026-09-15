"use client";

// B4 会话与权限（契约预埋骨架，见 .superpowers/sdd/2026-09-14-b4-permissions/CONTRACT.md）。
// B（会话导航 agent）在此骨架上增强（logout/refresh 等）；C（页面 agent）只消费 useSession/hasPage。
// 匿名本地会话 = 单机 auth-off 形态：全部页面可见、acc-001，与现状零差异。

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, type SessionInfo } from "@/lib/api";

/** 权限目录 8 键（与后端 permissions.py 逐字对齐；顺序=导航顺序）。 */
export const PAGE_KEYS = [
  "calls",
  "roster",
  "campaigns",
  "objects",
  "interpret",
  "templates",
  "qa",
  "reports",
] as const;

export type PageKey = (typeof PAGE_KEYS)[number];

export const PAGE_LABELS: Record<PageKey, string> = {
  calls: "工作台",
  roster: "名册",
  campaigns: "外呼",
  objects: "对象",
  interpret: "同传",
  templates: "话术",
  qa: "快答库",
  reports: "报表",
};

export type Session = SessionInfo & { anonymous: boolean };

/** 匿名本地会话（无 token / me 401）：单机形态，全部页面 + acc-001。 */
export const ANON_SESSION: Session = {
  anonymous: true,
  user_id: "",
  username: "",
  display_name: "",
  role: "user",
  org_id: "",
  account_id: "acc-001",
  permissions: [...PAGE_KEYS],
};

/** 会话动作（B4 B 线新增；C 只消费 useSession/hasPage，不受影响）。 */
export type SessionActions = {
  /** 重新按当前 token 拉 /api/auth/me；无 token 或探测失败 → 回匿名本地会话。 */
  refresh: () => Promise<void>;
  /** 退出登录：清 token 并回匿名本地会话（调用方负责跳转登录页）。 */
  logout: () => void;
};

type SessionContextValue = SessionActions & { session: Session | null };

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);

  const refresh = useCallback(async () => {
    const token =
      typeof window !== "undefined" ? window.localStorage.getItem("bok_token") : null;
    if (!token) {
      setSession(ANON_SESSION);
      return;
    }
    try {
      const info = await api.me();
      setSession({ ...info, anonymous: false });
    } catch {
      // token 过期/无效 → 回匿名本地会话（不在此清 token：401 统一由 request() 处理）。
      setSession(ANON_SESSION);
    }
  }, []);

  const logout = useCallback(() => {
    if (typeof window !== "undefined") window.localStorage.removeItem("bok_token");
    setSession(ANON_SESSION);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(() => ({ session, refresh, logout }), [session, refresh, logout]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session | null {
  return useContext(SessionContext)?.session ?? null;
}

/** 会话动作（刷新/退出）。Provider 之外返回空实现，组件不因缺 Provider 抛错。 */
const NOOP_ACTIONS: SessionActions = { refresh: async () => {}, logout: () => {} };

export function useSessionActions(): SessionActions {
  return useContext(SessionContext) ?? NOOP_ACTIONS;
}

/** 页面键可见性：匿名=全可见；admin/root=全可见；user=有效集含 key。 */
export function hasPage(session: Session | null, key: PageKey): boolean {
  if (!session) return false;
  if (session.anonymous) return true;
  if (session.role === "admin" || session.role === "root") return true;
  return session.permissions.includes(key);
}

/** 主管能力（契约 §3/§4）：匿名本地模式=单机全权；admin/root=主管；其余（含未加载）false。 */
export function isManager(session: Session | null): boolean {
  if (!session) return false;
  if (session.anonymous) return true;
  return session.role === "admin" || session.role === "root";
}
