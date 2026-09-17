"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { useAccount } from "@/components/account-context";
import { CallStudio } from "@/components/CallStudio";

const STATUS: Record<string, [string, string]> = {
  active: ["进行中", "bg-emerald-500"],
  ringing: ["振铃", "bg-amber-400"],
  paused: ["已暂停", "bg-sky-400"],
  ended: ["已结束", "bg-neutral-500"],
  failed: ["失败", "bg-red-400"],
};

const WA_PENDING = ["offered", "captured"];

function modeLabel(mode: string) {
  return mode === "live" ? "真实业务" : "训练";
}

const LANG_LABEL: Record<string, string> = {
  zh: "中文",
  cantonese: "粤语",
  en: "英语",
  vi: "越南语",
};

function langLabel(lang: string): string {
  return LANG_LABEL[String(lang ?? "")] ?? String(lang ?? "-");
}

export default function CallsPage() {
  const { accountId } = useAccount();
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [objects, setObjects] = useState<Record<string, unknown>[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  // 选中的通话：同页内嵌工作台（静态导出无法为真实 call id 生成路由，改内嵌而非 /calls/[id]）。
  const [openId, setOpenId] = useState<string | null>(null);
  // 列表上限递增（QA 2026-09-13）：CP 全量返回 1000+ 通且最老在前——刚挂断的通话
  // 沉底=切换客户闭环断头；改客户端最新优先排序 + 只渲染最近 limit 通。
  const [limit, setLimit] = useState(50);
  const sortedRows = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) =>
      String(b.created_at ?? "").localeCompare(String(a.created_at ?? ""))
    );
    return arr;
  }, [rows]);
  const visibleRows = sortedRows.slice(0, limit);

  async function removeOne(id: string) {
    if (!window.confirm("确认删除该通话记录？（转写与结算一并删除）")) return;
    try {
      await api.deleteCall(id);
      setRows((prev) => prev.filter((r) => String(r.id ?? r.call_id) !== id));
      if (openId === id) setOpenId(null);
    } catch (e) {
      setErr(String(e));
    }
  }

  async function clearEnded() {
    if (!window.confirm("确认清空所有已结束的通话历史？此操作不可恢复。")) return;
    setClearing(true);
    try {
      await api.clearEndedCalls(accountId);
      setRows((prev) => prev.filter((r) => String(r.status ?? "") !== "ended"));
    } catch (e) {
      setErr(String(e));
    } finally {
      setClearing(false);
    }
  }

  useEffect(() => {
    setLoading(true);
    Promise.all([api.listCalls(accountId, ""), api.listObjects(accountId)])
      .then(([c, o]) => {
        setRows(Array.isArray(c) ? c : []);
        setObjects(Array.isArray(o) ? o : []);
        setErr(null);
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [accountId]);

  // 从内嵌工作台返回列表时立即刷新:挂断的那通此刻状态已变,不等 ≤4s 轮询
  // (否则行还挂着「进行中」,交互不自洽)。
  useEffect(() => {
    if (openId) return;
    api.listCalls(accountId, "")
      .then((c) => setRows(Array.isArray(c) ? c : []))
      .catch(() => {});
  }, [accountId, openId]);

  // 主管台橫幅撳「進入工作台」→ /calls?call=<id>:自動開嗰通工作台(靜態 export 用 query,唔使新 route)。
  useEffect(() => {
    const m = window.location.search.match(/[?&]call=([^&]+)/);
    if (m) setOpenId(decodeURIComponent(m[1]));
  }, []);

  // 列表 4s 輪詢:捉 active call 嘅 WhatsApp 待對接/狀態變化(主管台同步;開咗工作台就唔 poll)。
  useEffect(() => {
    if (openId) return;
    const t = setInterval(() => {
      api.listCalls(accountId, "")
        .then((c) => setRows(Array.isArray(c) ? c : []))
        .catch(() => {});
    }, 4000);
    return () => clearInterval(t);
  }, [accountId, openId]);

  function objectName(id: unknown) {
    const o = objects.find((x) => String(x.id) === String(id));
    return String(o?.display_name ?? id ?? "-");
  }

  return (
    <div>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="page-title">通话会话</h1>
          <p className="page-sub">进入历史/活跃会话，或发起新会话</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            className="btn-ghost text-xs muted"
            onClick={clearEnded}
            disabled={clearing}
          >
            {clearing ? "清理中…" : "清空已结束历史"}
          </button>
          <Link href="/calls/new" className="btn-primary">
            + 新建通话
          </Link>
        </div>
      </div>

      {err && <p className="mb-4 text-sm text-red-600">{err}</p>}
      {loading && <p className="text-sm muted">加载中…</p>}

      {openId && (
        <section className="mb-6">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-medium">会话工作台 · {openId}</span>
            <button className="btn-ghost text-xs" onClick={() => setOpenId(null)}>
              ← 返回列表
            </button>
          </div>
          <CallStudio
            callId={openId}
            onRequestNewCall={(oid) => {
              // 退出内嵌工作台 → 跳独立新建页并预选该对象（?object= 预选已验证路径）
              setOpenId(null);
              window.location.assign(`/calls/new?object=${encodeURIComponent(oid)}`);
            }}
          />
        </section>
      )}

      {!openId && (
        <section className="card">
          <div className="space-y-2">
            {!loading && rows.length === 0 && (
              <p className="text-sm muted">暂无会话，点击右上角「新建通话」开始。</p>
            )}
            {rows.length > 0 && (
              <p className="px-2 text-xs muted">
                共 {rows.length} 通 · 按最新优先显示 {visibleRows.length} 通
                {sortedRows.length > visibleRows.length && (
                  <button className="ml-2 text-(--live)" onClick={() => setLimit((v) => v + 100)}>
                    显示更多
                  </button>
                )}
              </p>
            )}
            {visibleRows.map((c) => {
              const id = String(c.id ?? c.call_id);
              const status = String(c.status ?? "idle");
              const [label, color] = STATUS[status] ?? [status, "bg-neutral-500"];
              const live = ["active", "paused", "ringing"].includes(status);
              const wa = String(c.whatsapp_status ?? "");
              const waPending = live && WA_PENDING.includes(wa);
              const waNum = String(c.customer_whatsapp ?? "");
              return (
                <div
                  key={id}
                  className={`flex items-center justify-between gap-2 rounded-lg px-2 py-1 transition hover:bg-accent ${
                    waPending ? "wa-flash bg-muted/60" : "bg-muted/60"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => setOpenId(id)}
                    className="flex min-w-0 flex-1 items-center justify-between gap-3 px-2 py-2 text-left"
                  >
                    <div className="min-w-0">
                      <p className="truncate font-medium">
                        {objectName(c.object_id)}
                        {waPending && (
                          <span className="ml-2 rounded-sm bg-(--live-soft) px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-(--live-ink)">
                            WhatsApp {wa === "captured" && waNum ? waNum : "待对接"}
                          </span>
                        )}
                      </p>
                      <p className="mt-1 text-xs muted">
                        {modeLabel(String(c.mode ?? "simulation"))} · {langLabel(String(c.language ?? ""))} ·{" "}
                        {String(c.created_at ?? "").slice(0, 19).replace("T", " ")}
                      </p>
                      <p className="mt-1 truncate text-xs muted">
                        {id}
                        {Number(c.turn_count ?? 0) > 0 && (
                          <span className="ml-2">
                            {Number(c.turn_count)} 轮 · 均延迟 {Number(c.avg_latency_ms) || "—"}
                            {Number(c.avg_latency_ms) ? "ms" : ""}
                          </span>
                        )}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      <span className="inline-flex items-center gap-1.5 text-xs muted">
                        <span className={`h-2 w-2 rounded-full ${color}`} />
                        {label}
                      </span>
                      <span className="text-(--live)">进入 →</span>
                    </div>
                  </button>
                  <button
                    className="btn-ghost shrink-0 text-xs text-red-600/80 hover:text-red-600"
                    onClick={() => removeOne(id)}
                    title="删除该通话记录"
                  >
                    删除
                  </button>
                  {status === "ended" && String(c.object_id ?? "") && (
                    <Link
                      href={`/calls/new?object=${encodeURIComponent(String(c.object_id))}`}
                      className="btn-ghost shrink-0 text-xs text-(--live)"
                      title="用同一对象发起新通话（工作台预选该对象）"
                    >
                      再拨
                    </Link>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
