"use client";

/**
 * web 端统一 Logger 装配（2026-09-18）：把 lib/logger.ts 接进应用生命周期。
 *
 * - setupClientLogging()：安装全局兜底（window.onerror / unhandledrejection）
 *   + 配置 error 自动上报通道。落点复用 CP 诊断通道 POST /api/web_logs
 *   （weblog.ts 同款端点，event=log_report、data=完整上报 payload 落
 *   logs/web-client.log）——后端零改动。上报失败由 logger 内部隔离为本地
 *   警告（channel=log-reporter），不递归、不阻塞。
 * - bindSessionLogging()：会话解析后把当前用户身份挂进 logger
 *   （后续 startTrace 的默认 user + 上报 payload 用户上下文）。
 */
import { apiBase, authHeaders } from "@/lib/api";
import { configureReporting, setTraceUser } from "@/lib/logger";

export function setupClientLogging(): void {
  configureReporting({
    environment: "browser",
    transport: (payload) => {
      return fetch(`${apiBase()}/api/web_logs`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ event: "log_report", call_id: "", data: payload }),
        keepalive: true,
      }).then((res: Response) => {
        if (!res.ok) throw new Error(`log report endpoint ${res.status}`);
        return res;
      });
    },
  });
}

export function bindSessionLogging(
  session: { user_id?: string; username?: string; display_name?: string; role?: string } | null | undefined,
): void {
  setTraceUser(
    session?.user_id
      ? {
          userId: session.user_id,
          username: session.username ?? "",
          displayName: session.display_name ?? "",
          role: session.role ?? "",
        }
      : undefined,
  );
}
