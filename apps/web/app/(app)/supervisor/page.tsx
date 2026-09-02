"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";

type CallRow = Record<string, unknown> & { id?: string; call_id?: string; status?: string };

export default function SupervisorPage() {
  const [rows, setRows] = useState<CallRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  // 每行局部状态：busy 中 / 操作结果 / 错误
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [rowMsg, setRowMsg] = useState<Record<string, string>>({});
  const [rowErr, setRowErr] = useState<Record<string, string>>({});

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
    // 主管台轮询：暂停/恢复/挂断后的状态变化能及时反映。
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  async function act(id: string, kind: "pause" | "resume" | "takeover" | "transfer" | "hangup", okText: string) {
    setBusy((b) => ({ ...b, [id]: true }));
    setRowErr((m) => ({ ...m, [id]: "" }));
    setRowMsg((m) => ({ ...m, [id]: "" }));
    try {
      const fn =
        kind === "pause" ? api.supervisorPause
        : kind === "resume" ? api.supervisorResume
        : kind === "takeover" ? api.supervisorTakeover
        : kind === "transfer" ? api.supervisorTransfer
        : api.hangup;
      const r = await fn(id);
      const status = (r as { status?: string })?.status;
      setRowMsg((m) => ({ ...m, [id]: `${okText}${status ? `（状态：${status}）` : ""}` }));
      // 挂断/转人工后通话已结束，主动从列表移除（后端 status 过滤也会同步）。
      if (kind === "hangup" || kind === "transfer") {
        setRows((prev) => prev.filter((c) => String(c.id ?? c.call_id) !== id));
      }
      await refresh();
    } catch (e) {
      setRowErr((m) => ({ ...m, [id]: friendlyErrorText(String(e)) }));
    } finally {
      setBusy((b) => ({ ...b, [id]: false }));
    }
  }

  const idOf = (c: CallRow) => String(c.id ?? c.call_id ?? "");
  const labelOf = (c: CallRow) => String(c.object_name ?? c.call_id ?? c.id ?? "-");

  return (
    <div>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="page-title">主管台</h1>
          <p className="page-sub">监听（进房旁听）· 暂停 AI · 接管 · 转人工 · 挂断</p>
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
              const isBusy = busy[id];
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

                  {rowMsg[id] && <p className="mt-2 text-xs text-emerald-400">{rowMsg[id]}</p>}
                  {rowErr[id] && <p className="mt-2 rounded bg-red-500/10 p-2 text-xs text-red-300">{rowErr[id]}</p>}

                  <div className="mt-3 flex flex-wrap gap-2">
                    {/* 监听 = 作为第二参与者进房旁听：打开会话页并连接（token 由 /api/token 签发）。 */}
                    <a
                      className="btn-ghost text-xs"
                      href={`/calls/${id}`}
                      target="_blank"
                      rel="noreferrer"
                      onClick={() => setRowMsg((m) => ({ ...m, [id]: "正在新窗口进房旁听…" }))}
                    >
                      监听/进入
                    </a>
                    {!paused ? (
                      <button className="btn-ghost text-xs" disabled={isBusy} onClick={() => act(id, "pause", "AI 已暂停。")}>
                        {isBusy ? "处理中…" : "暂停 AI"}
                      </button>
                    ) : (
                      <button className="btn-ghost text-xs" disabled={isBusy} onClick={() => act(id, "resume", "AI 已恢复。")}>
                        {isBusy ? "处理中…" : "恢复 AI"}
                      </button>
                    )}
                    <button className="btn-ghost text-xs" disabled={isBusy} onClick={() => act(id, "takeover", "已转人工接管。")}>
                      {isBusy ? "处理中…" : "接管"}
                    </button>
                    <button className="btn-ghost text-xs" disabled={isBusy} onClick={() => act(id, "transfer", "已转人工。")}>
                      {isBusy ? "处理中…" : "转人工"}
                    </button>
                    <button
                      className="rounded border border-red-500/30 bg-red-500/10 px-3 h-7 text-xs font-semibold text-red-400 transition hover:bg-red-500/20 disabled:opacity-50"
                      disabled={isBusy}
                      onClick={() => {
                        if (window.confirm("确认挂断该通话？将断开房间并触发结算。"))
                          act(id, "hangup", "已挂断。");
                      }}
                    >
                      {isBusy ? "处理中…" : "挂断"}
                    </button>
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
