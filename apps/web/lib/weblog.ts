"use client";

/**
 * web 客户端关键事件上报（诊断通道，2026-09-12）。
 *
 * 同传控制台的设备枚举/自动分配/sink 路由/麦克风开关决策只发生在浏览器里，
 * 服务端日志全然看不见——排障只能靠排除法。wlog() 把这些决策打到 CP
 * POST /api/web_logs 落盘 logs/web-client.log（与 agent.log 同目录）。
 * fire-and-forget + keepalive：失败静默，诊断通道永不影响功能、永不抛错。
 */
import { apiBase } from "@/lib/api";

let callId = "";

/** 绑定当前会话 id（一体台进房后设一次，随每条事件带上）。 */
export function wlogBindCall(id: string) {
  callId = id;
}

export function wlog(event: string, data?: unknown) {
  try {
    void fetch(`${apiBase()}/api/web_logs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, call_id: callId, data }),
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* SSR/离线等极端场景静默 */
  }
}
