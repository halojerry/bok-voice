"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { AppShell, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { friendlyErrorText, useControlPlaneReady } from "@/lib/api-ready";

// ---- 宽类型工具：/api/stats/dashboard 全字段兜底，端点缺位/字段缺失不白屏 ----
type Row = Record<string, unknown>;

function asRecord(value: unknown): Row | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Row) : null;
}

function asRows(value: unknown): Row[] {
  return Array.isArray(value) ? value.filter((v) => asRecord(v) !== null).map((v) => v as Row) : [];
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function fmtCount(value: unknown): string {
  const n = asNumber(value);
  return n === null ? "—" : String(n);
}

function fmtPercent(rate: unknown): string {
  const n = asNumber(rate);
  return n === null ? "—" : `${(n * 100).toFixed(1)}%`;
}

// 标记计数 map（tags.disposition / tags.whatsapp）：按数量降序、零值剔除，展示层不再判断。
function asCountMap(value: unknown): [string, number][] {
  const rec = asRecord(value);
  if (!rec) return [];
  return Object.entries(rec)
    .map(([k, v]) => [k, asNumber(v) ?? 0] as [string, number])
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

// 标记中文映射（plan Task 6 规格）；未知键原样显示。
const DISPOSITION_LABELS: Record<string, string> = {
  completed: "已完成",
  declined: "婉拒",
  abandoned: "超时放弃",
  no_answer: "未接听",
  rejected: "拒接",
  failed: "失败",
  transferred: "转人工",
};

const WHATSAPP_LABELS: Record<string, string> = {
  offered: "已邀约",
  captured: "已捕获",
  handled: "已对接",
};

// 待办事项（2026-09-18 用户补卡）：运行中战役四桶账；契约外未知键原样显示。
const TODO_LABELS: Record<string, string> = {
  to_call: "待外呼",
  waiting_redispatch: "等重拨（间隔中）",
  due_redispatch: "重拨到期",
  exhausted: "重拨耗尽 · 需人工",
};

// 时长分布五桶固定顺序（后端契约键名）；契约外未知键追加在尾部。
const DURATION_BUCKET_ORDER = ["0-15", "15-30", "30-60", "60-90", "90+"];

function bucketRows(buckets: Row | null): [string, number][] {
  if (!buckets) return [];
  const known = DURATION_BUCKET_ORDER.map(
    (name) => [name, asNumber(buckets[name]) ?? 0] as [string, number],
  );
  const extra = Object.keys(buckets)
    .filter((k) => !DURATION_BUCKET_ORDER.includes(k))
    .map((k) => [k, asNumber(buckets[k]) ?? 0] as [string, number]);
  return [...known, ...extra];
}

function TagTable({
  title,
  rows,
  labels,
}: {
  title: string;
  rows: [string, number][];
  labels: Record<string, string>;
}) {
  return (
    <div>
      <p className="text-xs muted">{title}</p>
      {rows.length > 0 ? (
        <table className="mt-1 w-full text-sm">
          <tbody>
            {rows.map(([key, count]) => (
              <tr key={key} className="border-t border-(--card-border) first:border-t-0">
                <td className="py-1.5">{labels[key] ?? key}</td>
                <td className="py-1.5 text-right muted">{count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="mt-2 text-sm muted">暂无数据</p>
      )}
    </div>
  );
}

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
  const [stats, setStats] = useState<Row | null>(null);
  const [calls, setCalls] = useState<Row[]>([]);
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
      // 仪表盘端点未上线（旧 CP 404）时不拖垮整页：stats 落 null，全部 KPI 兜底「—」。
      // 404/网络中断静默降级；其余错误 warn 一声再降级——P0 信息面，别整页报错。
      api
        .statsDashboard()
        .catch((e: unknown) => {
          const msg = String(e);
          if (!/\b404\b/.test(msg) && !(e instanceof TypeError)) {
            console.warn("statsDashboard degraded:", msg);
          }
          return null;
        }),
      api.listCalls(accountId, ""),
    ])
      .then(([s, c]) => {
        if (cancelled) return;
        setStats(asRecord(s));
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

  const concurrency = asRecord(stats?.concurrency);
  const callsAgg = asRecord(stats?.calls);
  const tags = asRecord(stats?.tags);
  const disposition = asCountMap(tags?.disposition);
  const whatsapp = asCountMap(tags?.whatsapp);
  const todo = asCountMap(stats?.todo);
  const tagsTotal = tags
    ? disposition.reduce((sum, [, n]) => sum + n, 0) + whatsapp.reduce((sum, [, n]) => sum + n, 0)
    : null;
  // 坐席排行：端点已截 8，这里对异常形状（旧 CP/篡改响应）再兜一层，防长表撑爆卡片。
  const agents = asRows(stats?.agents).slice(0, 8);
  const buckets = bucketRows(asRecord(stats?.duration_buckets));
  const maxBucket = Math.max(1, ...buckets.map(([, n]) => n));

  // 六 KPI 卡（plan Task 6）：主值 + 可选副行；缺字段一律「—」。
  const kpis: [string, string, string?][] = [
    ["当前并发", fmtCount(concurrency?.current)],
    ["今日呼叫", fmtCount(callsAgg?.today), `累计 ${fmtCount(callsAgg?.total)}`],
    ["客户接通率", fmtPercent(callsAgg?.answer_rate), `接通 ${fmtCount(callsAgg?.answered)}`],
    // 今日接通量：新 CP 用 answered_today；旧 CP 无该字段回退全时段 answered（T5-M3）。
    // 卡 3 副行「接通」保持全时段 answered 不动——answer_rate 分母是全时段口径。
    ["今日接通量", fmtCount(callsAgg?.answered_today ?? callsAgg?.answered), `通话中 ${fmtCount(concurrency?.current)}`],
    ["标记总数", tagsTotal === null ? "—" : String(tagsTotal)],
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
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
            {kpis.map(([k, v, sub]) => (
              <div key={k} className="card">
                <p className="label">{k}</p>
                <p className="mt-2 text-2xl font-semibold">{v}</p>
                {sub && <p className="mt-1 text-xs muted">{sub}</p>}
              </div>
            ))}
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* 时长分布：CSS 横条（div 宽度 = count/max * 100%），空数据画零条 */}
            <section className="card">
              <span className="label">通话时长分布（秒）</span>
              <div className="mt-3 space-y-2.5">
                {buckets.map(([name, count]) => (
                  <div key={name} className="flex items-center gap-3">
                    <div className="h-2 flex-1 rounded bg-muted/60">
                      <div
                        className="h-2 rounded bg-live"
                        style={{ width: `${Math.round((count / maxBucket) * 100)}%` }}
                      />
                    </div>
                    <span className="w-24 shrink-0 text-right text-xs muted">
                      {name} · {count}
                    </span>
                  </div>
                ))}
                {buckets.length === 0 && <p className="text-sm muted">暂无数据</p>}
              </div>
            </section>

            {/* 坐席排行 */}
            <section className="card">
              <span className="label">坐席排行</span>
              {agents.length > 0 ? (
                <table className="mt-3 w-full text-sm">
                  <thead>
                    <tr className="text-left muted">
                      <th className="py-1">排名</th>
                      <th className="py-1">坐席</th>
                      <th className="py-1">呼叫</th>
                      <th className="py-1">接通</th>
                    </tr>
                  </thead>
                  <tbody>
                    {agents.map((a, i) => (
                      <tr key={String(a.user_id ?? i)} className="border-t border-(--card-border)">
                        <td className="py-1.5 muted">{i + 1}</td>
                        <td className="py-1.5">{String(a.name || a.user_id || "—")}</td>
                        <td className="py-1.5">{fmtCount(a.calls)}</td>
                        <td className="py-1.5">{fmtCount(a.answered)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="mt-3 text-sm muted">暂无数据</p>
              )}
            </section>
          </div>

          {/* 待办事项：运行中战役的待外呼/重拨/耗尽四桶（2026-09-18 用户补卡） */}
          <section className="card mt-6">
            <span className="label">待办事项</span>
            <div className="mt-3">
              <TagTable title="运行中战役" rows={todo} labels={TODO_LABELS} />
            </div>
          </section>

          {/* 标记统计：通话结果 / WhatsApp 双表 */}
          <section className="card mt-6">
            <span className="label">标记统计</span>
            <div className="mt-3 grid grid-cols-1 gap-6 md:grid-cols-2">
              <TagTable title="通话结果" rows={disposition} labels={DISPOSITION_LABELS} />
              <TagTable title="WhatsApp" rows={whatsapp} labels={WHATSAPP_LABELS} />
            </div>
          </section>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1fr_340px]">
            <section className="card">
              <div className="flex items-center justify-between">
                <span className="label">最近会话</span>
                <Link href="/calls" className="text-xs text-(--live)">查看全部 →</Link>
              </div>
              <div className="mt-3 space-y-2">
                {calls.slice(0, 6).map((call) => (
                  <Link
                    key={String(call.id ?? call.call_id)}
                    // 静态导出无法为真实 call id 生成 /calls/<id> 路由：统一去 /calls 列表，
                    // 在列表里点「进入」会内嵌工作台打开该会话。
                    href="/calls"
                    className="flex items-center justify-between rounded-lg bg-muted/60 px-4 py-3 hover:bg-accent"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{String(call.object_id ?? "-")}</p>
                      <p className="text-xs muted">
                        {String(call.status ?? "-")} · {String(call.mode ?? "-")} · {String(call.language ?? "-")}
                      </p>
                    </div>
                    <span className="text-(--live)">查看 →</span>
                  </Link>
                ))}
                {calls.length === 0 && <p className="text-sm muted">暂无会话，从「新建通话」开始。</p>}
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
              <div className="mt-4 rounded-lg bg-muted/60 p-3 text-xs muted">
                控制面状态：{health === false ? "离线" : health === true ? "在线" : "未知"}
              </div>
            </section>
          </div>
        </>
      )}
    </>
  );
}
