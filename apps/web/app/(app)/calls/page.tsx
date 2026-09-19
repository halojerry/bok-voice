"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowRight, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { useAccount } from "@/components/account-context";
import { useSession } from "@/components/session-context";
import { CallStudio } from "@/components/CallStudio";
import { ErrorState, LoadingState } from "@/components/app-shell";

const STATUS: Record<string, [string, string]> = {
  active: ["进行中", "bg-emerald-500"],
  ringing: ["振铃", "bg-amber-400"],
  paused: ["已暂停", "bg-blue-400"],
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

// ===== 意向规则卡（W4-T3）：确定性条件 → 意向码/标签/处置，挂断时评估覆盖 =====

type IntentRuleRow = {
  id: string;
  account_id?: string;
  name?: string;
  intent_code?: string;
  label?: string;
  disposition?: string;
  priority?: number;
  enabled?: boolean;
  conditions?: IntentCond[];
};

type IntentCond = { fact?: string; op?: string; value?: unknown };

/** fact 目录（12 键，中文标签与 CP core `intent_rules.INTENT_FACTS` 手工同步）。 */
const INTENT_FACTS: [string, string][] = [
  ["duration_s", "通话时长秒"],
  ["nudge_fired", "心跳次数"],
  ["watchdog_fired", "看门狗次数"],
  ["storm_rounds", "风暴轮数"],
  ["repeat_count", "复述次数"],
  ["refuse_count", "拒绝次数"],
  ["objection_count", "异议次数"],
  ["confirm_count", "确认次数"],
  ["question_count", "提问次数"],
  ["step_max", "到达最大步"],
  ["wa_captured", "已捕获号码"],
  ["graph_notifies", "人工求助次数"],
];
const INTENT_FACT_LABEL: Record<string, string> = Object.fromEntries(INTENT_FACTS);
const INTENT_OPS: [string, string][] = [["gte", "≥"], ["lte", "≤"], ["eq", "="]];
const INTENT_OP_SYM: Record<string, string> = Object.fromEntries(INTENT_OPS);

/** 新建表单的条件行草稿：value 存输入原文（数字串或 "true"/"false"），提交时定型。 */
type IntentCondDraft = { fact: string; op: string; value: string };

const EMPTY_RULE_FORM = {
  name: "",
  intent_code: "",
  label: "",
  disposition: "",
  priority: "10",
  isGlobal: false,
  conditions: [] as IntentCondDraft[],
};

/** CP `validate_conditions` 拒字符串 value（只收数字/布尔）——提交侧唯一定型点。 */
function condDraftToPayload(c: IntentCondDraft): { fact: string; op: string; value: number | boolean } {
  if (c.fact === "wa_captured") return { fact: c.fact, op: c.op, value: c.value === "true" };
  const n = Number(c.value);
  return { fact: c.fact, op: c.op, value: Number.isFinite(n) ? n : 0 };
}

function condText(c: IntentCond): string {
  const fact = String(c.fact ?? "");
  const label = INTENT_FACT_LABEL[fact] ?? fact;
  const op = INTENT_OP_SYM[String(c.op ?? "")] ?? String(c.op ?? "?");
  if (fact === "wa_captured") return `${label}${op}${c.value === true || c.value === "true" ? "是" : "否"}`;
  return `${label}${op}${String(c.value ?? "")}`;
}

/** 「意向规则」折叠卡：列表（enabled 开关/删除/作用域徽标）+ 新建表单（条件行编辑器）。 */
function IntentRulesCard({ accountId }: { accountId: string }) {
  const session = useSession();
  // 「全局」作用域仅主管（admin/root）可见（计划 §7：session.role 判定）。
  const isManager = session?.role === "admin" || session?.role === "root";
  const [rules, setRules] = useState<IntentRuleRow[] | null>(null);
  const [rulesErr, setRulesErr] = useState("");
  const [busyId, setBusyId] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_RULE_FORM);
  const [formErr, setFormErr] = useState("");
  const [saving, setSaving] = useState(false);

  const loadRules = useCallback(async () => {
    try {
      const data = await api.listIntentRules(accountId);
      setRules((Array.isArray(data) ? data : []) as IntentRuleRow[]);
      setRulesErr("");
    } catch (e) {
      setRulesErr(String(e));
    }
  }, [accountId]);

  useEffect(() => {
    void loadRules();
  }, [loadRules]);

  async function toggleEnabled(r: IntentRuleRow) {
    const id = String(r.id);
    setBusyId(id);
    try {
      await api.updateIntentRule(id, { enabled: r.enabled === false });
      await loadRules();
    } catch (e) {
      setRulesErr(String(e));
    } finally {
      setBusyId("");
    }
  }

  async function removeRule(r: IntentRuleRow) {
    const id = String(r.id);
    if (!window.confirm(`确认删除意向规则「${String(r.name ?? id)}」？`)) return;
    setBusyId(id);
    try {
      await api.deleteIntentRule(id);
      await loadRules();
    } catch (e) {
      setRulesErr(String(e));
    } finally {
      setBusyId("");
    }
  }

  const setField = (patch: Partial<typeof EMPTY_RULE_FORM>) => {
    setForm((f) => ({ ...f, ...patch }));
    setFormErr("");
  };
  const setCond = (i: number, patch: Partial<IntentCondDraft>) =>
    setForm((f) => ({ ...f, conditions: f.conditions.map((c, k) => (k === i ? { ...c, ...patch } : c)) }));
  const addCond = () =>
    setForm((f) => ({ ...f, conditions: [...f.conditions, { fact: INTENT_FACTS[0][0], op: "gte", value: "1" }] }));
  const delCond = (i: number) =>
    setForm((f) => ({ ...f, conditions: f.conditions.filter((_, k) => k !== i) }));

  async function submitRule() {
    const name = form.name.trim();
    const code = form.intent_code.trim();
    if (!name) return setFormErr("规则名称不能为空。");
    if (!code) return setFormErr("意向码不能为空。");
    const body: Record<string, unknown> = {
      name,
      intent_code: code,
      label: form.label.trim(),
      disposition: form.disposition.trim(),
      priority: Number(form.priority) || 10,
      conditions: form.conditions.map(condDraftToPayload),
    };
    if (form.isGlobal) body.account_id = ""; // 全局行；缺省=CP 盖章本账号
    setSaving(true);
    try {
      await api.createIntentRule(body);
      setForm(EMPTY_RULE_FORM);
      setShowForm(false);
      setFormErr("");
      await loadRules();
    } catch (e) {
      setFormErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <details className="card mb-6">
      <summary className="cursor-pointer text-sm font-medium">
        意向规则 <span className="ml-1 text-xs muted">（挂断时按通话事实判意向码/处置）</span>
      </summary>
      <div className="mt-3 space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-xs muted">
            命中条件全部成立才判中；按优先级从小到大取第一条启用中的规则。全局规则对所有账号生效。
          </p>
          <button className="btn-ghost text-xs" onClick={() => setShowForm((v) => !v)}>
            {showForm ? "收起表单" : "＋新建规则"}
          </button>
        </div>

        {rulesErr && <ErrorState message={rulesErr} />}

        {/* 新建表单 */}
        {showForm && (
          <div className="space-y-3 rounded-lg border border-(--card-border) p-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="text-xs text-muted-foreground">规则名称 *</span>
                <input
                  className="input mt-1"
                  maxLength={64}
                  value={form.name}
                  onChange={(e) => setField({ name: e.target.value })}
                  placeholder="如：高意向客户"
                />
              </label>
              <label className="block">
                <span className="text-xs text-muted-foreground">意向码 *</span>
                <input
                  className="input mt-1"
                  maxLength={32}
                  value={form.intent_code}
                  onChange={(e) => setField({ intent_code: e.target.value })}
                  placeholder="如：HIGH_INTENT"
                />
              </label>
              <label className="block">
                <span className="text-xs text-muted-foreground">标签（可选）</span>
                <input
                  className="input mt-1"
                  maxLength={64}
                  value={form.label}
                  onChange={(e) => setField({ label: e.target.value })}
                  placeholder="如：愿意办理"
                />
              </label>
              <label className="block">
                <span className="text-xs text-muted-foreground">处置 disposition（可选，留空=只打意向码）</span>
                <input
                  className="input mt-1"
                  maxLength={32}
                  value={form.disposition}
                  onChange={(e) => setField({ disposition: e.target.value })}
                  placeholder="如：intended"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <label className="block">
                <span className="text-xs text-muted-foreground">优先级（小者先）</span>
                <input
                  type="number"
                  min={0}
                  max={1000}
                  className="input mt-1 w-24"
                  value={form.priority}
                  onChange={(e) => setField({ priority: e.target.value })}
                />
              </label>
              {isManager && (
                <label className="flex items-center gap-1.5 pb-2 text-xs muted">
                  <input
                    type="checkbox"
                    className="size-3 accent-(--live)"
                    checked={form.isGlobal}
                    onChange={(e) => setField({ isGlobal: e.target.checked })}
                  />
                  全局（管理员）
                </label>
              )}
            </div>

            {/* 条件行编辑器：fact 下拉 12 键 / op 下拉 / value（wa_captured=真假下拉，其余数字） */}
            <div>
              <span className="text-xs text-muted-foreground">命中条件（全部成立才判中）</span>
              <div className="mt-1 space-y-2">
                {form.conditions.length === 0 && (
                  <p className="text-[11px] muted">还没有条件：空条件集不参与判中，请至少添加一条。</p>
                )}
                {form.conditions.map((c, i) => (
                  <div key={i} className="flex flex-wrap items-center gap-2">
                    <span className="text-[10px] muted">{i + 1}</span>
                    <select
                      className="select text-xs"
                      value={c.fact}
                      onChange={(e) => setCond(i, { fact: e.target.value })}
                    >
                      {INTENT_FACTS.map(([k, zh]) => (
                        <option key={k} value={k}>{zh}</option>
                      ))}
                    </select>
                    <select
                      className="select text-xs"
                      value={c.op}
                      onChange={(e) => setCond(i, { op: e.target.value })}
                    >
                      {INTENT_OPS.map(([k, sym]) => (
                        <option key={k} value={k}>{sym}</option>
                      ))}
                    </select>
                    {c.fact === "wa_captured" ? (
                      <select
                        className="select text-xs"
                        value={c.value}
                        onChange={(e) => setCond(i, { value: e.target.value })}
                      >
                        <option value="true">是</option>
                        <option value="false">否</option>
                      </select>
                    ) : (
                      <input
                        type="number"
                        className="input w-24 text-xs"
                        value={c.value}
                        onChange={(e) => setCond(i, { value: e.target.value })}
                      />
                    )}
                    <button className="btn-ghost text-xs text-red-600" onClick={() => delCond(i)}>
                      删除
                    </button>
                  </div>
                ))}
              </div>
              <button className="btn-ghost mt-2 text-xs" onClick={addCond}>＋添加条件</button>
            </div>

            {formErr && <p className="text-sm text-red-600">{formErr}</p>}
            <div className="flex gap-2">
              <button className="btn-primary text-xs" disabled={saving} onClick={() => void submitRule()}>
                {saving ? "保存中…" : "创建规则"}
              </button>
              <button
                className="btn-ghost text-xs"
                onClick={() => { setShowForm(false); setForm(EMPTY_RULE_FORM); setFormErr(""); }}
              >
                取消
              </button>
            </div>
          </div>
        )}

        {/* 规则列表 */}
        {rules === null && !rulesErr && <p className="text-sm muted">加载中…</p>}
        {rules !== null && rules.length === 0 && !rulesErr && (
          <p className="text-sm muted">暂无规则。点「＋新建规则」按通话事实（时长/轮数/捕获等）配置意向判定。</p>
        )}
        {rules !== null && rules.length > 0 && (
          <div className="space-y-2">
            {rules.map((r) => {
              const id = String(r.id);
              const global = String(r.account_id ?? "") === "";
              const enabled = r.enabled !== false;
              const conds = Array.isArray(r.conditions) ? r.conditions : [];
              return (
                <div key={id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-muted/60 px-3 py-2">
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                      {String(r.name ?? "(未命名)")}
                      {global ? (
                        <span className="rounded-sm bg-muted px-1 text-[10px] muted">全局</span>
                      ) : (
                        <span className="rounded-sm bg-blue-100 px-1 text-[10px] text-blue-700">本账号</span>
                      )}
                      <span className="rounded-sm bg-(--live-soft) px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-(--live-ink)">
                        {String(r.intent_code ?? "")}
                      </span>
                    </p>
                    <p className="mt-0.5 text-xs muted">
                      标签 {String(r.label ?? "") || "-"} · 处置 {String(r.disposition ?? "") || "-"} · P
                      {Number(r.priority ?? 10)} · 条件：
                      {conds.length > 0 ? conds.map(condText).join("，") : "-"}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <label className="flex items-center gap-1 text-[11px] muted" title="启用/停用该规则">
                      <input
                        type="checkbox"
                        className="accent-(--live)"
                        checked={enabled}
                        disabled={busyId === id}
                        onChange={() => void toggleEnabled(r)}
                      />
                      启用
                    </label>
                    <button
                      className="btn-ghost text-xs text-red-600/80 hover:text-red-600"
                      disabled={busyId === id}
                      onClick={() => void removeRule(r)}
                    >
                      删除
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </details>
  );
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
  // 正在补结算的通话 id（行级 busy，防重复点击）。
  const [settlingId, setSettlingId] = useState("");
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
      const c = await api.listCalls(accountId, "");
      setRows(Array.isArray(c) ? c : []);
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
            <Plus className="h-3.5 w-3.5" /> 新建通话
          </Link>
        </div>
      </div>

      {err && <p className="mb-4 text-sm text-red-600">{err}</p>}
      {loading && <LoadingState />}

      {/* 意向规则（W4-T3）：折叠卡放页面顶部，列表+新建常驻可用（内嵌工作台时也不挡路） */}
      <IntentRulesCard accountId={accountId} />

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
