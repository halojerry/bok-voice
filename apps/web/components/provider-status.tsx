"use client";

/**
 * 容灾可观测波（feat/dr-observability，契约 §6）· Provider 状态呈现件。
 * 两处消费（判定/色调全在 lib/provider-status.ts 纯函数，此处只画）：
 *   ① CallStudio 右栏「Provider 服务状态」卡（useProviderMetrics 3s 轮询）
 *   ② root 容灾面板（数据来自 /api/ops/disaster-status，同样本组件渲染四行）
 * Ethan 定稿格式（严格照抄行文案；前导 emoji=灯）：
 *   ASR   🟢 已连接 · 412ms (p95 490)
 *   LLM   🟢 已连接 · 680ms (p95 1240)
 *   TTS   🟢 已连接 · 310ms (p95 450)
 *   VAD   🟢 已连接 · 18ms
 *   ─────────────────────────   ← 实现为 1px 分隔线（视觉等价）
 *   状态: 🟢 健康 (EMA 0.6s)
 */

import { useEffect, useRef, useState } from "react";
import { api, type ProviderMetricsReport } from "@/lib/api";
import {
  LEVEL_DOT,
  LEVEL_TEXT_CLASS,
  famineLine,
  providerLevel,
  providerValueText,
  type FamineLike,
  type ProviderStatLike,
} from "@/lib/provider-status";

/** 契约四 kind（顺序即卡片行序）。 */
export const PROVIDER_FIELDS: [string, string][] = [
  ["asr", "ASR"],
  ["llm", "LLM"],
  ["tts", "TTS"],
  ["vad", "VAD"],
];

/**
 * Provider 卡轮询钩子（3s）：请求失败**静默保持上次值**（卡不闪）——CP 抖动/
 * 冷启动期间读数保持最后已知值，直到下一次成功刷新。callId 经 ref 透传，
 * 不因切换通话重建定时器（通话切换时 CallStudio 本就按 epoch 重挂）。
 */
export function useProviderMetrics(callId?: string): ProviderMetricsReport | null {
  const [metrics, setMetrics] = useState<ProviderMetricsReport | null>(null);
  const callIdRef = useRef<string>("");
  callIdRef.current = callId ?? "";
  useEffect(() => {
    let stopped = false;
    let busy = false;
    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        const m = await api.providerMetrics(callIdRef.current || undefined);
        if (!stopped) setMetrics(m);
      } catch {
        /* 静默：保持上次值（不闪、不清空） */
      } finally {
        busy = false;
      }
    };
    void load();
    const t = setInterval(load, 3000);
    return () => {
      stopped = true;
      clearInterval(t);
    };
  }, []);
  return metrics;
}

/** 四 provider 行（label + 灯 + 「已连接 · 412ms (p95 490)」）。 */
export function ProviderLines({ providers }: { providers?: Record<string, ProviderStatLike> | null }) {
  return (
    <div className="space-y-1 text-sm">
      {PROVIDER_FIELDS.map(([kind, label]) => {
        const stat = providers ? (providers[kind] ?? null) : null;
        const level = providerLevel(kind, stat);
        return (
          <p key={kind} className="flex justify-between gap-2">
            <span className="muted">{label}</span>
            <span className={`inline-flex items-center gap-1.5 ${LEVEL_TEXT_CLASS[level]}`}>
              <span aria-hidden="true">{LEVEL_DOT[level]}</span>
              {providerValueText(stat)}
            </span>
          </p>
        );
      })}
    </div>
  );
}

/** 状态行：healthy/famine/downgraded/dialing_paused → 灯 + 文案（EMA 1 位小数）。 */
export function FamineStatusLine({ famine }: { famine?: FamineLike | null }) {
  const line = famineLine(famine);
  return (
    <p className="text-sm">
      <span className="muted">状态: </span>
      <span className={`inline-flex items-center gap-1.5 ${LEVEL_TEXT_CLASS[line.level]}`}>
        <span aria-hidden="true">{line.dot}</span>
        {line.text}
      </span>
    </p>
  );
}
