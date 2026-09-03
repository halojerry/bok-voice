"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

/**
 * 打包客户端冷启动时，桌面壳异步拉起整栈服务需数十秒；本 hook 持续探测
 * Control Plane 的 /health，让页面在服务就绪后自动重载数据，而不是把
 * 「TypeError: Load failed」这类原始网络错误直接抛给用户。
 *
 * state: checking=首次探测中 / ready=服务在线 / starting=在线前等待 /
 *        offline=超过 offlineAfterMs 仍不可达
 * attempt: 每经历一次「离线 → 在线」转换 +1，可作为数据加载 effect 的依赖，
 *          实现在线后自动重拉。配合调用方的 loadedRef 去重，避免重复请求。
 */

export type ControlPlaneState = "checking" | "ready" | "starting" | "offline";

export function useControlPlaneReady(opts?: { intervalMs?: number; offlineAfterMs?: number }) {
  const intervalMs = opts?.intervalMs ?? 1500;
  const offlineAfterMs = opts?.offlineAfterMs ?? 90_000;
  const [state, setState] = useState<ControlPlaneState>("checking");
  const [attempt, setAttempt] = useState(0);
  const lastOkRef = useRef(false);
  const failSinceRef = useRef(0);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      let ok = false;
      try {
        ok = Boolean((await api.health()).ok);
      } catch {
        ok = false;
      }
      if (disposed) return;
      const now = Date.now();
      if (ok) {
        failSinceRef.current = 0;
        if (!lastOkRef.current) {
          lastOkRef.current = true;
          setAttempt((a) => a + 1);
        }
        setState("ready");
      } else {
        lastOkRef.current = false;
        if (!failSinceRef.current) failSinceRef.current = now;
        setState(now - failSinceRef.current >= offlineAfterMs ? "offline" : "starting");
      }
      timer = setTimeout(tick, intervalMs);
    };

    void tick();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [intervalMs, offlineAfterMs]);

  return { state, ready: state === "ready", attempt };
}

const NETWORK_ERROR_RE = /failed to fetch|load failed|networkerror|fetch failed|typeerror/i;
const STATUS_RE = /\b(\d{3})\b/;

/**
 * 后端 detail 已随状态码拼进 message（"503 MiniMax API Key 未配置…"）时，
 * 直接展示真实原因；只有裸状态码/statusText 才落到通用文案。
 */
function splitDetail(text: string): { code: number | null; rest: string } {
  const m = text.match(STATUS_RE);
  if (!m) return { code: null, rest: "" };
  const code = Number(m[1]);
  const rest = text.slice(m.index! + m[0].length).replace(/^[:：,，\s]+/, "").trim();
  return { code, rest };
}

/** 把 ErrorState 等页面收到的字符串错误映射成可读中文（不抛异常）。 */
export function friendlyErrorText(raw: string): string {
  const text = String(raw ?? "").trim();
  if (!text) return "请求失败。";
  if (NETWORK_ERROR_RE.test(text)) {
    return "无法连接本地 Control Plane（127.0.0.1:8000）。请确认桌面服务已启动；若应用刚打开，请稍候几秒自动重试。";
  }
  const { code, rest } = splitDetail(text);
  if (code !== null) {
    if (code === 503) {
      // 带后端 detail 时优先展示真实原因（如缺 MiniMax key），别误导成 LiveKit。
      if (rest && !/^service unavailable$/i.test(rest)) return `Control Plane 服务暂不可用：${rest}`;
      return "Control Plane 暂时不可用（可能仍在启动，或缺少 LiveKit 凭据）。请稍后重试。";
    }
    if (code >= 500) return `本地服务错误（HTTP ${code}${rest ? `：${rest}` : ""}）。请到「本机桌面服务」查看日志。`;
    if (code === 404) return rest || "请求的资源不存在（可能已被删除）。";
    if (code === 401 || code === 403) return rest || "没有权限执行该操作。";
  }
  return text;
}

/** 把任意异常（含 TypeError）映射成可读中文。 */
export function friendlyApiError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  return friendlyErrorText(msg || "未知错误");
}

/** 接通（connect）失败时按阶段给出可定位的中文提示，真实原因始终保留到 console。 */
export function describeConnectError(err: unknown, phase: "create-call" | "join-session"): string {
  const raw = err instanceof Error ? err.message : String(err ?? "");
  console.error(`connect ${phase} failed`, err);
  if (err instanceof TypeError || NETWORK_ERROR_RE.test(raw)) {
    return "无法连接本地服务（Control Plane / LiveKit）。请确认桌面服务已启动，或到「本机桌面服务」查看状态。";
  }
  if (raw.includes("503")) {
    return "Control Plane 未能签发通话令牌（缺少 LiveKit 凭据或仍在启动）。请查看服务日志后重试。";
  }
  if (raw.includes("401") || /token|jwt|decode/i.test(raw)) {
    return "LiveKit 令牌校验失败，请确认 LIVEKIT_API_KEY/SECRET 已正确注入。";
  }
  return `接通失败：${raw}`;
}
