"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const METRICS = [
  { label: "今日通话", value: "12", note: "较昨日 +2" },
  { label: "活跃会话", value: "3", note: "实时" },
  { label: "结算队列", value: "5", note: "待处理" },
  { label: "知识命中率", value: "94%", note: "近 7 天" },
  { label: "平均首包", value: "0.8s", note: "端到端" },
  { label: "坐席接管", value: "1", note: "主管台" },
];

export default function DashboardPage() {
  const [active, setActive] = useState<Record<string, unknown>[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.activeCalls().then(setActive).catch((e) => setErr(String(e)));
  }, []);

  return (
    <div>
      <div className="mb-8">
        <h1 className="page-title">总览</h1>
        <p className="page-sub">多账号客服语音助手 · 实时状态</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        {METRICS.map((m) => (
          <div key={m.label} className="card">
            <p className="label">{m.label}</p>
            <p className="mt-2 text-3xl font-semibold">{m.value}</p>
            <p className="mt-1 text-xs text-[var(--muted)]">{m.note}</p>
          </div>
        ))}
      </div>

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="card">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-semibold">活跃会话</h2>
            <a href="/supervisor" className="text-xs text-[var(--accent)]">进入主管台 →</a>
          </div>
          {err && <p className="text-sm text-red-300">{err}</p>}
          <div className="space-y-2">
            <p className="text-sm text-[var(--muted)]">暂无活跃通话（示例数据）。</p>
            {active.map((c) => (
              <div key={String(c.call_id)} className="rounded-xl bg-white/5 p-3 text-sm">
                {String(c.call_id)} · {String(c.object_name ?? "-")}
              </div>
            ))}
          </div>
        </section>

        <section className="card">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-semibold">服务健康</h2>
            <span className="inline-flex items-center gap-1.5 text-xs text-emerald-400">
              <span className="h-2 w-2 rounded-full bg-emerald-400" /> 全部运行中
            </span>
          </div>
          <div className="grid grid-cols-2 gap-2 text-sm">
            {[
              ["LiveKit Server", "7880"],
              ["Control Plane", "8000"],
              ["Agent Worker", "注册中"],
              ["Postgres", "pgvector"],
            ].map(([k, v]) => (
              <div key={k} className="rounded-xl bg-white/5 p-3">
                <p className="text-[var(--muted)]">{k}</p>
                <p className="mt-1 text-neutral-300">{v}</p>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
