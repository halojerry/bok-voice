"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { ArrowRight, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { useCallsList, useObjectsList } from "@/lib/swr";
import { useAccount } from "@/components/account-context";
import { LoadingState } from "@/components/app-shell";
import IntentRulesCard from "@/components/intent-rules-card";

// 内嵌工作台懒加载（2026-10-02 UX 根因修复）：CallStudio 静态引入会把 livekit 706KB +
// streamdown 467KB + agents-ui 246KB 全部打进 /calls 初始包（共 2.1MB）；列表态只有
// 点开 ?call= 才需要工作台——dynamic 后重媒体栈只进按需 chunk（/calls/new 保持直引，
// 该页首屏即工作台）。/qa 画布 dynamic() 同款先例。
const CallStudio = dynamic(() => import("@/components/CallStudio").then((m) => m.CallStudio), {
  ssr: false,
  loading: () => <LoadingState label="载入工作台…" />,
});

const STATUS: Record<string, [string, string]> = {
  active: ["进行中", "bg-emerald-500"],
  ringing: ["振铃", "bg-amber-400"],
  paused: ["已暂停", "bg-blue-400"],
  ended: ["已结束", "bg-neutral-500"],
  failed: ["失败", "bg-red-400"],
};

const WA_PENDING = ["offered", "captured"];

function modeLabel(mode: string) {
  if (mode === "realtime_demo") return "演示档";
  return mode === "live" ? "真实业务" : "训练";
}

const LANG_LABEL: Record<string, string> = {
  zh: "中文",
  cantonese: "粤语",
  en: "英语",
  vi: "越南语",
};

function langLabel(lang: string) {
  return LANG_LABEL[String(lang ?? "")] ?? String(lang ?? "-");
}

// 意向规则卡（W4-T3，2026-09-20 抽 components/intent-rules-card.tsx）：
// /calls 与 AI 工作站「客户意向」tab 共用。

export default function CallsPage() {
  const { accountId } = useAccount();
  const [err, setErr] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  // 选中的通话：同页内嵌工作台（静态导出无法为真实 call id 生成路由，改内嵌而非 /calls/[id]）。
  const [openId, setOpenId] = useState<string | null>(null);
  // 服务端分页页量（2026-10-02 改造；旧客户端全量排序注释存档）：limit 直传
  // GET /api/calls?limit=（CP created_at 倒序截断），「显示更多」+100；
  // SWR keepPreviousData 换 key 时旧列表留屏不闪白。
  const [limit, setLimit] = useState(50);
  // 正在补结算的通话 id（行级 busy，防重复点击）。
  const [settlingId, setSettlingId] = useState("");
  const router = useRouter();
  // 数据层（2026-10-02 UX 根因修复）：SWR 缓存+去重+轮询接管列表——旧形状=裸 fetch
  // 全量回传（生产 1570 行）浏览器再排序再切 50、4s 裸 setInterval 全量重拉、
  // 每次导航进页全冷取。新形状：服务端 limit 分页（created_at 倒序截断）、
  // SWR refreshInterval 轮询（tab 隐藏自停）、缓存导航秒开。开着内嵌工作台时
  // 轮询暂停（旧语义保留）。
  const { data: rowsData, isLoading: loading, error: callsErr, mutate: mutateCalls } =
    useCallsList(accountId, "", limit, openId ? 0 : 4000);
  const { data: objectsData } = useObjectsList(accountId);
  const rows = rowsData ?? [];
  const objects = objectsData ?? [];
  const fetchErr = callsErr ? String(callsErr) : null;
  const sortedRows = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) =>
      String(b.created_at ?? "").localeCompare(String(a.created_at ?? ""))
    );
    return arr;
  }, [rows]);
  const visibleRows = sortedRows.slice(0, limit);

  /** 补结算（W5-T2 孤儿 API 收编：api.settle 此前无 UI 消费点）：漏走挂断结算的
   *  ended 通话手动补一次（纪要/知识沉淀）；成功本地刷新列表，失败亮错误行。 */
  async function settleOne(id: string) {
    if (!window.confirm("为这通通话补一次挂断结算？（生成纪要/知识沉淀）")) return;
    setSettlingId(id);
    try {
      await api.settle(id);
      setErr(null);
      void mutateCalls();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSettlingId("");
    }
  }

  async function removeOne(id: string) {
    if (!window.confirm("确认删除该通话记录？（转写与结算一并删除）")) return;
    try {
      await api.deleteCall(id);
      void mutateCalls();
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
      void mutateCalls();
    } catch (e) {
      setErr(String(e));
    } finally {
      setClearing(false);
    }
  }

  // 从内嵌工作台返回列表时立即重验:挂断的那通此刻状态已变,不等 ≤4s 轮询
  // (否则行还挂着「进行中」,交互不自洽)。首挂载时 SWR 已自取,此处被去重窗吞掉。
  useEffect(() => {
    if (!openId) void mutateCalls();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openId]);

  // 主管台橫幅撳「進入工作台」→ /calls?call=<id>:自動開嗰通工作台(靜態 export 用 query,唔使新 route)。
  useEffect(() => {
    const m = window.location.search.match(/[?&]call=([^&]+)/);
    if (m) setOpenId(decodeURIComponent(m[1]));
  }, []);

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
            <Plus className="h-3.5 w-3.5" /> 新建通话
          </Link>
        </div>
      </div>

      {(err || fetchErr) && <p className="mb-4 text-sm text-red-600">{err || fetchErr}</p>}
      {loading && <LoadingState />}

      {/* 意向规则（W4-T3）：折叠卡放页面顶部，列表+新建常驻可用（内嵌工作台时也不挡路） */}
      <div className="mb-6">
        <IntentRulesCard accountId={accountId} />
      </div>

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
              router.push(`/calls/new?object=${encodeURIComponent(oid)}`);
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
                已载入 {rows.length} 通（最新优先）
                {rows.length >= limit && (
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
                        {String(c.mode ?? "") === "realtime_demo" && (
                          <span
                            className="ml-2 rounded-sm bg-fuchsia-500/15 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fuchsia-600"
                            title="云端 Realtime 演示档（root 建单，出境计费）"
                          >
                            演示档
                          </span>
                        )}
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
                      <span className="text-(--live)">进入 <ArrowRight className="h-3.5 w-3.5" /></span>
                    </div>
                  </button>
                  <button
                    className="btn-ghost shrink-0 text-xs text-red-600/80 hover:text-red-600"
                    onClick={() => removeOne(id)}
                    title="删除该通话记录"
                  >
                    删除
                  </button>
                  {status === "ended" && (
                    <button
                      className="btn-ghost shrink-0 text-xs"
                      disabled={settlingId === id}
                      onClick={() => settleOne(id)}
                      title="补一次挂断结算（纪要/知识沉淀）"
                    >
                      {settlingId === id ? "结算中…" : "补结算"}
                    </button>
                  )}
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
