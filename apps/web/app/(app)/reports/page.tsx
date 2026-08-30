"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/app-shell";

export default function ReportsPage() {
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [calls, setCalls] = useState<Record<string, unknown>[]>([]);
  const [usage, setUsage] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.reportsSummary(), api.reportsCalls(), api.reportsUsage()])
      .then(([s, c, u]) => {
        setSummary(s as Record<string, unknown>);
        setCalls(Array.isArray(c) ? c : []);
        setUsage(u as Record<string, unknown>);
        setErr(null);
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const cards: [string, string][] = [
    ["通话总数", String(summary?.total_calls ?? "—")],
    ["活跃通话", String(summary?.active_calls ?? "—")],
    ["已结算", String(summary?.settled_calls ?? "—")],
    ["总轮次", String(summary?.total_turns ?? "—")],
  ];

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">报表</h1>
        <p className="page-sub">用量 · 通话 · 结算 · Provider 明细</p>
      </div>

      {err && <ErrorState message={err} />}
      {loading ? (
        <LoadingState />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            {cards.map(([k, v]) => (
              <div key={String(k)} className="card">
                <p className="label">{k}</p>
                <p className="mt-2 text-2xl font-semibold">{String(v)}</p>
              </div>
            ))}
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1fr_360px]">
            <section className="card">
              <span className="label">通话记录</span>
              <table className="mt-3 w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-[var(--muted)]">
                    <th className="pb-2">对象</th>
                    <th className="pb-2">状态</th>
                    <th className="pb-2">模式</th>
                    <th className="pb-2">语言</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.map((c) => (
                    <tr key={String(c.id)} className="border-t border-[var(--card-border)]">
                      <td className="py-2">{String(c.object_id ?? "-")}</td>
                      <td className="py-2 text-[var(--muted)]">{String(c.status ?? "-")}</td>
                      <td className="py-2">{String(c.mode ?? "-")}</td>
                      <td className="py-2">{String(c.language ?? "-")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>

            <section className="card">
              <span className="label">Provider 用量</span>
              <div className="mt-3 space-y-2 text-sm">
                {[
                  ["ASR", usage?.asr_calls],
                  ["LLM tokens", usage?.llm_tokens],
                  ["TTS", usage?.tts_calls],
                  ["VAD", usage?.vad_calls],
                ].map(([k, v]) => (
                  <div key={String(k)} className="flex justify-between">
                    <span className="text-[var(--muted)]">{String(k)}</span>
                    <span>{String(v ?? "—")}</span>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
