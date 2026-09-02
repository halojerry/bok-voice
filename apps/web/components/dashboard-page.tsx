"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { AppShell, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { friendlyErrorText, useControlPlaneReady } from "@/lib/api-ready";

export function DashboardPage() {
  return (
    <AppShell>
      <DashboardContent />
    </AppShell>
  );
}

function DashboardContent() {
  const { accountId, health } = useAccount();
  const cp = useControlPlaneReady();
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [objects, setObjects] = useState<Record<string, unknown>[]>([]);
  const [calls, setCalls] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const loadAttemptRef = useRef(-1);

  useEffect(() => {
    // 冷启动自愈：每次 Control Plane 离线→就绪转换时自动重拉一次。
    if (loadAttemptRef.current === cp.attempt) return;
    loadAttemptRef.current = cp.attempt;
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.reportsSummary(),
      api.listObjects(accountId),
      api.listCalls(accountId, ""),
    ])
      .then(([s, o, c]) => {
        if (cancelled) return;
        setSummary(s as Record<string, unknown>);
        setObjects(Array.isArray(o) ? o : []);
        setCalls(Array.isArray(c) ? c : []);
        setErr(null);
      })
      .catch((e) => {
        if (!cancelled) setErr(friendlyErrorText(String(e)));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, cp.attempt]);

  const cards: [string, string][] = [
    ["活跃通话", String(summary?.active_calls ?? "—")],
    ["已结算", String(summary?.settled_calls ?? "—")],
    ["对象数量", String(objects.length)],
    ["最近会话", String(calls.length)],
  ];

  return (
    <>
      <div className="mb-8">
        <h1 className="page-title">工作台</h1>
        <p className="page-sub">实时状态、活跃通话与业务入口</p>
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

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1fr_340px]">
            <section className="card">
              <div className="flex items-center justify-between">
                <span className="label">最近会话</span>
                <Link href="/calls" className="text-xs text-[var(--accent)]">查看全部 →</Link>
              </div>
              <div className="mt-3 space-y-2">
                {calls.slice(0, 6).map((call) => (
                  <Link
                    key={String(call.id ?? call.call_id)}
                    href={`/calls/${String(call.id ?? call.call_id)}`}
                    className="flex items-center justify-between rounded-lg bg-white/5 px-4 py-3 hover:bg-white/10"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{String(call.object_id ?? "-")}</p>
                      <p className="text-xs text-[var(--muted)]">
                        {String(call.status ?? "-")} · {String(call.mode ?? "-")} · {String(call.language ?? "-")}
                      </p>
                    </div>
                    <span className="text-[var(--accent)]">进入 →</span>
                  </Link>
                ))}
                {calls.length === 0 && <p className="text-sm text-[var(--muted)]">暂无会话，从「新建通话」开始。</p>}
              </div>
            </section>

            <section className="card">
              <span className="label">快捷入口</span>
              <div className="mt-3 grid grid-cols-1 gap-2">
                <Link href="/calls/new" className="btn-primary w-full">+ 新建通话</Link>
                <Link href="/objects" className="btn-ghost w-full">对象管理</Link>
                <Link href="/knowledge" className="btn-ghost w-full">知识库</Link>
                <Link href="/supervisor" className="btn-ghost w-full">主管台</Link>
                <Link href="/settings" className="btn-ghost w-full">设置</Link>
              </div>
              <div className="mt-4 rounded-lg bg-white/5 p-3 text-xs text-[var(--muted)]">
                控制面状态：{health === false ? "离线" : health === true ? "在线" : "未知"}
              </div>
            </section>
          </div>
        </>
      )}
    </>
  );
}
