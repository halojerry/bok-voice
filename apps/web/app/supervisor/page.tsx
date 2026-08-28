"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

export default function SupervisorPage() {
  const [calls, setCalls] = useState<Record<string, unknown>[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function refresh() {
    try {
      const data = await api.activeCalls();
      setCalls(Array.isArray(data) ? data : []);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function send(action: (id: string) => Promise<unknown>, id: string) {
    setErr(null);
    setNotice(null);
    try {
      await action(id);
      await refresh();
      setNotice("操作已执行。");
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="page-title">主管台</h1>
          <p className="page-sub">进房旁听 · 暂停 AI · 接管 · 转人工</p>
        </div>
        <button className="btn-ghost" onClick={refresh}>刷新</button>
      </div>

      {err && <p className="mb-4 text-sm text-red-300">{err}</p>}
      {notice && <p className="mb-4 text-sm text-emerald-400">{notice}</p>}

      <section className="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold">活跃通话</h2>
          <span className="text-xs text-[var(--muted)]">{calls.length} 路</span>
        </div>
        {calls.length === 0 ? (
          <p className="text-sm text-[var(--muted)]">暂无活跃通话。</p>
        ) : (
          <div className="space-y-3">
            {calls.map((c) => (
              <div key={String(c.call_id ?? c.id)} className="rounded-xl bg-white/5 p-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="font-medium">{String(c.object_name ?? c.call_id ?? "-")}</p>
                    <p className="text-xs text-[var(--muted)]">{String(c.status ?? "live")} · {String(c.id ?? c.call_id)}</p>
                  </div>
                  <span className="inline-flex items-center gap-1.5 text-xs text-emerald-400">
                    <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" /> 进行中
                  </span>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Link
                    className="btn-ghost text-xs"
                    href={`/calls/${String(c.id ?? c.call_id)}`}
                  >
                    进入会话
                  </Link>
                  <button className="btn-ghost text-xs" onClick={() => send(api.supervisorJoin, String(c.id ?? c.call_id))}>监听</button>
                  <button className="btn-ghost text-xs" onClick={() => send(api.supervisorPause, String(c.id ?? c.call_id))}>暂停 AI</button>
                  <button className="btn-ghost text-xs" onClick={() => send(api.supervisorTakeover, String(c.id ?? c.call_id))}>接管</button>
                  <button className="btn-ghost text-xs" onClick={() => send(api.supervisorTransfer, String(c.id ?? c.call_id))}>转人工</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="card">
          <span className="label">质量监控</span>
          <p className="mt-3 text-sm text-[var(--muted)]">表达密度 / 填充词 / 犹豫词 / 打断成功率。接结算后展示。</p>
        </section>
        <section className="card">
          <span className="label">纪律控制</span>
          <p className="mt-3 text-sm text-[var(--muted)]">provider 降级状态机、熔断、回切、审计。MVP 骨架。</p>
        </section>
      </div>
    </div>
  );
}
