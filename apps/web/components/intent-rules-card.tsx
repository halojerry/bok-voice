"use client";

// 客户意向规则卡（W4-T3 抽组件，2026-09-20）：确定性条件 → 意向码/标签/处置，
// 挂断时评估覆盖 disposition。原在 /calls 页内联；重设计后 AI 工作站「客户意向」
// tab 与 /calls 页共用（逻辑逐字搬出，不改行为）。

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorState } from "@/components/app-shell";
import { useSession } from "@/components/session-context";

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

/** 「意向规则」折叠卡：列表（enabled 开关/删除/作用域徽标）+ 新建表单（条件行编辑器）。
 *  嵌入方：/calls 页（原位）+ /studio 客户意向 tab。 */
export default function IntentRulesCard({ accountId }: { accountId: string }) {
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
    <details className="card">
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
