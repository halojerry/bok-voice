"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";

type CallRow = Record<string, unknown> & { id?: string; call_id?: string; status?: string };

export default function SupervisorPage() {
  const [rows, setRows] = useState<CallRow[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.activeCalls();
      setRows((Array.isArray(data) ? data : []) as CallRow[]);
      setErr(null);
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  const idOf = (c: CallRow) => String(c.id ?? c.call_id ?? "");
  const labelOf = (c: CallRow) => String(c.object_name ?? c.call_id ?? c.id ?? "-");

  return (
    <div>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="page-title">主管台</h1>
          <p className="page-sub">
            只读监控（进行中通话 / 暂停状态）。暂停 AI · 接管 · 转人工 · 挂断请到「通话会话」进入该通话的工作台操作。
          </p>
        </div>
        <button className="btn-ghost" onClick={() => refresh()}>刷新</button>
      </div>

      {err && <p className="mb-4 rounded-lg bg-red-500/10 p-3 text-sm text-red-300">{err}</p>}

      <section className="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold">通话</h2>
          <span className="text-xs text-[var(--muted)]">{rows.length} 路</span>
        </div>
        {rows.length === 0 ? (
          <p className="text-sm text-[var(--muted)]">暂无通话。有进行中(active)或已暂停(paused)的通话会显示在这里。</p>
        ) : (
          <div className="space-y-3">
            {rows.map((c) => {
              const id = idOf(c);
              const status = String(c.status ?? "active");
              const paused = status === "paused" || Boolean(c.escalated_to_human);
              return (
                <div key={id} className="rounded-lg bg-white/5 p-4">
                  <div className="flex items-center justify-between">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{labelOf(c)}</p>
                      <p className="text-xs text-[var(--muted)]">{status} · {id}</p>
                    </div>
                    <span
                      className={`inline-flex shrink-0 items-center gap-1.5 text-xs ${
                        paused ? "text-amber-400" : "text-emerald-400"
                      }`}
                    >
                      <span className={`h-2 w-2 rounded-full animate-pulse ${paused ? "bg-amber-400" : "bg-emerald-400"}`} />
                      {paused ? "AI 已暂停" : "进行中"}
                    </span>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Link href="/calls" className="btn-ghost text-xs">
                      进入工作台（暂停/接管/挂断）
                    </Link>
                  </div>
                </div>
              );
            })}
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
