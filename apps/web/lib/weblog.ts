"use client";

/**
 * web 客户端关键事件上报（诊断通道，2026-09-12）。
 *
 * 同传控制台的设备枚举/自动分配/sink 路由/麦克风开关决策只发生在浏览器里，
 * 服务端日志全然看不见——排障只能靠排除法。wlog() 把这些决策打到 CP
 * POST /api/web_logs 落盘 logs/web-client.log（与 agent.log 同目录）。
 * fire-and-forget + keepalive：失败永不影响功能、永不抛错。
 *
 * 自保铁律（2026-09-18 静默 catch 收编）：weblog 本身就是诊断上报通道，与
 * lib/logger.ts 的自动上报落同一个端点——weblog 的失败若走 logger.error 会形成
 * 「上报器失败→再上报→再失败」递归。故本地失败只打 console.warn 的 JSON 行
 * （channel=weblog，镜像 logger safeWarn 形状），**绝不 import logger**。
 */
import { apiBase, authHeaders } from "@/lib/api";

let callId = "";

/** 绑定当前会话 id（一体台进房后设一次，随每条事件带上）。 */
export function wlogBindCall(id: string) {
  callId = id;
}

/** 错误 → 可序列化形状（本地自保打点用；与 logger serializeError 同构、不引依赖）。 */
function errShape(err: unknown): { name: string; message: string; stack?: string } {
  if (err instanceof Error) {
    const out: { name: string; message: string; stack?: string } = {
      name: err.name,
      message: err.message,
    };
    if (err.stack) out.stack = err.stack;
    return out;
  }
  return { name: "Error", message: String(err) };
}

/** weblog 自身故障的显式打点：raw console.warn + JSON。刻意不走 logger——
 * 日志上报通道的失败再走日志上报即递归（logger safeWarn 同款自保语义）。 */
function weblogSelfWarn(message: string, event: string, err: unknown): void {
  console.warn(
    JSON.stringify({
      ts: new Date().toISOString(),
      level: "warn",
      channel: "weblog",
      event,
      call_id: callId,
      message,
      error: errShape(err),
    }),
  );
}

export function wlog(event: string, data?: unknown) {
  try {
    void fetch(`${apiBase()}/api/web_logs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ event, call_id: callId, data }),
      keepalive: true,
    }).catch((err: unknown) => weblogSelfWarn("weblog post failed", event, err));
  } catch (err) {
    /* SSR/离线等极端场景静默 */
    weblogSelfWarn("weblog post threw", event, err);
  }
}
