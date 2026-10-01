"use client";

/**
 * 容灾处置面板（feat/dr-observability 契约 §4/§6，root 专属平台面）。
 *
 * 位置选择说明（协同方要求「看哪个改动小选哪个，写注释说明」）：选 **rootOnly 导航
 * 新入口 + 独立页**——navigation.ts 记一行 + 本文件即成；/nodes 先例的三条通路
 * （RouteGuard `kind:"root"` 门、侧栏 navVisible 过滤、顶栏 pathname→标题反查）
 * 全部自动生效，壳层零改动。不塞进 /settings：设置页是「配置」面（且为属主专属
 * ownerOnly，语义不同），容灾是「处置」面；四钮是应急开关，埋进 settings 长表单
 * 会延迟操作。
 *
 * 数据：GET /api/ops/disaster-status 3s 轮询——读失败**保留上次值**（面板不闪空）
 * 并在顶部亮一行提示，下一轮成功自愈。动作：POST /api/ops/disaster-override
 * 四钮，每钮 window.confirm 二次确认（仓内 confirmText/ window.confirm 先例：
 * table-import.tsx / intent-manager.tsx / app/(app)/nodes/page.tsx）。
 */

import { useCallback, useEffect, useState } from "react";
import { api, type DisasterOverrideAction, type DisasterStatusReport } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { FamineStatusLine, ProviderLines } from "@/components/provider-status";
import { LEVEL_DOT, LEVEL_TEXT_CLASS, sinceDuration, swapLevel } from "@/lib/provider-status";
import { useSession } from "@/components/session-context";
import { friendlyErrorText } from "@/lib/api-ready";

/** 手动覆盖四钮（契约 §3 动作名逐字；顺序=Ethan 定稿）。 */
const OVERRIDES: {
  action: DisasterOverrideAction;
  label: string;
  hint: string;
  confirm: string;
}[] = [
  {
    action: "force_downgrade",
    label: "强制降档",
    hint: "a_reply 车道 overlay 成 4B（新通话下一通生效）",
    confirm: "确认强制降档？a_reply 车道将被 overlay 成 4B（下一通生效），直到手动「恢复自动」。",
  },
  {
    action: "force_healthy",
    label: "恢复自动",
    hint: "解除手动覆盖，饥荒状态机重新接管",
    confirm: "确认恢复自动？手动覆盖将解除，饥荒状态机（EMA/持续时长判定）重新接管。",
  },
  {
    action: "pause_dialing",
    label: "暂停外呼",
    hint: "新建单/派发被拒；在途通话不受影响",
    confirm: "确认暂停外呼？新建单与外呼派发将被拒绝（在途通话不受影响），直到「恢复外拨」。",
  },
  {
    action: "resume_dialing",
    label: "恢复外拨",
    hint: "解除外呼闸门",
    confirm: "确认恢复外拨？暂停外呼的准入闸门解除（饥荒自动闸仍按状态机生效）。",
  },
];

/** ISO/任意时间串 → 「YYYY-MM-DD HH:MM:SS」（非 ISO 原样截断）。 */
function fmtTs(ts: string): string {
  const s = String(ts ?? "");
  if (!s) return "—";
  return s.includes("T") ? s.slice(0, 19).replace("T", " ") : s.slice(0, 19);
}

