"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { friendlyErrorText } from "@/lib/api-ready";
import { useAccount } from "@/components/account-context";

type CallRow = Record<string, unknown> & { id?: string; call_id?: string; status?: string };

const PENDING_WA = ["offered", "captured"];

export default function SupervisorPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<CallRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [objName, setObjName] = useState<Record<string, string>>({});
  const [copied, setCopied] = useState<string | null>(null);

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

  useEffect(() => {
    (async () => {
      try {
        const objs = await api.listObjects(accountId);
        const m: Record<string, string> = {};
        (Array.isArray(objs) ? objs : []).forEach((o) => {
          const rec = o as Record<string, unknown>;
          m[String(rec.id ?? "")] = String(rec.display_name ?? "");
        });
        setObjName(m);
      } catch {
        /* objects 拉唔到唔影響 WhatsApp 通知 */
      }
    })();
  }, [accountId]);

  const idOf = (c: CallRow) => String(c.id ?? c.call_id ?? "");
  const labelOf = (c: CallRow) =>
    String(objName[String(c.object_id ?? "")] ?? c.object_name ?? c.call_id ?? c.id ?? "-");
  const waStatus = (c: CallRow) => String(c.whatsapp_status ?? "");
  const waNum = (c: CallRow) => String(c.customer_whatsapp ?? "");

  const pending = rows.filter((c) => PENDING_WA.includes(waStatus(c)));

  async function copyNum(num: string) {
    try {
      await navigator.clipboard.writeText(num);
      setCopied(num);
      setTimeout(() => setCopied(null), 1200);
    } catch {
      /* clipboard 失敗靜默 */
    }
  }

  async function handleDone(c: CallRow) {
    try {
      await api.markWhatsappHandled(idOf(c));
      await refresh();
    } catch (e) {
      setErr(friendlyErrorText(String(e)));
    }
  }

  return (
    <div>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="page-title">主管台</h1>
          <p className="page-sub">
            只读监控（进行中通话 / 暂停 / WhatsApp 对接待办）。暂停 AI · 接管 · 转人工 · 挂断请到「通话会话」进入该通话的工作台操作。
          </p>
        </div>
        <button className="btn-ghost" onClick={() => refresh()}>刷新</button>
      </div>

      {err && <p className="mb-4 rounded-lg bg-red-500/10 p-3 text-sm text-red-300">{err}</p>}

      {/* WhatsApp 對接橫幅區:有待對接 call 先顯示,撳「已對接」就收起 */}
      {pending.length > 0 && (
        <section className="mb-6 space-y-3">
          {pending.map((c) => {
            const id = idOf(c);
            const st = waStatus(c);
            const num = waNum(c);
            const isCaptured = st === "captured";
            return (
              <div key={`banner-${id}`} className="wa-flash rounded-lg border border-[var(--accent)] bg-[var(--card)] p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-[var(--accent)]">
                      📱 WhatsApp 待对接
                      <span className="ml-2 rounded bg-[var(--accent)]/15 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider">
                        {isCaptured ? "已拿到号码" : "客户已应承加"}
                      </span>
                    </p>
                    <p className="mt-1 text-sm text-[var(--foreground)]">
                      {labelOf(c)} <span className="text-[var(--muted)]">· {id}</span>
                    </p>
                    {isCaptured && num ? (
                      <p className="mt-0.5 font-mono text-lg tracking-wider text-[var(--foreground)]">
                        {num}
                        {copied === num && <span className="ml-2 text-xs text-emerald-400">已复制 ✓</span>}
                      </p>
                    ) : (
                      <p className="mt-0.5 text-xs text-[var(--muted)]">客户应承咗加专员,等紧佢俾号码 / 由专员主动联系。</p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-2">
                    {isCaptured && num && (
                      <button className="btn-ghost text-xs" onClick={() => copyNum(num)}>
                        {copied === num ? "已复制" : "复制号码"}
                      </button>
                    )}
                    <button className="btn-primary text-xs" onClick={() => handleDone(c)}>标记已对接</button>
                    <Link href={`/calls?call=${encodeURIComponent(id)}`} className="btn-ghost text-xs">
                      进入工作台
                    </Link>
                  </div>
                </div>
              </div>
            );
          })}
        </section>
      )}

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
              const wa = waStatus(c);
              const waPending = PENDING_WA.includes(wa);
              return (
                <div key={id} className={`rounded-lg p-4 ${waPending ? "wa-flash bg-white/5" : "bg-white/5"}`}>
                  <div className="flex items-center justify-between">
                    <div className="min-w-0">
                      <p className="truncate font-medium">
                        {labelOf(c)}
                        {waPending && (
                          <span className="ml-2 rounded bg-[var(--accent)]/15 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-[var(--accent)]">
                            WhatsApp {wa === "captured" && waNum(c) ? waNum(c) : "待对接"}
                          </span>
                        )}
                      </p>
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
                    {waPending && (
                      <button className="btn-primary text-xs" onClick={() => handleDone(c)}>标记 WhatsApp 已对接</button>
                    )}
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