export default function DisasterPage() {
  const session = useSession();
  const [data, setData] = useState<DisasterStatusReport | null>(null);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");

  /** 平台面：仅登录 root 可用（匿名本地会话不算 root，与 /nodes 同判据）。 */
  const isRoot = Boolean(session && !session.anonymous && session.role === "root");

  const refresh = useCallback(async () => {
    try {
      const d = await api.disasterStatus();
      setData(d);
      setErr("");
    } catch (e) {
      // 读失败保留上次值（不闪空），亮一行原因；下一轮成功即清
      setErr(friendlyErrorText(String(e)));
    }
  }, []);

  useEffect(() => {
    if (!isRoot) return;
    void refresh();
    const t = setInterval(() => void refresh(), 3000);
    return () => clearInterval(t);
  }, [isRoot, refresh]);

  async function runOverride(action: DisasterOverrideAction, label: string, confirmText: string) {
    if (!window.confirm(confirmText)) return;
    setBusy(action);
    setNotice("");
    try {
      await api.disasterOverride(action);
      setNotice(`已执行「${label}」；面板读数随下一轮轮询（≤3s）刷新。`);
      await refresh();
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    } finally {
      setBusy("");
    }
  }

  if (!session) return <LoadingState label="正在读取会话…" />;
  if (!isRoot) {
    return (
      <div className="card space-y-2">
        <span className="label">无权限</span>
        <p className="text-sm">容灾面板仅超级管理员（root）可用，请使用 root 账号登录。</p>
        <p className="text-xs muted">手动覆盖会改变全节点的模型档位与外呼准入，管理员与话务员不可见。</p>
      </div>
    );
  }

  const famine = data?.famine ?? null;
  const swapUsed = data?.memory?.swap_used_gb;
  const swapThreshold = data?.memory?.threshold_gb ?? 8;
  const swapLv = swapLevel(swapUsed, swapThreshold);
  const servers = Array.isArray(data?.servers) ? data.servers : [];
  // 事件流：contract 后端给最近 20 条（新→旧或旧→新均可能），此处钉「最新在前 + 取 8 条」
  const events = (Array.isArray(data?.recent_events) ? data.recent_events : []).slice(-8).reverse();
  const ema = typeof famine?.ema_s === "number" && Number.isFinite(famine.ema_s)
    ? `${famine.ema_s.toFixed(1)}s`
    : "—";
  const holding = famine && String(famine.level ?? "") !== "healthy" && famine.since
    ? sinceDuration(famine.since)
    : "";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="page-title">容灾</h1>
          <p className="page-sub">饥荒监视 · Provider 读数 · 手动覆盖（root 专属处置面）</p>
        </div>
        <button className="btn-ghost text-xs" disabled={data === null} onClick={() => void refresh()}>
          刷新
        </button>
      </div>

      {err && <ErrorState message={`读取失败（显示上次数据）：${err}`} />}
      {notice && <p className="text-sm text-emerald-600">{notice}</p>}

      {data === null ? (
        <section className="card">
          <LoadingState />
        </section>
      ) : (
        <>
          <section className="card space-y-2">
            <span className="label">饥荒监视（CP 单一真源）</span>
            <FamineStatusLine famine={famine} />
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs muted sm:grid-cols-4">
              <span>EMA：{ema}</span>
              <span>持续：{holding || "—"}</span>
              <span>手动覆盖：{famine?.manual_override ? String(famine.manual_override) : "无（自动）"}</span>
              <span>在途通话：{Number(data.active_calls ?? 0)}</span>
            </div>
            <p className="text-[11px] muted">
              EMA ≥4s（BOK_LLM_FAMINE_TTFT_S）持续 10s → 自动降档（a_reply overlay 成 4B）+ 暂停外呼；
              EMA 回落至阈值以下持续 30s → 回健康。降档/停拨中新建单返回 409，在途通话不受影响。
            </p>
          </section>

          <section className="card space-y-2">
            {/* §4 响应不带 window_s（§2 才有）——这里钉契约常量 300s 写明口径 */}
            <span className="label">Provider 读数（全局滚动窗 300s）</span>
            <ProviderLines providers={data.providers} />
          </section>

          <section className="card space-y-2">
            <span className="label">系统</span>
            <p className="flex justify-between gap-2 text-sm">
              <span className="muted">Swap</span>
              <span className={`inline-flex items-center gap-1.5 ${LEVEL_TEXT_CLASS[swapLv]}`}>
                <span aria-hidden="true">{LEVEL_DOT[swapLv]}</span>
                {typeof swapUsed === "number" ? `${swapUsed.toFixed(1)}GB` : "无数据"}
                <span className="muted">/ 阈值 {Number(swapThreshold)}GB</span>
              </span>
            </p>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
              {servers.length === 0 && <span className="muted">暂无 servers 数据</span>}
              {servers.map((s) => (
                <span key={`${s.name}:${s.port}`} className="flex items-center gap-1.5">
                  <span aria-hidden="true">{s.up ? "🟢" : "🔴"}</span>
                  <span className="font-mono text-xs">{s.name}</span>
                  <span className="muted font-mono text-[11px]">:{s.port}</span>
                </span>
              ))}
            </div>
          </section>

          <section className="card space-y-2">
            <span className="label">最近事件（最新 8 条）</span>
            {events.length === 0 ? (
              <EmptyState label="暂无事件记录" />
            ) : (
              <ul className="space-y-1 text-xs">
                {events.map((e, i) => (
                  <li key={`${e.ts}:${e.event}:${i}`} className="flex gap-2">
                    <span className="shrink-0 font-mono muted">{fmtTs(e.ts)}</span>
                    <span className="shrink-0 font-mono">{e.event}</span>
                    {e.detail && (
                      <span className="min-w-0 truncate muted" title={JSON.stringify(e.detail)}>
                        {JSON.stringify(e.detail)}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="card space-y-3">
            <div>
              <span className="label">手动覆盖（优先于自动状态机）</span>
              <p className="mt-1 text-[11px] muted">每钮二次确认后生效，全部记审计 `ops.famine_override`。</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {OVERRIDES.map((o) => (
                <button
                  key={o.action}
                  type="button"
                  className={`btn-ghost text-xs ${o.action === "force_downgrade" || o.action === "pause_dialing" ? "text-red-600" : ""}`}
                  title={o.hint}
                  disabled={busy !== ""}
                  onClick={() => void runOverride(o.action, o.label, o.confirm)}
                >
                  {busy === o.action ? "执行中…" : o.label}
                </button>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
